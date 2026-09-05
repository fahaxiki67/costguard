"""Sheet 单元格摘要（缓存感知）——结转判定与 Run Contract 共用同一实现。

摘要算法只有这一份：raw_cells 按 (row, col) 序逐格 canonical_json 后
SHA-256，结果落 sheet_cell_digests 缓存表（v53）。raw_cells 按 sheet_id
插入后不可变（重解析产生新 sheet_id），因此缓存永久有效，可随时清空重建。
任何需要比较「两个 Sheet 内容是否一致」的场景（如重解析人工确认结转）
必须调用本函数，不得另写算法产生第二套口径。
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

from jiadun.core.evidence.finding import canonical_json


def sheet_cell_digest(conn: sqlite3.Connection, sheet_id: int) -> str:
    """返回 sheet 的原始单元格 SHA-256；优先读缓存，miss 时计算并写缓存。"""
    cached = conn.execute(
        "SELECT digest FROM sheet_cell_digests WHERE sheet_id=?",
        (int(sheet_id),),
    ).fetchone()
    if cached is not None:
        return str(cached["digest"])
    digest = hashlib.sha256()
    cells = conn.execute(
        """SELECT row, col, raw_value, cached_value, is_formula,
                  is_number_stored_as_text, num_fmt
           FROM raw_cells WHERE sheet_id=? ORDER BY row, col""",
        (int(sheet_id),),
    ).fetchall()
    for cell in cells:
        digest.update(
            canonical_json({
                "row": int(cell["row"]),
                "col": int(cell["col"]),
                "raw_value": cell["raw_value"],
                "cached_value": cell["cached_value"],
                "is_formula": int(cell["is_formula"] or 0),
                "is_number_stored_as_text": int(cell["is_number_stored_as_text"] or 0),
                "num_fmt": cell["num_fmt"] or "",
            }).encode("utf-8")
        )
        digest.update(b"\n")
    hexdigest = digest.hexdigest()
    conn.execute(
        """INSERT INTO sheet_cell_digests(sheet_id, digest, cell_count, computed_at)
           VALUES (?,?,?,?)
           ON CONFLICT(sheet_id) DO UPDATE SET
               digest=excluded.digest, cell_count=excluded.cell_count,
               computed_at=excluded.computed_at""",
        (int(sheet_id), hexdigest, len(cells),
         datetime.now().isoformat(timespec="seconds")),
    )
    return hexdigest
