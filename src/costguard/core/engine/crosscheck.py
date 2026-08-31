"""双向校核（Phase 3 核心）。

A 路径：各期 line_items 金额直接累计（数据库已清洗值）。
B 路径：从 raw_cells 原始网格重新抽取、重新计算（完全独立复算）。
C 校验：明细合计 vs 原表自带"小计"行。

差异必须落库并生成证据；差异不做任何"调平"。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from costguard.core.anomalies import coverage as detection_coverage
from costguard.core.contracts import run_contract
from costguard.core.engine.money import (
    NotANumberError,
    money_mul,
    round2,  # noqa: F401  (money 入口纪律：保留显式依赖)
    to_decimal,
)
from costguard.core.engine.settlement_io import load_sheet_grid, pending_sheet_count
from costguard.core.labels import direction_label
from costguard.core.parsing import extract_items
from costguard.core.parsing.header_detect import HeaderDetection, detect_header

D = Decimal

TOL = D("0.02")  # 两条独立路径各保留两位，容差 0.02 仅用于分级报告


class AmbiguousPeriodError(ValueError):
    """同一期号存在多个方向（或不存在）且调用方未声明 direction。

    明确拒绝，绝不静默选择任意方向（监督指令一.3）。
    """


@dataclass
class CheckResult:
    period_id: int
    period_no: int
    direction: str
    path_a_total: Decimal | None  # 直接累计
    path_b_total: Decimal | None  # 网格重算
    raw_subtotal: Decimal | None  # 原表小计行
    diff_ab: Decimal | None
    status: str  # 'match' | 'diff' | 'incomplete'
    control_diff: Decimal | None = None  # A 路径与 C 原表控制额之差
    control_status: str = "not_available"  # 'match' | 'diff' | 'not_available'
    missing_rows: int = 0
    notes: list[str] = field(default_factory=list)
    path_a_raw_total: Decimal | None = None
    path_a_calculated_total: Decimal | None = None
    path_a_calculated_used_total: Decimal | None = None
    path_b_raw_total: Decimal | None = None
    path_b_calculated_total: Decimal | None = None
    path_b_calculated_used_total: Decimal | None = None
    amount_source: str = "missing"  # 'raw' | 'calculated' | 'mixed' | 'missing'
    amount_status: str = "missing"  # 'raw' | 'calculated' | 'raw_checked_match' | 'raw_checked_diff'
    derived_rows: int = 0
    formula_mismatch_rows: int = 0
    verification_level: str = "insufficient"  # 'sufficient' | 'findings' | 'insufficient'
    detail_rows: int = 0          # 参与累计明细行数（排除小计行）
    excluded_subtotal_rows: int = 0
    excluded_title_rows: int = 0
    pending_sheets: int = 0       # 项目级待人工确认工作表数
    range_unproven_sheets: int = 0  # 取数范围无法证明完整的工作表数


@dataclass
class _AmountSummary:
    total: Decimal | None = None
    raw_total: Decimal | None = None
    calculated_total: Decimal | None = None
    calculated_used_total: Decimal | None = None
    missing_rows: int = 0
    derived_rows: int = 0
    formula_mismatch_rows: int = 0
    amount_source: str = "missing"
    amount_status: str = "missing"
    amount_check_status: str = "not_available"


def _sum_amounts(rows) -> tuple[Decimal | None, int]:
    total: Decimal | None = None
    missing = 0
    for r in rows:
        if r["amount"] is None or r["amount"] == "":
            missing += 1
            continue
        try:
            v = to_decimal(r["amount"])
        except NotANumberError:
            missing += 1
            continue
        total = v if total is None else total + v
    return total, missing


def _dec_or_none(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return to_decimal(value)
    except NotANumberError:
        return None


def _merge_source(current: str, incoming: str) -> str:
    if incoming == "missing":
        return current
    if current in ("missing", incoming):
        return incoming
    return "mixed"


def _merge_check_status(current: str, incoming: str) -> str:
    if incoming == "diff" or current == "diff":
        return "diff"
    if incoming == "match" or current == "match":
        return "match"
    return "not_available"


def _assess_amount(row) -> tuple[Decimal | None, Decimal | None, Decimal | None, str, str]:
    """返回 raw、calculated、effective、source、公式校验状态。"""
    raw = _dec_or_none(row["amount"])
    quantity = _dec_or_none(row["quantity"])
    unit_price = _dec_or_none(row["unit_price"])
    calculated = money_mul(quantity, unit_price) if quantity is not None and unit_price is not None else None
    if raw is not None:
        effective = raw
        source = "raw"
    elif calculated is not None:
        effective = calculated
        source = "calculated"
    else:
        effective = None
        source = "missing"
    if raw is not None and calculated is not None:
        check_status = "match" if abs(raw - calculated) <= TOL else "diff"
    else:
        check_status = "not_available"
    return raw, calculated, effective, source, check_status


def _sum_line_item_amounts(rows) -> _AmountSummary:
    """按原始金额优先、缺失时数量×单价回退，汇总并保留来源统计。"""
    summary = _AmountSummary()
    for row in rows:
        raw, calculated, effective, source, check_status = _assess_amount(row)
        if raw is None:
            summary.missing_rows += 1
        if source == "calculated":
            summary.derived_rows += 1
        if check_status == "diff":
            summary.formula_mismatch_rows += 1
        if effective is not None:
            summary.total = effective if summary.total is None else summary.total + effective
            summary.amount_source = _merge_source(summary.amount_source, source)
        if raw is not None:
            summary.raw_total = raw if summary.raw_total is None else summary.raw_total + raw
        if calculated is not None:
            summary.calculated_total = (
                calculated if summary.calculated_total is None
                else summary.calculated_total + calculated
            )
        if source == "calculated":
            summary.calculated_used_total = (
                calculated if summary.calculated_used_total is None
                else summary.calculated_used_total + calculated
            )
        summary.amount_check_status = _merge_check_status(
            summary.amount_check_status, check_status
        )
    if summary.amount_source == "mixed":
        summary.amount_status = "mixed"
    elif summary.amount_source in ("raw", "calculated"):
        summary.amount_status = summary.amount_source
    else:
        summary.amount_status = "missing"
    return summary


def _merge_summaries(target: _AmountSummary, incoming: _AmountSummary) -> None:
    """合并多个 sheet/路径的金额统计，不把缺失值当作零。"""
    for field_name in ("total", "raw_total", "calculated_total", "calculated_used_total"):
        current = getattr(target, field_name)
        value = getattr(incoming, field_name)
        if value is not None:
            setattr(target, field_name, value if current is None else current + value)
    target.missing_rows += incoming.missing_rows
    target.derived_rows += incoming.derived_rows
    target.formula_mismatch_rows += incoming.formula_mismatch_rows
    target.amount_source = _merge_source(target.amount_source, incoming.amount_source)
    target.amount_check_status = _merge_check_status(
        target.amount_check_status, incoming.amount_check_status
    )
    if target.amount_source == "mixed":
        target.amount_status = "mixed"
    elif target.amount_source in ("raw", "calculated"):
        target.amount_status = target.amount_source
    else:
        target.amount_status = "missing"


def _range_is_proven(
    cells: dict[tuple[int, int], str],
    max_row: int,
    det: HeaderDetection,
    confirmed: sqlite3.Row | None,
) -> bool:
    """判断 B 路径使用的取数范围是否有可复核的完整性依据。

    - ``manual_confirmation`` 表示用户明确给出边界；
    - ``last_non_empty_row`` 只在原始网格没有新增的非空行时视为可复核；
    - 历史 NULL/unknown 或范围越界一律不充分，不能因为 A/B/C 数字碰巧相等
      就显示绿色。
    """
    if confirmed is None:
        return False
    start = confirmed["data_row_start"]
    end = confirmed["data_row_end"]
    status = confirmed["data_range_status"] or "unproven"
    method = confirmed["data_range_method"] or "unknown"
    if start is None or end is None:
        return False
    start, end = int(start), int(end)
    if start <= int(det.header_row_hi) or end < start or end > int(max_row):
        return False
    if status == "confirmed" and method == "manual_confirmation":
        return True
    # 隐藏行/列可能包含未参与抽取的金额或明细。自动推断不能把“最后非空行”
    # 当作完整范围；只有人工明确确认过边界后才允许继续。
    try:
        hidden_rows = json.loads(confirmed["hidden_rows_json"] or "[]")
        hidden_cols = json.loads(confirmed["hidden_cols_json"] or "[]")
    except (TypeError, json.JSONDecodeError, KeyError):
        hidden_rows, hidden_cols = [], []
    if (hidden_rows or hidden_cols) and not (
        status == "confirmed" and method == "manual_confirmation"
    ):
        return False
    if status != "inferred" or method != "last_non_empty_row":
        return False
    non_empty_rows = {
        row for (row, _col), value in cells.items()
        if start <= row <= max_row and value not in (None, "")
    }
    # 自动推断的边界必须仍覆盖当前原始网格中表头之后的最后非空行；
    # 若原始网格后来新增一行/发生截断，旧结果立即降为不充分。
    return bool(non_empty_rows) and max(non_empty_rows) == end


def check_period(conn: sqlite3.Connection, period_id: int) -> CheckResult:
    """对单期做 A/B/C 三重校核。全部查询以 period_id 锁定，
    A/B/C 三路径、异常与证据都落在同一期次上。"""
    meta = conn.execute(
        "SELECT period_no, direction, project_id FROM settlement_periods WHERE id=?",
        (period_id,),
    ).fetchone()
    if not meta:
        raise ValueError(f"period {period_id} not found")
    period_no, direction = int(meta["period_no"]), meta["direction"]
    project_id = int(meta["project_id"])

    # A：直接累计清洗后的明细；原始合价缺失时仅在计算路径中回退到数量×单价。
    rows = conn.execute(
        """SELECT li.quantity, li.unit_price, li.amount, li.flags_json FROM line_items li
           WHERE li.period_id=?""",
        (period_id,),
    ).fetchall()
    detail_rows = [r for r in rows if not json.loads(r["flags_json"] or "{}").get("subtotal")]
    summary_a = _sum_line_item_amounts(detail_rows)
    total_a = summary_a.total

    # C：原表小计行金额。控制值只认"合计级"行（合计/总计/累计）——真实分页
    # 结算书"本页小计"与"合计"并存，全部求和会把控制值翻倍（v0.1.5 已知限制）。
    # 存在合计级行时必须恰好一条且金额可解析；否则 C 不可用并留注，
    # 不擅自挑行、求和或回退到页级小计。无合计级行时，页级小计也必须
    # 全部可解析后才可互斥求和。
    subtotal_rows = [r for r in rows if json.loads(r["flags_json"] or "{}").get("subtotal")]
    grand_rows = [
        r for r in subtotal_rows
        if json.loads(r["flags_json"] or "{}").get("grand_total")
    ]
    control_note = None
    if grand_rows:
        # 合计级行一旦存在，就必须恰好只有一条且金额可解析；无效行不能
        # 被忽略后回退到页级小计，否则同一张表会因脏控制行假绿色。
        if len(grand_rows) != 1:
            raw_subtotal = None
            control_note = (
                f"{len(grand_rows)} 个合计级行，C 控制值不唯一，"
                "请人工核对原表（未擅自挑行或求和）"
            )
        else:
            raw_subtotal = _dec_or_none(grand_rows[0]["amount"])
            if raw_subtotal is None:
                control_note = "唯一合计级行金额缺失或非法，C 控制不可用"
    elif subtotal_rows:
        # 无合计级行时，页级小计按页互斥求和；任一页级小计无效时，
        # 不能只累计其余可解析值形成不完整的 C 控制值。
        subtotal_values = [_dec_or_none(row["amount"]) for row in subtotal_rows]
        if any(value is None for value in subtotal_values):
            raw_subtotal = None
            control_note = "无合计级行且页级小计金额缺失或非法，C 控制不可用"
        else:
            raw_subtotal = sum(subtotal_values, D("0"))
            control_note = f"无合计级行，C 控制值={len(subtotal_rows)} 个小计行之和"
    else:
        raw_subtotal = None
        control_note = "无带金额的小计/合计行，C 控制不可用"

    # B：从原始网格独立重算（不复用 line_items）
    sheet_ids = conn.execute(
        "SELECT id FROM raw_sheets rs WHERE rs.period_id=?",
        (period_id,),
    ).fetchall()
    summary_b = _AmountSummary()
    excluded_title_rows = 0
    range_unproven_sheets = 0
    for sheet_row in sheet_ids:
        sheet_id = int(sheet_row["id"])
        cells, merged, n_rows, _n_cols = load_sheet_grid(conn, sheet_id)
        confirmed = conn.execute(
            """SELECT header_row_lo, header_row_hi, col_map_json, confidence,
                      needs_review, data_row_start, data_row_end,
                      data_range_status, data_range_method,
                      rs.hidden_rows_json, rs.hidden_cols_json
               FROM table_headers th JOIN raw_sheets rs ON rs.id=th.sheet_id
               WHERE th.sheet_id=?""",
            (sheet_id,),
        ).fetchone()
        data_range = None
        if confirmed is not None:
            det = HeaderDetection(
                sheet_index=0,
                header_row_lo=int(confirmed["header_row_lo"]),
                header_row_hi=int(confirmed["header_row_hi"]),
                col_map=json.loads(confirmed["col_map_json"]),
                confidence=float(confirmed["confidence"]),
                needs_review=bool(confirmed["needs_review"]),
            )
            if confirmed["data_row_start"] is not None and confirmed["data_row_end"] is not None:
                data_range = (int(confirmed["data_row_start"]), int(confirmed["data_row_end"]))
        else:
            det = detect_header(
                0, cells, merged, n_rows, max((c for _, c in cells), default=0))
        if det is None or not _range_is_proven(cells, n_rows, det, confirmed):
            range_unproven_sheets += 1
        if det is None:
            continue
        _skip_stats: dict = {}
        items = extract_items.extract_items(cells, merged, det, n_rows,
                                            data_range=data_range, stats=_skip_stats)
        excluded_title_rows += _skip_stats.get("title_rows", 0)
        rows_b = []
        for it in items:
            if it.flags.get("subtotal") or it.flags.get("title_row"):
                continue
            rows_b.append(
                {
                    "quantity": it.quantity.value if it.quantity else None,
                    "unit_price": it.unit_price.value if it.unit_price else None,
                    "amount": it.amount.value if it.amount else None,
                }
            )
        _merge_summaries(summary_b, _sum_line_item_amounts(rows_b))

    total_b = summary_b.total
    amount_source = _merge_source(summary_a.amount_source, summary_b.amount_source)
    # A/B 代表同一批明细的两条路径；来源统计按较大值计，避免同一行在两条
    # 路径中被重复计数。missing_rows 仍保留原有的路径级计数口径。
    formula_mismatch_rows = max(
        summary_a.formula_mismatch_rows, summary_b.formula_mismatch_rows
    )
    derived_rows = max(summary_a.derived_rows, summary_b.derived_rows)
    if formula_mismatch_rows:
        amount_status = (
            "raw_checked_diff" if amount_source == "raw" else "mixed_checked_diff"
        )
    elif amount_source == "raw" and (
        summary_a.amount_check_status == "match" or summary_b.amount_check_status == "match"
    ):
        amount_status = "raw_checked_match"
    else:
        amount_status = amount_source

    notes: list[str] = []
    missing_raw_rows = max(summary_a.missing_rows, summary_b.missing_rows)
    if missing_raw_rows:
        notes.append(
            f"{missing_raw_rows}行原始金额缺失或不可解析，"
            f"{('A/B路径均按数量×单价计算并保留来源' if derived_rows else '候选金额未能完整解析')}"
        )
    if formula_mismatch_rows:
        notes.append(
            f"{formula_mismatch_rows}行原始金额与数量×单价计算值存在差异，未调平"
        )

    status = "match"
    diff_ab = None
    if total_a is None or total_b is None:
        status = "incomplete"
        if summary_a.missing_rows or summary_b.missing_rows:
            status = "incomplete"
    else:
        diff_ab = total_a - total_b
        status = "diff" if formula_mismatch_rows else ("match" if abs(diff_ab) <= TOL else "diff")

    # C 是源表控制值，不是 A/B 独立复算的一部分。单独记录，避免 A/B 一致时
    # 把尚未解释的源表差异误写成“全部通过”。
    control_diff = None
    control_status = "not_available"
    if total_a is not None and raw_subtotal is not None:
        control_diff = total_a - raw_subtotal
        control_status = "match" if abs(control_diff) <= TOL else "diff"
        if control_status == "diff":
            notes.append(f"A路径与C原表控制额差异{control_diff}，保持待复核，未调平")
    if control_note:
        notes.append(control_note)

    # ---- 校核级别（P0-2/P0-5）：A/B 一致不等于结算正确 ----
    # 绿色（校核充分）仅当：A=B 且 C 控制可用且控制一致，且项目无待人工工作表。
    pending_sheets = pending_sheet_count(conn, project_id)
    detail_count = len(detail_rows)
    reasons: list[str] = []
    if pending_sheets:
        reasons.append(f"{pending_sheets} 张工作表待人工确认")
    if range_unproven_sheets:
        reasons.append(f"{range_unproven_sheets} 张工作表取数范围完整性无法证明")
    if missing_raw_rows:
        reasons.append("原始金额存在缺失或不可解析")
    if control_status == "not_available":
        reasons.append("C 控制不可用")
    if status == "incomplete":
        reasons.append("明细数据不完整")
    if reasons or status == "incomplete":
        verification_level = "insufficient"
        notes.append("校核不充分：" + "；".join(reasons))
    elif status == "diff" or control_status == "diff":
        verification_level = "findings"
        notes.append("校核有发现：A/B 或 C 控制存在差异，保持待复核，未调平")
    else:
        verification_level = "sufficient"

    return CheckResult(
        period_id=period_id, period_no=period_no, direction=direction,
        path_a_total=total_a, path_b_total=total_b, raw_subtotal=raw_subtotal,
        diff_ab=diff_ab, status=status,
        control_diff=control_diff, control_status=control_status,
        missing_rows=summary_a.missing_rows + summary_b.missing_rows,
        verification_level=verification_level,
        detail_rows=detail_count,
        excluded_subtotal_rows=len(subtotal_rows),
        excluded_title_rows=excluded_title_rows,
        pending_sheets=pending_sheets,
        range_unproven_sheets=range_unproven_sheets,
        notes=notes,
        path_a_raw_total=summary_a.raw_total,
        path_a_calculated_total=summary_a.calculated_total,
        path_a_calculated_used_total=summary_a.calculated_used_total,
        path_b_raw_total=summary_b.raw_total,
        path_b_calculated_total=summary_b.calculated_total,
        path_b_calculated_used_total=summary_b.calculated_used_total,
        amount_source=amount_source,
        amount_status=amount_status,
        derived_rows=derived_rows,
        formula_mismatch_rows=formula_mismatch_rows,
    )


def check_period_by_no(conn: sqlite3.Connection, project_id: int, period_no: int,
                       direction: str | None = None) -> CheckResult:
    """按 (project_id, period_no[, direction]) 的兼容入口。

    direction 缺省且该期号存在多个方向 → AmbiguousPeriodError（明确拒绝）。
    """
    sql = "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?"
    params: list = [project_id, period_no]
    if direction is not None:
        sql += " AND direction=?"
        params.append(direction)
    rows = conn.execute(sql, params).fetchall()
    if len(rows) == 0:
        raise ValueError(f"period {period_no} (direction={direction}) not found in project {project_id}")
    if len(rows) > 1:
        raise AmbiguousPeriodError(
            f"period_no {period_no} has {len(rows)} periods with different directions;"
            " call with explicit direction"
        )
    return check_period(conn, int(rows[0]["id"]))


def make_evidence(project_id: int, res: CheckResult) -> str:
    status_zh = {"match": "一致", "diff": "存在差异", "incomplete": "数据不完整"}
    return json.dumps(
        {
            "kind": "cross_check",
            "summary": (
                f"第{res.period_no}期{direction_label(res.direction)}双向校核："
                f"{status_zh.get(res.status, '待复核')}，"
                f"{ {'sufficient': '校核充分', 'findings': '校核有发现', 'insufficient': '校核不充分'}.get(res.verification_level, '待复核') }"
            ),
            "steps": [
                {"step": "A 直接累计清洗明细", "result": str(res.path_a_total)},
                {"step": "B 从原始网格独立重算", "result": str(res.path_b_total)},
                {"step": "C 原表小计行", "result": str(res.raw_subtotal)},
                {"step": "差异 A-B", "result": str(res.diff_ab)},
                {"step": "差异 A-C", "result": str(res.control_diff)},
                {"step": "C 控制状态", "result": res.control_status},
                {"step": "参与明细行数", "result": res.detail_rows},
                {"step": "排除小计行数", "result": res.excluded_subtotal_rows},
                {"step": "排除标题/说明行数", "result": res.excluded_title_rows},
                {"step": "待确认工作表数量", "result": res.pending_sheets},
                {"step": "取数范围未证明工作表数量", "result": res.range_unproven_sheets},
            ],
            "sources": [{"period_id": res.period_id, "period": res.period_no,
                         "direction": res.direction, "missing_rows": res.missing_rows}],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
    )


_VALIDATION_PATHS = ("path_a", "path_b", "path_c")


def _validation_key(period_id: int | None, path: str, *, period_no: int | None = None) -> str:
    subject = f"period:{period_id}" if period_id is not None else f"period_no:{period_no}"
    return f"{subject}:{path}"


def _expected_validation_keys(
    conn: sqlite3.Connection,
    project_id: int,
    period_nos: list[int],
    direction: str | None,
) -> list[str]:
    """先登记本批次应覆盖的 A/B/C 路径，异常时也能留下失败覆盖率。"""
    expected: list[str] = []
    for period_no in period_nos:
        sql = "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?"
        params: list[object] = [project_id, period_no]
        if direction is not None:
            sql += " AND direction=?"
            params.append(direction)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            expected.extend(
                _validation_key(None, path, period_no=int(period_no))
                for path in _VALIDATION_PATHS
            )
            continue
        for row in rows:
            expected.extend(
                _validation_key(int(row["id"]), path)
                for path in _VALIDATION_PATHS
            )
    return list(dict.fromkeys(expected))


def _validate_period_selection(
    conn: sqlite3.Connection,
    project_id: int,
    period_nos: list[int],
    direction: str | None,
) -> None:
    """在任何失效化/失败边界前验证期次入口参数。"""
    for period_no in period_nos:
        sql = "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?"
        params: list[object] = [project_id, period_no]
        if direction is not None:
            sql += " AND direction=?"
            params.append(direction)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            raise ValueError(
                f"period {period_no} (direction={direction}) not found in project {project_id}"
            )
        if len(rows) != 1:
            raise AmbiguousPeriodError(
                f"period_no {period_no} has {len(rows)} periods with different directions;"
                " call with explicit direction"
            )


def _requested_period_ids(
    conn: sqlite3.Connection,
    project_id: int,
    period_nos: list[int],
    direction: str | None,
) -> list[int]:
    """解析本次校核目标期次；歧义期号不触发旧结果预失效。"""
    period_ids: list[int] = []
    for period_no in period_nos:
        sql = "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?"
        params: list[object] = [project_id, period_no]
        if direction is not None:
            sql += " AND direction=?"
            params.append(direction)
        rows = conn.execute(sql, params).fetchall()
        if direction is None and len(rows) != 1:
            # 调用方未明确方向时，后续 check_period_by_no 会明确拒绝；在
            # 拒绝前不改变任何旧成果，保持原有歧义入口的事务语义。
            return []
        period_ids.extend(int(row["id"]) for row in rows)
    return list(dict.fromkeys(period_ids))


def _period_total_has_check_state(row: sqlite3.Row) -> bool:
    """判断 period_totals 是否已经承载过校核状态，而非仅有聚合待校核行。"""
    return any(
        (
            row["evidence_id"] is not None,
            row["cross_check_status"] not in (None, "pending"),
            row["cross_check_diff"] is not None,
            row["ab_status"] not in (None, "pending"),
            row["ab_diff"] is not None,
            row["control_status"] not in (None, "not_available"),
            row["control_diff"] is not None,
            row["verification_level"] not in (None, "insufficient"),
        )
    )


def _invalidate_previous_validation(
    conn: sqlite3.Connection,
    project_id: int,
    period_nos: list[int],
    direction: str | None,
    active_contract: run_contract.RunContract,
) -> int:
    """把本次重跑覆盖的旧结果移出当前面，并保留可追溯历史。"""
    period_ids = _requested_period_ids(conn, project_id, period_nos, direction)
    if not period_ids:
        return 0
    placeholders = ",".join("?" for _ in period_ids)
    current_signature = active_contract.signature
    current_scope, current_params = run_contract.current_scope(conn, project_id, "cr")
    old_results = conn.execute(
        f"""SELECT cr.id, cr.period_id, cr.evidence_id
            FROM crosscheck_results cr
            WHERE cr.project_id=? AND {current_scope}
              AND cr.period_id IN ({placeholders})""",
        (project_id, *current_params, *period_ids),
    ).fetchall()
    stale_period_ids = {int(row["period_id"]) for row in old_results}

    total_scope, total_params = run_contract.current_scope(conn, project_id, "pt")
    old_totals = conn.execute(
        f"""SELECT pt.*
               FROM period_totals pt
               WHERE pt.project_id=? AND {total_scope}
                 AND pt.period_id IN ({placeholders})""",
        (project_id, *total_params, *period_ids),
    ).fetchall()
    stale_period_ids.update(
        int(row["period_id"])
        for row in old_totals
        if _period_total_has_check_state(row)
    )

    old_detection = conn.execute(
        """SELECT COUNT(*) AS c FROM detection_runs
           WHERE project_id=? AND run_signature=? AND run_kind=?""",
        (
            project_id,
            current_signature,
            detection_coverage.AGGREGATE_VALIDATION,
        ),
    ).fetchone()
    if not stale_period_ids and not int(old_detection["c"] or 0):
        return 0

    stale_ids = sorted(stale_period_ids)
    evidence_ids = {
        int(row["evidence_id"])
        for row in (*old_results, *old_totals)
        if int(row["period_id"]) in stale_period_ids and row["evidence_id"] is not None
    }
    invalidation_note = "本次校核重跑尚未成功落库，旧结果已移出当前读取面"
    invalidated_at = datetime.now().isoformat(timespec="seconds")
    invalidated_signature = run_contract.INVALIDATED_RUN_SIGNATURE

    def _write_invalidation() -> None:
        with run_contract._transaction(conn, "invalidate_previous_validation"):
            if stale_ids:
                stale_placeholders = ",".join("?" for _ in stale_ids)
                conn.execute(
                    f"""UPDATE crosscheck_results
                       SET verification_level='insufficient', status='invalidated',
                           path_a_total=NULL, path_b_total=NULL, raw_subtotal=NULL,
                           diff_ab=NULL, control_diff=NULL, ab_status='pending',
                           control_status='not_available', detail_rows=0,
                           excluded_subtotal_rows=0, excluded_title_rows=0,
                           pending_sheets=0, range_unproven_sheets=0,
                           notes_json=?, evidence_id=NULL, checked_at=?, run_signature=?
                       WHERE project_id=? AND run_signature=?
                         AND period_id IN ({stale_placeholders})""",
                    (
                        json.dumps([invalidation_note], ensure_ascii=False),
                        invalidated_at,
                        invalidated_signature,
                        project_id,
                        current_signature,
                        *stale_ids,
                    ),
                )
                # R1/I4 的故障注入可能拒绝任意 ``UPDATE OF cross_check_status``。
                # 预失效必须先于新批次提交，并且不能被这种“新批次回写”故障
                # 提前截断；用完整行值做原子替换，保留 period_totals 的 ID、
                # 聚合金额和其他字段，只改变校核状态边界。真正的新结果仍在
                # 后续批次中按原有 UPDATE 写入，因此故障注入仍会在目标阶段触发。
                total_columns = list(old_totals[0].keys()) if old_totals else []
                total_column_sql = ", ".join(total_columns)
                total_value_sql = ", ".join("?" for _ in total_columns)
                for old_total in old_totals:
                    if int(old_total["period_id"]) not in stale_period_ids:
                        continue
                    values = {column: old_total[column] for column in total_columns}
                    values.update(
                        {
                            "cross_check_status": "invalidated",
                            "cross_check_diff": None,
                            "evidence_id": None,
                            "ab_status": "pending",
                            "ab_diff": None,
                            "control_status": "not_available",
                            "control_diff": None,
                            "verification_level": "insufficient",
                            "run_signature": invalidated_signature,
                        }
                    )
                    conn.execute(
                        f"INSERT OR REPLACE INTO period_totals ({total_column_sql}) "
                        f"VALUES ({total_value_sql})",
                        tuple(values[column] for column in total_columns),
                    )

            for evidence_id in sorted(evidence_ids):
                evidence_row = conn.execute(
                    """SELECT summary, steps_json, run_signature FROM evidence
                       WHERE id=? AND project_id=?""",
                    (evidence_id, project_id),
                ).fetchone()
                if not evidence_row or evidence_row["run_signature"] != current_signature:
                    continue
                try:
                    steps = json.loads(evidence_row["steps_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    steps = {"original_steps_json": evidence_row["steps_json"]}
                if not isinstance(steps, dict):
                    steps = {"original_steps": steps}
                steps["invalidation"] = {
                    "status": "invalidated",
                    "at": invalidated_at,
                    "reason": invalidation_note,
                    "previous_run_signature": current_signature,
                }
                summary = evidence_row["summary"] or ""
                if not summary.startswith("【已失效】"):
                    summary = f"【已失效】{summary}"
                conn.execute(
                    """UPDATE evidence
                       SET summary=?, steps_json=?, run_signature=?
                       WHERE id=? AND project_id=? AND run_signature=?""",
                    (
                        summary,
                        json.dumps(steps, ensure_ascii=False, default=str),
                        invalidated_signature,
                        evidence_id,
                        project_id,
                        current_signature,
                    ),
                )

            # 旧 coverage 也必须离开 current_detection_run；否则当新一轮
            # detection_runs INSERT 被阻断时，旧的 complete 仍会被误读为本轮成功。
            conn.execute(
                """UPDATE detection_runs SET run_signature=?
                   WHERE project_id=? AND run_signature=? AND run_kind=?""",
                (
                    invalidated_signature,
                    project_id,
                    current_signature,
                    detection_coverage.AGGREGATE_VALIDATION,
                ),
            )

    # authorizer/驱动的一次性 COMMIT 拒绝会回滚整个失效化事务。仅对事务
    # 助手明确标记的外层 COMMIT 失败重放一次完整写事务；业务 DML 异常、
    # SAVEPOINT 释放异常和持续拒绝均不重试，保持原始异常和回滚边界。
    for attempt in range(2):
        try:
            _write_invalidation()
        except Exception as exc:
            if (
                attempt == 0
                and not conn.in_transaction
                and run_contract._is_transaction_commit_failure(exc)
            ):
                continue
            raise
        return len(stale_period_ids)
    raise AssertionError("invalidation retry loop exhausted without a result")


def _record_validation_coverage(
    conn: sqlite3.Connection,
    project_id: int,
    expected: list[str],
    results: list[CheckResult],
    active_contract: run_contract.RunContract,
    *,
    period_nos: list[int],
    direction: str | None,
    error: BaseException | None = None,
) -> int:
    """记录 A/B/C 实际覆盖率；C 无源表控制额时只能明确标记跳过。"""
    executed: list[str] = []
    skipped: dict[str, str] = {}
    for result in results:
        executed.extend(
            _validation_key(result.period_id, path)
            for path in ("path_a", "path_b")
        )
        control_key = _validation_key(result.period_id, "path_c")
        if result.raw_subtotal is None:
            skipped[control_key] = "原表小计/合计控制值不可用"
        else:
            executed.append(control_key)

    expected_set = set(expected)
    accounted = set(executed) | set(skipped)
    failure_text = (
        f"{type(error).__name__}: {error}" if error is not None else "本批次结束时未完成"
    )
    failed = {key: failure_text for key in expected_set - accounted}
    if error is not None and expected_set:
        # 发生业务或覆盖持久化异常时，即使计算已产生结果，也不能把这批次
        # 的部分执行记录解释为成功；覆盖记录本身可落库时统一标记为失败。
        failed = {key: failure_text for key in expected_set}
    coverage = detection_coverage.coverage_from_values(
        expected=expected,
        executed=executed,
        skipped=skipped,
        failed=failed,
        critical_failed=failed,
    )
    return detection_coverage.record_detection_run(
        conn,
        project_id,
        coverage,
        run_signature=active_contract.signature,
        run_kind=detection_coverage.AGGREGATE_VALIDATION,
        error_summary=failure_text if error is not None else None,
        metadata={
            "stage": "A/B/C crosscheck",
            "period_nos": list(period_nos),
            "direction": direction,
            "result_count": len(results),
            "incomplete_results": [
                result.period_id for result in results if result.status == "incomplete"
            ],
        },
    )


def _record_invalidation_failure_boundary(
    conn: sqlite3.Connection,
    project_id: int,
    expected: list[str],
    active_contract: run_contract.RunContract,
    *,
    period_nos: list[int],
    direction: str | None,
    error: BaseException,
) -> None:
    """把失效化失败提升为运行级不可用，并尽力保留失败覆盖诊断。"""
    # 先设置边界，再尝试写 FAILED coverage。侧车不可写时
    # ``set_fail_closed_state`` 会落到同进程 fallback；无论 coverage 是否能
    # 写入，调用方都继续保留这里收到的原始异常。
    run_contract.set_fail_closed_state(
        conn,
        project_id,
        reason="失效化阶段未能安全完成，数据库不可写，当前结果不可用",
        run_signature=active_contract.signature,
        error=error,
    )
    _record_validation_coverage(
        conn,
        project_id,
        expected,
        [],
        active_contract,
        period_nos=period_nos,
        direction=direction,
        error=error,
    )


def _set_crosscheck_fail_closed(
    conn: sqlite3.Connection,
    project_id: int,
    active_contract: run_contract.RunContract | None,
    error: BaseException,
    *,
    reason: str,
) -> None:
    """在任何批次级失败后先建立运行边界，再尝试补充数据库诊断。"""
    run_contract.set_fail_closed_state(
        conn,
        project_id,
        reason=reason,
        run_signature=active_contract.signature if active_contract else None,
        error=error,
    )


def run_crosscheck(conn: sqlite3.Connection, project_id: int, period_nos: list[int],
                   direction: str | None = None) -> list[CheckResult]:
    """执行多期校核；差异与结论写入 evidence 表并回填 period_totals。

    direction=None 时若某期号对应多个方向，抛 AmbiguousPeriodError。
    证据与 period_totals 回写锁定同一 period_id。
    """
    # 参数歧义属于调用方输入错误，不应先失效化旧结果再把它记录成数据库
    # 故障；先完成选择验证，避免一次错误入口永久留下运行级不可用状态。
    # 但验证失败本身仍须留下 FAILED coverage，便于审阅者知道本次入口没有
    # 覆盖任何期次，不能被误读成“没有运行记录所以没有问题”。
    try:
        _validate_period_selection(conn, project_id, period_nos, direction)
    except (AmbiguousPeriodError, ValueError) as validation_error:
        active_contract: run_contract.RunContract | None = None
        try:
            active_contract = run_contract.ensure_run_contract(conn, project_id)
            expected = _expected_validation_keys(conn, project_id, period_nos, direction)
            _record_validation_coverage(
                conn,
                project_id,
                expected,
                [],
                active_contract,
                period_nos=period_nos,
                direction=direction,
                error=validation_error,
            )
        except Exception as coverage_error:
            if active_contract is not None:
                _set_crosscheck_fail_closed(
                    conn,
                    project_id,
                    active_contract,
                    coverage_error,
                    reason="校核入口参数失败且覆盖率无法记录，当前结果不可用",
                )
            raise validation_error from coverage_error
        raise
    had_outer_transaction = conn.in_transaction
    try:
        active_contract = run_contract.ensure_run_contract(conn, project_id)
    except Exception as exc:
        # 契约切换本身发生在预失效之前。若这里的 COMMIT 被拒绝，旧契约和
        # 旧成功行会因事务回滚而仍在库内；必须先用运行级边界把它们从所有
        # 当前读取面挡住，不能等到后续失效化阶段才处理。
        try:
            _set_crosscheck_fail_closed(
                conn,
                project_id,
                run_contract.get_current_contract(conn, project_id),
                exc,
                reason="运行契约切换未能安全提交，数据库不可写，当前结果不可用",
            )
        except Exception as boundary_error:
            raise exc from boundary_error
        raise
    had_fail_closed_state = run_contract.get_fail_closed_state(conn, project_id) is not None
    expected = _expected_validation_keys(conn, project_id, period_nos, direction)
    # 同一输入签名的重跑不能让上一次成功结果跨过本次失败继续充当当前
    # 成果。预失效单独落在批次事务之前，才能在后续 Evidence、coverage 或
    # COMMIT 失败回滚后继续保留 fail-closed 边界；嵌套调用方事务则由
    # _transaction 以 savepoint 方式参与其外层事务。
    try:
        _invalidate_previous_validation(
            conn, project_id, period_nos, direction, active_contract
        )
    except Exception as exc:
        try:
            _record_invalidation_failure_boundary(
                conn,
                project_id,
                expected,
                active_contract,
                period_nos=period_nos,
                direction=direction,
                error=exc,
            )
        except Exception as boundary_or_coverage_error:
            # 失败覆盖或边界登记是补充诊断，绝不能覆盖失效化阶段的原始
            # SQLite DML/COMMIT 异常；from 链保留补充失败供排查。
            raise exc from boundary_or_coverage_error
        raise
    results: list[CheckResult] = []
    coverage_run_id: int | None = None
    now = datetime.now().isoformat(timespec="seconds")
    batch_transaction = run_contract._transaction(conn, "run_crosscheck_batch")
    batch_transaction.__enter__()
    for pno in period_nos:
        try:
            # 计算、状态门控及本期所有业务回写共享同一个错误边界；发生
            # 任何异常时，由事务上下文回滚本期半成品，再登记失败覆盖率。
            res = check_period_by_no(conn, project_id, pno, direction=direction)
            dir_label = direction_label(res.direction)
            status_zh = {"match": "一致", "diff": "存在差异", "incomplete": "数据不完整"}
            if res.status != "match":
                combined_status = res.status
                combined_diff = res.diff_ab
            elif res.control_status == "diff":
                combined_status = "control_diff"
                combined_diff = res.control_diff
            elif res.control_status == "match":
                combined_status = "match"
                combined_diff = res.diff_ab
            else:
                combined_status = "ab_match_control_not_available"
                combined_diff = res.diff_ab
            # 兼容旧读取面的同时，禁止“数值碰巧一致但证据条件不足”仍落成
            # ``cross_check_status='match'``。逐清单旧字段保留 A/B/C 原始状态，
            # 但项目级整体状态以三档校核级别为最终门控；所有新界面/成果读取
            # verification_level，历史消费者也能从此值看出不能按通过解释。
            if res.verification_level == "insufficient" and combined_status == "match":
                combined_status = "insufficient"
            with run_contract._transaction(conn, "run_crosscheck"):
                cur = conn.execute(
                    """INSERT INTO evidence(
                           project_id, kind, summary, steps_json, sources_json,
                           created_at, run_signature)
                       VALUES (?,?,?,?,?,?,?)""",
                    (project_id, "cross_check",
                     f"第{pno}期{dir_label}双向校核：{status_zh.get(res.status, '待复核')}；"
                     f"{'校核充分' if res.verification_level == 'sufficient' else '校核有发现' if res.verification_level == 'findings' else '校核不充分'}",
                     json.dumps(res.__dict__ | {"path_a_total": str(res.path_a_total),
                                                "path_b_total": str(res.path_b_total),
                                                "raw_subtotal": str(res.raw_subtotal),
                                                "diff_ab": str(res.diff_ab),
                                                "control_diff": str(res.control_diff)},
                                ensure_ascii=False, default=str),
                     json.dumps({"period_id": res.period_id, "period": pno, "direction": res.direction}),
                     now, active_contract.signature),
                )
                ev_id = cur.lastrowid
                conn.execute(
                    """UPDATE period_totals SET
                       cross_check_status=?, cross_check_diff=?, evidence_id=?,
                       ab_status=?, ab_diff=?, control_status=?, control_diff=?,
                       verification_level=?, run_signature=?
                       WHERE period_id=?""",
                    (
                        combined_status,
                        str(combined_diff) if combined_diff is not None else None,
                        ev_id,
                        res.status,
                        str(res.diff_ab) if res.diff_ab is not None else None,
                        res.control_status,
                        str(res.control_diff) if res.control_diff is not None else None,
                        res.verification_level,
                        active_contract.signature,
                        res.period_id,
                    ),
                )
                conn.execute(
                    """INSERT INTO crosscheck_results(
                           project_id, period_id, verification_level, status,
                           path_a_total, path_b_total, raw_subtotal, diff_ab, control_diff,
                           ab_status, control_status, detail_rows, excluded_subtotal_rows,
                           excluded_title_rows, pending_sheets, range_unproven_sheets,
                           notes_json, evidence_id, checked_at, run_signature)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(period_id) DO UPDATE SET
                           project_id=excluded.project_id,
                           verification_level=excluded.verification_level,
                           status=excluded.status,
                           path_a_total=excluded.path_a_total,
                           path_b_total=excluded.path_b_total,
                           raw_subtotal=excluded.raw_subtotal,
                           diff_ab=excluded.diff_ab,
                           control_diff=excluded.control_diff,
                           ab_status=excluded.ab_status,
                           control_status=excluded.control_status,
                           detail_rows=excluded.detail_rows,
                           excluded_subtotal_rows=excluded.excluded_subtotal_rows,
                           excluded_title_rows=excluded.excluded_title_rows,
                           pending_sheets=excluded.pending_sheets,
                           range_unproven_sheets=excluded.range_unproven_sheets,
                           notes_json=excluded.notes_json,
                           evidence_id=excluded.evidence_id,
                           checked_at=excluded.checked_at,
                           run_signature=excluded.run_signature""",
                    (
                        project_id, res.period_id, res.verification_level, combined_status,
                        str(res.path_a_total) if res.path_a_total is not None else None,
                        str(res.path_b_total) if res.path_b_total is not None else None,
                        str(res.raw_subtotal) if res.raw_subtotal is not None else None,
                        str(res.diff_ab) if res.diff_ab is not None else None,
                        str(res.control_diff) if res.control_diff is not None else None,
                        res.status, res.control_status, res.detail_rows,
                        res.excluded_subtotal_rows, res.excluded_title_rows,
                        res.pending_sheets, res.range_unproven_sheets,
                        json.dumps(res.notes, ensure_ascii=False), ev_id, now,
                        active_contract.signature,
                    ),
                )
        except Exception as exc:
            cleanup_error = None
            try:
                batch_transaction.__exit__(type(exc), exc, exc.__traceback__)
            except Exception as rollback_error:
                cleanup_error = rollback_error
            try:
                _set_crosscheck_fail_closed(
                    conn,
                    project_id,
                    active_contract,
                    exc,
                    reason="交叉校验批次未能完成，当前结果不可用",
                )
            except Exception as boundary_error:
                raise exc from boundary_error
            try:
                _record_validation_coverage(
                    conn,
                    project_id,
                    expected,
                    results,
                    active_contract,
                    period_nos=period_nos,
                    direction=direction,
                    error=exc,
                )
            except Exception as coverage_exc:
                raise exc from coverage_exc
            if cleanup_error is not None:
                raise exc from cleanup_error
            raise
        results.append(res)
    try:
        coverage_run_id = _record_validation_coverage(
            conn,
            project_id,
            expected,
            results,
            active_contract,
            period_nos=period_nos,
            direction=direction,
        )
    except Exception as exc:
        cleanup_error = None
        try:
            batch_transaction.__exit__(type(exc), exc, exc.__traceback__)
        except Exception as rollback_error:
            cleanup_error = rollback_error
        try:
            _set_crosscheck_fail_closed(
                conn,
                project_id,
                active_contract,
                exc,
                reason="交叉校验覆盖率未能安全记录，当前结果不可用",
            )
        except Exception as boundary_error:
            raise exc from boundary_error
        try:
            _record_validation_coverage(
                conn,
                project_id,
                expected,
                results,
                active_contract,
                period_nos=period_nos,
                direction=direction,
                error=exc,
            )
        except Exception as coverage_exc:
            raise exc from coverage_exc
        if cleanup_error is not None:
            raise exc from cleanup_error
        raise
    else:
        try:
            batch_transaction.__exit__(None, None, None)
        except Exception as exc:
            # _transaction 已经负责清理被拒绝的 COMMIT；失败 coverage 必须
            # 在批次事务之外另行登记，避免把提交异常误当成成功完成。
            try:
                _set_crosscheck_fail_closed(
                    conn,
                    project_id,
                    active_contract,
                    exc,
                    reason="交叉校验批次提交未能完成，当前结果不可用",
                )
            except Exception as boundary_error:
                raise exc from boundary_error
            try:
                _record_validation_coverage(
                    conn,
                    project_id,
                    expected,
                    results,
                    active_contract,
                    period_nos=period_nos,
                    direction=direction,
                    error=exc,
                )
            except Exception as coverage_exc:
                # 失败 coverage 也无法落库时，保持原始 COMMIT 异常为主因；
                # from 链保留 coverage 写入失败供调用方排查。
                raise exc from coverage_exc
            raise
        try:
            # 只有业务回写、成功 coverage 和批次外层 COMMIT 全部完成后，
            # 才允许清除运行级不可用边界。清除失败时立即重新建立边界，
            # 防止已提交的新结果在一个未完成的清除操作下被当作当前成果。
            if had_outer_transaction and had_fail_closed_state:
                if coverage_run_id is None:
                    raise RuntimeError("成功运行缺少覆盖率记录，不能清除当前结果不可用边界")
                run_contract.defer_fail_closed_state_clear(
                    conn,
                    project_id,
                    run_signature=active_contract.signature,
                    coverage_run_id=coverage_run_id,
                    coverage_run_kind=detection_coverage.AGGREGATE_VALIDATION,
                )
            else:
                run_contract.clear_fail_closed_state(
                    conn,
                    project_id,
                    run_signature=active_contract.signature,
                    coverage_run_id=coverage_run_id,
                    coverage_run_kind=detection_coverage.AGGREGATE_VALIDATION,
                )
        except Exception as exc:
            try:
                run_contract.set_fail_closed_state(
                    conn,
                    project_id,
                    reason="新运行已提交但不可用边界清除失败，当前结果不可用",
                    run_signature=active_contract.signature,
                    error=exc,
                )
            except Exception as boundary_error:
                raise exc from boundary_error
            raise
    return results
