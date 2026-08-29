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
    needs_manual_review: bool = False


_PERIOD_RE = re.compile(r"第\s*([0-9一二三四五六七八九十]+)\s*期")

# sheet 级写入闸门（监督第八轮）：
CONF_WRITE_GATE = 0.70   # 需角色审阅的置信度下限（低于此不写 canonical）
ROW_GUARD = 500          # 文件级大表护栏：任一 sheet 超过该行数 → 整文件需角色审阅


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


def next_period_no(conn: sqlite3.Connection, project_id: int,
                   direction: str = "unknown") -> int:
    """下一个可用期号——按方向独立递增（对上/对下各自有序）。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(period_no), 0) AS m FROM settlement_periods"
        " WHERE project_id=? AND direction=?",
        (project_id, direction),
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
    """按 (project_id, period_no, direction) 定位或创建期次。

    对上/对下各自拥有独立的期号序列：同一 period_no 的不同方向是两个期次，
    不得复用（方向隔离）。同方向同期号重复调用幂等复用（重复导入安全）。
    """
    row = conn.execute(
        "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=? AND direction=?",
        (project_id, period_no, direction),
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


def _route_role_review(conn: sqlite3.Connection, project_id: int, file_id: int,
                       sheet_id: int, sheet_name: str,
                       confidence: float | None, oversized: bool) -> None:
    """需角色审阅 sheet：留 evidence 候选与审计，不写 canonical tables。"""
    from costguard.core.evidence import audit as audit_log
    from costguard.core.evidence import evidence as evidence_api

    reason = ("超长汇总/台账/核销型结构（行数超护栏），疑似非结算清单"
              if oversized else f"表头识别置信度不足（{confidence}），不得直接写入结算模型")
    evidence_api.add_evidence(
        conn, project_id, "sheet_role_candidate",
        f"Sheet「{sheet_name}」待人工角色确认：{reason}",
        steps=[{"step": "角色门控", "confidence": confidence, "oversized": oversized}],
        sources=[{"file_id": file_id, "sheet_id": sheet_id, "location": "整表",
                  "confidence": confidence}],
    )
    audit_log.record_audit(
        conn, project_id, "system", "route_role_review", f"sheet:{sheet_id}",
        None, {"sheet": sheet_name, "confidence": confidence, "oversized": oversized},
        f"角色审阅路由：{reason}；保留 raw 网格，未进入结算模型")


def _route_form_sheet(conn: sqlite3.Connection, project_id: int, file_id: int,
                      sheet_id: int, sheet_name: str,
                      cells: dict[tuple[int, int], str]) -> None:
    """键值对表单 → 待人工确认的证据候选（仅通用 evidence 表 + 审计）。

    纪律（监督第六轮）：
    - 不写 settlement_period / line_items / period_totals（防污染结算模型）；
    - 不写 contract_docs / contract_facts（合同模块专用，避免虚假合同风险）；
    - 每条候选 = 一条 evidence（kind='form_field_candidate'，sources 含
      file_id/sheet_id/行列/原文，summary 明确待人工确认）+ audit 留痕；
    - 不新建数据库迁移。
    """
    import re

    from costguard.core.evidence import audit as audit_log
    from costguard.core.evidence import evidence as evidence_api

    kv_pairs = []
    max_col = max(c for _r, c in cells) if cells else 0
    for (row, col), text in sorted(cells.items()):
        t = (text or "").strip()
        m = re.match(r"^(.{2,24}?)[：:]\s*(.+)$", t)
        if m and not any(w in t for w in ("清单编码", "清单名称", "合价")):
            kv_pairs.append((m.group(1).strip(), m.group(2).strip(), row, col, t))
            continue
        # 键列 + 同行右侧第一个非空格作为值（间隔布局）。
        # 标签先去掉尾部冒号再匹配后缀——"申请单位名称："不能因冒号而漏掉。
        label = t.rstrip("：:")
        if t and len(label) <= 24 and not any(
            w in t for w in ("清单编码", "清单名称", "合价")
        ) and (label.endswith("名称") or label.endswith("行") or label.endswith("编号")
               or label.endswith("信息") or label.endswith("金额")):
            value_col, nxt = next(
                ((c2, cells.get((row, c2), "").strip())
                 for c2 in range(col + 1, max_col + 1)
                 if cells.get((row, c2), "").strip()),
                (None, ""))
            if value_col is not None and nxt:
                # 位置记真实值列（可反向定位到原格），不是 col+1
                kv_pairs.append((label, nxt, row, value_col, f"{t} {nxt}"))
    for key, value, row, col, quote in kv_pairs[:40]:
        evidence_api.add_evidence(
            conn, project_id, "form_field_candidate",
            f"表单字段候选（待人工确认）：{key} = {value[:60]}",
            steps=[{"step": "表单路由", "sheet": sheet_name, "status": "待人工确认"}],
            sources=[{"file_id": file_id, "sheet_id": sheet_id,
                      "location": f"行{row}列{col}", "quote": quote[:120]}],
        )
    audit_log.record_audit(
        conn, project_id, "system", "route_non_settlement_form", f"sheet:{sheet_id}",
        None, {"sheet": sheet_name, "n_candidates": len(kv_pairs[:40])},
        "键值对表单路由：保留原 Sheet，字段候选存证据表待人工复核，未进入结算与合同模型")


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
        fallback = file_period or next_period_no(conn, project_id, direction)
        return ImportReport(sf.file_id, None, fallback, -1, "failed", message=result.error)

    from costguard.core.parsing.excel_parser import persist_parse_result

    batch_id = persist_parse_result(conn, sf.file_id, result)

    report = ImportReport(sf.file_id, batch_id, file_period or 0, -1, "partial")
    parsed_any = False
    form_routed = False
    role_gated = False
    period_ids: set[int] = set()
    # 文件级大表护栏：任一 sheet 超长（汇总/台账/核销典型特征）→ 整文件需角色审阅
    oversized_file = any(sh.n_rows > ROW_GUARD for sh in result.sheets)
    for sheet in result.sheets:
        sheet_id = conn.execute(
            "SELECT id FROM raw_sheets WHERE batch_id=? AND sheet_index=?", (batch_id, sheet.sheet_index)
        ).fetchone()["id"]
        cells, merged, n_rows, _ = load_sheet_grid(conn, sheet_id)
        det = detect_header(sheet.sheet_index, cells, merged, n_rows, sheet.n_cols)
        from costguard.core.parsing.header_detect import detect_form_like

        # ---- sheet 级写入闸门（监督第八轮）----
        form_level = detect_form_like(cells, merged)
        form_like = form_level == "strong" or (det is None and form_level is not None)
        if form_like:
            # 键值对表单结构优先于弱清单识别（含 det 非 None 的误命中页）
            _route_form_sheet(conn, project_id, sf.file_id, sheet_id, sheet.sheet_name, cells)
            report.sheets.append(SheetReport(
                sheet.sheet_name, "non_settlement_form",
                notes=["检测为键值对表单（非结算清单）：已按待人工复核的表单字段候选记录，"
                       "保留原 Sheet 与单元格；请人工确认后使用通用 evidence 人工复核入口"]))
            form_routed = True
            continue
        no_quantity_column = det is not None and "quantity" not in det.col_map
        if oversized_file or no_quantity_column or (det is not None and det.needs_review
                              and det.confidence < CONF_WRITE_GATE):
            # 需角色审阅：不写 canonical tables，保留 raw 网格与候选证据
            _route_role_review(conn, project_id, sf.file_id, sheet_id, sheet.sheet_name,
                               det.confidence if det else None, oversized_file)
            report.sheets.append(SheetReport(
                sheet.sheet_name, "needs_role_review",
                notes=["表头识别不可靠或超长汇总/台账型结构：未经人工确认角色前"
                       "不写入结算模型；字段候选已存 evidence，请人工选择角色"]))
            role_gated = True
            continue
        if det is None:
            report.sheets.append(SheetReport(sheet.sheet_name, "no_header"))
            continue
        items = extract_items.extract_items(cells, merged, det, n_rows)
        if not items:
            report.sheets.append(SheetReport(sheet.sheet_name, "no_header", notes=["header-like but 0 data rows"]))
            continue

        # ---- 逐 Sheet 期次判定 ----
        # 优先级：用户显式指定 > 文件名期号 > Sheet 名期号 > 递增。
        # 递增按导入 direction 独立（对上/对下各自有序）；文件名与 Sheet 名期号
        # 冲突时按文件名（导入语境更明确）并提示复核。
        sheet_pno = guess_period_no_from_text(sheet.sheet_name) or guess_period_no_from_text(
            " ".join(t for (r, _c), t in sorted(cells.items()) if r <= 5)
        )
        notes: list[str] = []
        if used_increment:
            pno = (next_period_no(conn, project_id, direction) if parsed_any
                   else (file_period or sheet_pno or next_period_no(conn, project_id, direction)))
        elif file_period is not None:
            pno = file_period
            if sheet_pno is not None and sheet_pno != file_period:
                notes.append(f"Sheet 名期号({sheet_pno})与文件名期号({file_period})不一致，按文件名处理，请复核")
        else:
            pno = sheet_pno if sheet_pno is not None else next_period_no(conn, project_id, direction)
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
    if parsed_any:
        report.status = "ok"
    elif form_routed and not role_gated:
        # 纯表单：不是结算解析成功，也不是普通失败——待人工复核
        report.status = "partial"
        report.needs_manual_review = True
        report.message = "non_settlement_form_needs_manual_review"
    elif role_gated or form_routed:
        # 存在需角色审阅/表单路由 sheet 且无 canonical：partial + 待人工
        report.status = "partial"
        report.needs_manual_review = True
        report.message = ("non_settlement_spreadsheet_needs_role_review" if role_gated
                          else "non_settlement_form_needs_manual_review")
    else:
        report.status = "failed"
        report.message = "no sheets parsed"
    return report
