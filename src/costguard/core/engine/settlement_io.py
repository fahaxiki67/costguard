"""结算文件导入编排：解析 → 表头识别 → 期次归属 → 行抽取 → 落库。

一条管线供 UI / CLI / 测试共用。报告每个 sheet 的处理结论，绝不静默丢弃。
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from costguard.core.models.source_file import import_file
from costguard.core.parsing import excel_parser, extract_items
from costguard.core.parsing.header_detect import detect_header


@dataclass
class SheetReport:
    sheet_name: str
    status: str  # 'parsed' | 'no_header' | 'duplicate_header' | 'needs_review'
    n_items: int = 0
    n_subtotal: int = 0
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    file_id: int
    batch_id: int | None
    period_no: int
    period_id: int
    status: str  # 'ok' | 'partial' | 'failed'
    sheets: list[SheetReport] = field(default_factory=list)
    message: str = ""


_PERIOD_RE = re.compile(r"第\s*([0-9一二三四五六七八九十]+)\s*期")


def _cn_to_int(s: str) -> int:
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if s.isdigit():
        return int(s)
    return digits.get(s, 0)


def guess_period_no(path: Path) -> int | None:
    m = _PERIOD_RE.search(path.stem)
    return _cn_to_int(m.group(1)) if m else None


def load_sheet_grid(conn: sqlite3.Connection, sheet_id: int) -> tuple[dict, list[str], int, int]:
    rows = conn.execute(
        "SELECT row, col, raw_value, cached_value, is_formula FROM raw_cells WHERE sheet_id=?",
        (sheet_id,),
    ).fetchall()
    cells: dict[tuple[int, int], str] = {}
    for r in rows:
        text = r["cached_value"] if r["is_formula"] and r["cached_value"] is not None else r["raw_value"]
        if text:
            cells[(r["row"], r["col"])] = text
    meta = conn.execute(
        "SELECT sheet_name, n_rows, n_cols, merged_ranges_json FROM raw_sheets WHERE id=?",
        (sheet_id,),
    ).fetchone()
    merged = json.loads(meta["merged_ranges_json"])
    return cells, merged, meta["n_rows"], meta["n_cols"]


def next_period_no(conn: sqlite3.Connection, project_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(period_no), 0) AS m FROM settlement_periods WHERE project_id=?",
        (project_id,),
    ).fetchone()
    return int(row["m"]) + 1


def ensure_period(
    conn: sqlite3.Connection,
    project_id: int,
    period_no: int,
    title: str,
    source_file_id: int | None,
    direction: str = "unknown",
    contract_party: str = "",
) -> int:
    row = conn.execute(
        "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?",
        (project_id, period_no),
    ).fetchone()
    if row:
        return int(row["id"])
    with conn:
        cur = conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, source_file_id, direction, contract_party)"
            " VALUES (?,?,?,?,?,?)",
            (project_id, period_no, title, source_file_id, direction, contract_party),
        )
        return int(cur.lastrowid)


def import_settlement_file(
    conn: sqlite3.Connection,
    project_id: int,
    project_dir: Path,
    src: Path,
    period_no: int | None = None,
    direction: str = "unknown",
    contract_party: str = "",
) -> ImportReport:
    """导入并解析一个结算文件。期次缺省按文件名猜测，猜不出按递增编号。"""
    src = Path(src)
    sf = import_file(conn, project_id, project_dir, src)
    if period_no is None:
        period_no = guess_period_no(src) or next_period_no(conn, project_id)

    result = excel_parser.parse_file(Path(sf.stored_path), sf.file_type)
    if result.status != "ok":
        return ImportReport(sf.file_id, None, period_no, -1, "failed", message=result.error)

    from costguard.core.parsing.excel_parser import persist_parse_result

    batch_id = persist_parse_result(conn, sf.file_id, result)
    period_id = ensure_period(conn, project_id, period_no, src.stem, sf.file_id, direction, contract_party)

    report = ImportReport(sf.file_id, batch_id, period_no, period_id, "partial")
    parsed_any = False
    for sheet in result.sheets:
        sheet_id = conn.execute(
            "SELECT id FROM raw_sheets WHERE batch_id=? AND sheet_index=?", (batch_id, sheet.sheet_index)
        ).fetchone()["id"]
        cells, merged, n_rows, _ = load_sheet_grid(conn, sheet_id)
        det = detect_header(sheet.sheet_index, cells, merged, n_rows, sheet.n_cols)
        if det is None:
            report.sheets.append(SheetReport(sheet.sheet_name, "no_header"))
            continue
        items = extract_items.extract_items(cells, merged, det, n_rows)
        if not items:
            report.sheets.append(SheetReport(sheet.sheet_name, "no_header", notes=["header-like but 0 data rows"]))
            continue
        extract_items.persist_line_items(conn, period_id, sheet_id, items)
        parsed_any = True
        status = "needs_review" if det.needs_review else "parsed"
        report.sheets.append(
            SheetReport(
                sheet.sheet_name, status,
                n_items=len([i for i in items if not i.flags.get("subtotal")]),
                n_subtotal=len([i for i in items if i.flags.get("subtotal")]),
                confidence=det.confidence, notes=det.notes,
            )
        )
    report.status = "ok" if parsed_any else "partial" if any(s.status == "needs_review" for s in report.sheets) else "partial"
    if not parsed_any and not report.sheets:
        report.status = "failed"
        report.message = "no sheets parsed"
    return report
