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

from costguard.core.engine.money import (
    NotANumberError,
    money_mul,
    round2,  # noqa: F401  (money 入口纪律：保留显式依赖)
    to_decimal,
)
from costguard.core.engine.settlement_io import load_sheet_grid
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


def check_period(conn: sqlite3.Connection, period_id: int) -> CheckResult:
    """对单期做 A/B/C 三重校核。全部查询以 period_id 锁定，
    A/B/C 三路径、异常与证据都落在同一期次上。"""
    meta = conn.execute(
        "SELECT period_no, direction FROM settlement_periods WHERE id=?", (period_id,)
    ).fetchone()
    if not meta:
        raise ValueError(f"period {period_id} not found")
    period_no, direction = int(meta["period_no"]), meta["direction"]

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
    # 唯一合计级有值行 → 控制值；零个 → not_available；多个 → 不唯一，
    # not_available 并留注（不擅自挑行或求和）。
    subtotal_rows = [r for r in rows if json.loads(r["flags_json"] or "{}").get("subtotal")]
    grand_rows = [
        r for r in subtotal_rows
        if json.loads(r["flags_json"] or "{}").get("grand_total")
    ]
    grand_with_amount = [r for r in grand_rows if r["amount"] is not None]
    control_note = None
    if len(grand_with_amount) == 1:
        raw_subtotal, _ = _sum_amounts(grand_with_amount)
    else:
        raw_subtotal, _ = _sum_amounts([])  # None
        if len(grand_with_amount) > 1:
            control_note = (
                f"{len(grand_with_amount)} 个合计级行均有金额，C 控制值不唯一，"
                "请人工核对原表（未擅自挑行或求和）")
        elif subtotal_rows:
            control_note = "仅有页级/分部级小计行，无合计级控制行，C 控制不可用"

    # B：从原始网格独立重算（不复用 line_items）
    sheet_ids = conn.execute(
        "SELECT id FROM raw_sheets rs WHERE rs.period_id=?",
        (period_id,),
    ).fetchall()
    summary_b = _AmountSummary()
    for (sheet_id,) in [(r["id"],) for r in sheet_ids]:
        cells, merged, n_rows, _n_cols = load_sheet_grid(conn, sheet_id)
        confirmed = conn.execute(
            """SELECT header_row_lo, header_row_hi, col_map_json, confidence,
                      needs_review, data_row_start, data_row_end
               FROM table_headers WHERE sheet_id=?""",
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
        if det is None:
            continue
        items = extract_items.extract_items(cells, merged, det, n_rows, data_range=data_range)
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
    if derived_rows:
        notes.append(
            f"{derived_rows}行原始金额缺失，A/B路径均按数量×单价计算并保留来源"
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
    return CheckResult(
        period_id=period_id, period_no=period_no, direction=direction,
        path_a_total=total_a, path_b_total=total_b, raw_subtotal=raw_subtotal,
        diff_ab=diff_ab, status=status,
        control_diff=control_diff, control_status=control_status,
        missing_rows=summary_a.missing_rows + summary_b.missing_rows,
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
    return json.dumps(
        {
            "kind": "cross_check",
            "summary": f"第{res.period_no}期双向校核：{res.status}",
            "steps": [
                {"step": "A 直接累计清洗明细", "result": str(res.path_a_total)},
                {"step": "B 从原始网格独立重算", "result": str(res.path_b_total)},
                {"step": "C 原表小计行", "result": str(res.raw_subtotal)},
                {"step": "差异 A-B", "result": str(res.diff_ab)},
                {"step": "差异 A-C", "result": str(res.control_diff)},
                {"step": "C 控制状态", "result": res.control_status},
            ],
            "sources": [{"period_id": res.period_id, "period": res.period_no,
                         "direction": res.direction, "missing_rows": res.missing_rows}],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
    )


def run_crosscheck(conn: sqlite3.Connection, project_id: int, period_nos: list[int],
                   direction: str | None = None) -> list[CheckResult]:
    """执行多期校核；差异与结论写入 evidence 表并回填 period_totals。

    direction=None 时若某期号对应多个方向，抛 AmbiguousPeriodError。
    证据与 period_totals 回写锁定同一 period_id。
    """
    results = []
    now = datetime.now().isoformat(timespec="seconds")
    for pno in period_nos:
        res = check_period_by_no(conn, project_id, pno, direction=direction)
        results.append(res)
        dir_label = {"upward": "对上", "downward": "对下"}.get(res.direction, "")
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
        with conn:
            cur = conn.execute(
                "INSERT INTO evidence(project_id, kind, summary, steps_json, sources_json, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (project_id, "cross_check",
                 f"第{pno}期{dir_label}双向校核：{res.status}".replace("期", "期", 1),
                 json.dumps(res.__dict__ | {"path_a_total": str(res.path_a_total),
                                            "path_b_total": str(res.path_b_total),
                                            "raw_subtotal": str(res.raw_subtotal),
                                            "diff_ab": str(res.diff_ab),
                                            "control_diff": str(res.control_diff)},
                            ensure_ascii=False, default=str),
                 json.dumps({"period_id": res.period_id, "period": pno, "direction": res.direction}), now),
            )
            ev_id = cur.lastrowid
            conn.execute(
                """UPDATE period_totals SET
                   cross_check_status=?, cross_check_diff=?, evidence_id=?,
                   ab_status=?, ab_diff=?, control_status=?, control_diff=?
                   WHERE period_id=?""",
                (
                    combined_status,
                    str(combined_diff) if combined_diff is not None else None,
                    ev_id,
                    res.status,
                    str(res.diff_ab) if res.diff_ab is not None else None,
                    res.control_status,
                    str(res.control_diff) if res.control_diff is not None else None,
                    res.period_id,
                ),
            )
    return results
