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


def guess_period_no_from_text(text: str) -> int | None:
    m = _PERIOD_RE.search(text or "")
    return _cn_to_int(m.group(1)) if m else None


def import_settlement_file(
    conn: sqlite3.Connection,
    project_id: int,
    project_dir: Path,
    src: Path,
    period_no: int | None = None,
    direction: str = "unknown",
    contract_party: str = "",
) -> ImportReport:
    """导入并解析一个结算文件。

    期次归属（v2 模型）：一个工作簿可含多期。逐 Sheet 判定期号——
    Sheet 名或表前标题含"第N期"则用之；否则用文件名期号；再否则按递增编号。
    """
    src = Path(src)
    sf = import_file(conn, project_id, project_dir, src)
    file_period = period_no if period_no is not None else guess_period_no(src)
    used_increment = file_period is None

    result = excel_parser.parse_file(Path(sf.stored_path), sf.file_type)
    if result.status != "ok":
        fallback = file_period or next_period_no(conn, project_id)
        return ImportReport(sf.file_id, None, fallback, -1, "failed", message=result.error)

    from costguard.core.parsing.excel_parser import persist_parse_result

    batch_id = persist_parse_result(conn, sf.file_id, result)

    report = ImportReport(sf.file_id, batch_id, file_period or 0, -1, "partial")
    parsed_any = False
    period_ids: set[int] = set()
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

        # ---- 逐 Sheet 期次判定 ----
        # 优先级：用户显式指定 > 文件名期号 > Sheet 名期号 > 递增。
        # 文件名与 Sheet 名期号冲突时按文件名（导入语境更明确）并提示复核。
        sheet_pno = guess_period_no_from_text(sheet.sheet_name) or guess_period_no_from_text(
            " ".join(t for (r, _c), t in sorted(cells.items()) if r <= 5)
        )
        notes: list[str] = []
        if used_increment:
            pno = next_period_no(conn, project_id) if parsed_any else (file_period or sheet_pno or next_period_no(conn, project_id))
        elif file_period is not None:
            pno = file_period
            if sheet_pno is not None and sheet_pno != file_period:
                notes.append(f"Sheet 名期号({sheet_pno})与文件名期号({file_period})不一致，按文件名处理，请复核")
        else:
            pno = sheet_pno if sheet_pno is not None else next_period_no(conn, project_id)
        title = f"{src.stem}/{sheet.sheet_name}"
        period_id = ensure_period(conn, project_id, pno, title, sf.file_id, direction, contract_party)
        with conn:
            conn.execute("UPDATE raw_sheets SET period_id=? WHERE id=?", (period_id, sheet_id))
            conn.execute(
                """INSERT INTO table_headers(sheet_id, header_row_lo, header_row_hi, col_map_json,
                   confidence, needs_review) VALUES (?,?,?,?,?,?)""",
                (sheet_id, det.header_row_lo, det.header_row_hi,
                 json.dumps(det.col_map), det.confidence, int(det.needs_review)),
            )
        period_ids.add(period_id)

        extract_items.persist_line_items(conn, period_id, sheet_id, items)
        parsed_any = True
        status = "needs_review" if det.needs_review else "parsed"
        report.sheets.append(
            SheetReport(
                sheet.sheet_name, status,
                n_items=len([i for i in items if not i.flags.get("subtotal")]),
                n_subtotal=len([i for i in items if i.flags.get("subtotal")]),
                confidence=det.confidence, notes=det.notes + notes,
            )
        )
    report.period_id = next(iter(period_ids), -1)
    report.period_no = min(
        (conn.execute("SELECT period_no FROM settlement_periods WHERE id=?", (pid,)).fetchone()["period_no"]
         for pid in period_ids), default=0,
    )
    report.status = "ok" if parsed_any else "failed"
    if not parsed_any:
        report.message = "no sheets parsed"
    return report
