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
from costguard.core.parsing.header_detect import HeaderDetection, detect_header


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

# sheet 级写入闸门（监督第九轮）：
# - det.needs_review 一律不写 canonical（歧义/低置信未经人工确认不得进入结算模型）；
# - sheet 名/document 语义门控：汇总/核销/台账类 sheet 即使强表头也需角色确认；
# - 无行数护栏（600 行合法大结算必须解析，判断不按大小）。
SUMMARY_LIKE_PATTERN = re.compile(r"汇总|核销|台账|summary|reconciliation|ledger", re.IGNORECASE)


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
                       confidence: float | None, oversized: bool,
                       reason: str | None = None) -> None:
    """需角色审阅 sheet：留 evidence 候选与审计，不写 canonical tables。"""
    from costguard.core.evidence import audit as audit_log
    from costguard.core.evidence import evidence as evidence_api

    reason = reason or ("超长汇总/台账/核销型结构，疑似非结算清单"
                        if oversized else f"表头识别置信度不足（{confidence}）")
    evidence_api.add_evidence(
        conn, project_id, "sheet_role_candidate",
        f"Sheet「{sheet_name}」待人工角色确认：{reason}",
        steps=[{"step": "角色门控", "confidence": confidence, "reason": reason}],
        sources=[{"file_id": file_id, "sheet_id": sheet_id, "location": "整表",
                  "confidence": confidence}],
    )
    audit_log.record_audit(
        conn, project_id, "system", "route_role_review", f"sheet:{sheet_id}",
        None, {"sheet": sheet_name, "confidence": confidence, "oversized": oversized},
        f"角色审阅路由：{reason}；保留 raw 网格，未进入结算模型")


def _validate_confirmed_col_map(col_map: dict, max_col: int) -> None:
    """校验人工确认列映射。

    支持两类真实结算结构：
    - 工程量计价：name + quantity + unit_price；
    - 金额计价/比例计价：name + amount（工程量可以真实缺失，绝不补 0）。
    """
    if "name" not in col_map:
        raise ValueError("确认列映射缺少必需字段: name")
    amount_path = "amount" in col_map
    quantity_price_path = "quantity" in col_map and "unit_price" in col_map
    if not amount_path and not quantity_price_path:
        raise ValueError(
            "确认列映射缺少必需的完整金额口径：需 amount，或同时提供 quantity + unit_price")
    cols = list(col_map.values())
    if len(cols) != len(set(cols)):
        raise ValueError("确认列映射存在冲突：同一列被映射到多个字段")
    if any(not isinstance(c, int) or c < 1 or c > max_col for c in cols):
        raise ValueError(f"确认列映射列号越界（有效范围 1..{max_col}）")


def confirm_sheet_role_and_extract(conn: sqlite3.Connection, project_id: int,
                                   sheet_id: int, actor: str, reason: str,
                                   direction: str = "unknown",
                                   confirmed_col_map: dict | None = None,
                                   confirmed_header_range: tuple[int, int] | None = None,
                                   confirmed_data_range: tuple[int, int] | None = None,
                                   period_no: int | None = None) -> int:
    """人工确认被门控 sheet 的角色为结算清单后，重放语义层抽取（ADR-008）。

    - reason 必填（原则 14）；
    - det.needs_review（歧义/低置信）时必须显式传 confirmed_col_map（人工真正
      选择列），未传即拒绝；无歧义 sheet（仅角色语义被挡）可仅确认角色；
    - 审计记录 old（检测到的映射）/new（人工确认的映射）；
    - 校验：必需字段、列号存在且不冲突。
    """
    from dataclasses import replace

    from costguard.core.evidence import audit as audit_log
    from costguard.core.evidence import evidence as evidence_api

    if not reason or not reason.strip():
        raise audit_log.AuditReasonRequiredError("人工确认角色必须记录原因（原则 14）")
    cells, merged, n_rows, n_cols = load_sheet_grid(conn, sheet_id)
    meta = conn.execute(
        """SELECT rs.sheet_name, sf.id AS file_id, sf.original_name
           FROM raw_sheets rs JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE rs.id=? AND sf.project_id=?""",
        (sheet_id, project_id),
    ).fetchone()
    if not meta:
        raise ValueError(f"sheet {sheet_id} 不属于 project {project_id}")
    sheet_name = meta["sheet_name"]
    det = detect_header(0, cells, merged, n_rows, n_cols)
    if det is None:
        if confirmed_col_map is None or confirmed_header_range is None:
            raise ValueError(
                "该 sheet 无可识别表头，需同时提供人工列映射与表头范围后才能按清单抽取")
        det = HeaderDetection(
            sheet_index=0,
            header_row_lo=confirmed_header_range[0],
            header_row_hi=confirmed_header_range[1],
            col_map=dict(confirmed_col_map),
            confidence=0.0,
            needs_review=True,
            notes=["表头由人工确认"],
        )

    if det.needs_review and confirmed_col_map is None:
        raise ValueError(
            "该 sheet 表头存在歧义（needs_review），必须显式传入人工确认的列映射 "
            "confirmed_col_map 后才能写入结算模型")

    used_map = dict(det.col_map)
    if confirmed_col_map is not None:
        _validate_confirmed_col_map(confirmed_col_map, n_cols)
        used_map = dict(confirmed_col_map)

    header_lo, header_hi = det.header_row_lo, det.header_row_hi
    if confirmed_header_range is not None:
        header_lo, header_hi = confirmed_header_range
        if not (isinstance(header_lo, int) and isinstance(header_hi, int)
                and 1 <= header_lo <= header_hi <= n_rows):
            raise ValueError(f"确认表头范围无效（有效行 1..{n_rows}）")
    if confirmed_data_range is not None:
        data_start, data_end = confirmed_data_range
        if not (isinstance(data_start, int) and isinstance(data_end, int)
                and header_hi < data_start <= data_end <= n_rows):
            raise ValueError(
                f"确认数据行范围无效：应在表头末行 {header_hi} 之后且不超过 {n_rows}")
        data_range = (data_start, data_end)
    else:
        from costguard.core.parsing.header_detect import data_rows_range

        data_range = data_rows_range(
            cells, replace(det, header_row_lo=header_lo, header_row_hi=header_hi), n_rows)

    # 幂等/重复确认拒绝：已回写 period_id 的 sheet 视为已完成
    already = conn.execute(
        "SELECT period_id FROM raw_sheets WHERE id=?", (sheet_id,)).fetchone()
    if already and already["period_id"] is not None:
        raise ValueError(
            f"该 sheet 已完成确认抽取（period_id={already['period_id']}），不得重复确认")

    # 先抽取（items 非空才建 period，避免失败留空期次）
    det_used = replace(det, header_row_lo=header_lo, header_row_hi=header_hi,
                       col_map=used_map, needs_review=False)
    items = extract_items.extract_items(cells, merged, det_used, n_rows, data_range=data_range)
    if not items:
        raise ValueError("确认后抽取 0 行：请核对人工列映射（未创建期次）")

    audit_log.record_audit(
        conn, project_id, actor, "confirm_sheet_role", f"sheet:{sheet_id}",
        {"role": "gated", "needs_review": det.needs_review,
         "mapping": {"detected": det.col_map}},
        {"role": "settlement", "confidence": det.confidence,
         "mapping": {"detected": det.col_map, "confirmed": used_map},
         "header_range": [header_lo, header_hi], "data_range": list(data_range),
         "period_no": period_no}, reason)
    pno = period_no if period_no is not None else next_period_no(conn, project_id, direction)
    if not isinstance(pno, int) or pno < 1:
        raise ValueError("确认期次必须为正整数")
    period_id = ensure_period(
        conn, project_id, pno, f"{meta['original_name']}/{sheet_name}", meta["file_id"],
                              direction=direction)
    n = extract_items.persist_line_items(conn, period_id, sheet_id, items)
    # 回写：sheet→期次关联 + 已确认列映射（needs_review 归零，保持证据链）
    with conn:
        conn.execute("UPDATE raw_sheets SET period_id=? WHERE id=?",
                     (period_id, sheet_id))
        conn.execute("DELETE FROM table_headers WHERE sheet_id=?", (sheet_id,))
        conn.execute(
            """INSERT INTO table_headers(sheet_id, header_row_lo, header_row_hi,
               col_map_json, confidence, needs_review, data_row_start, data_row_end)
               VALUES (?,?,?,?,?,0,?,?)""",
            (sheet_id, header_lo, header_hi, json.dumps(used_map), det.confidence,
             data_range[0], data_range[1]))
    evidence_api.add_evidence(
        conn, project_id, "sheet_role_confirmed",
        f"Sheet「{sheet_name}」经人工确认为结算清单，重放抽取 {n} 行",
        steps=[{"step": "人工确认", "actor": actor, "reason": reason,
                "mapping": {"detected": det.col_map, "confirmed": used_map},
                "header_range": [header_lo, header_hi], "data_range": list(data_range),
                "period_no": pno, "confidence": det.confidence}],
        sources=[{"sheet_id": sheet_id, "period_id": period_id, "n_items": n}],
    )
    return n


NON_SETTLEMENT_ROLES = {
    "non_settlement_form",
    "settlement_summary",
    "supporting_evidence",
    "contract_control",
    "other_non_settlement",
}


def confirm_sheet_non_settlement_role(
    conn: sqlite3.Connection,
    project_id: int,
    sheet_id: int,
    actor: str,
    confirmed_role: str,
    reason: str,
) -> None:
    """人工确认被门控 sheet 仅作表单/汇总/控制证据，不进入结算模型。"""
    from costguard.core.evidence import audit as audit_log
    from costguard.core.evidence import evidence as evidence_api

    if not reason or not reason.strip():
        raise audit_log.AuditReasonRequiredError("人工确认角色必须记录原因（原则 14）")
    if confirmed_role not in NON_SETTLEMENT_ROLES:
        raise ValueError(
            f"不支持的非结算角色 {confirmed_role!r}；允许值: {sorted(NON_SETTLEMENT_ROLES)}")
    meta = conn.execute(
        """SELECT rs.sheet_name, rs.period_id, sf.id AS file_id
           FROM raw_sheets rs JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE rs.id=? AND sf.project_id=?""",
        (sheet_id, project_id),
    ).fetchone()
    if not meta:
        raise ValueError(f"sheet {sheet_id} 不属于 project {project_id}")
    if meta["period_id"] is not None:
        raise ValueError("该 sheet 已进入结算模型，不能再确认成非结算角色")
    duplicate = conn.execute(
        """SELECT id FROM audit_log WHERE project_id=? AND target=?
           AND action='confirm_sheet_non_settlement_role' LIMIT 1""",
        (project_id, f"sheet:{sheet_id}"),
    ).fetchone()
    if duplicate:
        raise ValueError("该 sheet 的非结算角色已确认，不得重复确认")

    audit_log.record_audit(
        conn, project_id, actor, "confirm_sheet_non_settlement_role", f"sheet:{sheet_id}",
        {"role": "gated"}, {"role": confirmed_role}, reason,
    )
    evidence_api.add_evidence(
        conn, project_id, "sheet_role_confirmed",
        f"Sheet「{meta['sheet_name']}」经人工确认为 {confirmed_role}，仅作证据，不进入结算模型",
        steps=[{"step": "人工确认非结算角色", "actor": actor,
                "role": confirmed_role, "reason": reason}],
        sources=[{"file_id": meta["file_id"], "sheet_id": sheet_id, "location": "整表"}],
    )


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
    # 文档级语义门控：文件名含汇总/核销/台账语义 → 整文件需角色审阅
    # （copy 文件名是用户/验收语境的真实命名，非 test_id 硬编码）
    doc_summary_like = bool(SUMMARY_LIKE_PATTERN.search(src.stem))
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
        # 语义门控：sheet 名含汇总/核销/台账 → 无论表头多强都需角色确认
        summary_like = bool(SUMMARY_LIKE_PATTERN.search(sheet.sheet_name))
        # needs_review 一律挡（监督第九轮）：歧义/低置信未经人工确认不得写 canonical
        needs_review_gate = det is not None and det.needs_review
        no_quantity_column = det is not None and "quantity" not in det.col_map
        if doc_summary_like or summary_like or needs_review_gate or no_quantity_column:
            reason = ("文档文件名含汇总/核销/台账语义，整文件需角色确认" if doc_summary_like else
                      "sheet 名含汇总/核销/台账语义" if summary_like else
                      "表头存在歧义或识别不可靠（needs_review）" if needs_review_gate else
                      "未识别到数量（计量）列，结算清单结构不完整")
            _route_role_review(conn, project_id, sf.file_id, sheet_id, sheet.sheet_name,
                               det.confidence if det else None, False, reason)
            report.sheets.append(SheetReport(
                sheet.sheet_name, "needs_role_review",
                notes=[f"{reason}：未经人工确认角色前不写入结算模型；"
                       "字段候选已存 evidence，请人工选择角色"
                       "（confirm_sheet_role_and_extract）"]))
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
    has_pending = role_gated or form_routed
    if parsed_any and not has_pending:
        report.status = "ok"
    elif parsed_any:
        # 有 canonical 但仍有被挡 sheet：overall=partial，pending 不得被 full_pipeline 掩盖
        report.status = "partial"
        report.needs_manual_review = True
        report.message = "settlement_parsed_with_pending_role_review"
    elif form_routed and not role_gated:
        report.status = "partial"
        report.needs_manual_review = True
        report.message = "non_settlement_form_needs_manual_review"
    elif role_gated or form_routed:
        report.status = "partial"
        report.needs_manual_review = True
        report.message = ("non_settlement_spreadsheet_needs_role_review" if role_gated
                          else "non_settlement_form_needs_manual_review")
    else:
        report.status = "failed"
        report.message = "no sheets parsed"
    return report
