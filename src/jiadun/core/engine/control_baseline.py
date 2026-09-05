"""对上控制基准候选与上限比较（宪章 §六 控制基准）。

角色定义：
- control_candidate：终审/审计报告等登记为上限候选的金额；只有人工确认
  （confirmed）的候选才是控制基准；
- settlement_result：对上结算期次的明细合计（导入时小计行已被排除）；
- reference：候选来源的合同/报告原文与 Evidence。

比较输出固定五态，禁止自动调平、禁止用最新/最大/最小金额自动挑选基准：
- CONTROL_CONFLICT：两个已确认基准并存且无 supersedes 替代关系；
- INCOMPARABLE：范围/税口径明确不同；
- PENDING：税口径未确认、候选未确认等无法确认的情形；
- FAIL：结算结果超过已确认控制基准（给出超出金额，不认定违规/责任）；
- PASS：结算结果未超过。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

from jiadun.core.engine.money import to_decimal
from jiadun.core.evidence import evidence as evidence_api

BASELINE_CANDIDATE = "candidate"
BASELINE_CONFIRMED = "confirmed"
BASELINE_REJECTED = "rejected"

# 比较状态
COMPARE_PASS = "PASS"
COMPARE_FAIL = "FAIL"
COMPARE_PENDING = "PENDING"
COMPARE_INCOMPARABLE = "INCOMPARABLE"
COMPARE_CONTROL_CONFLICT = "CONTROL_CONFLICT"

# 税口径规范词表：settlement_periods.tax_mode 用 incl_tax/excl_tax/unknown，
# 基准/费率对话框早期用 included/excluded——比较前统一归一到 incl_tax/excl_tax，
# 否则同义口径会被误判 INCOMPARABLE（词表不一致缺陷，v54 收口）。
_TAX_ALIASES = {
    "included": "incl_tax",
    "含税": "incl_tax",
    "incl_tax": "incl_tax",
    "excluded": "excl_tax",
    "不含税": "excl_tax",
    "excl_tax": "excl_tax",
}
TAX_BASIS_VALUES = ("unknown", "incl_tax", "excl_tax")


def normalize_tax_basis(value) -> str:
    """把历史/界面税口径写法归一到 canonical 词表；无法识别按 unknown（fail-closed）。"""
    text = str(value or "").strip().lower()
    return _TAX_ALIASES.get(text, "unknown")


def list_baselines(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT cb.id, cb.doc_id, cb.fact_id, cb.amount, cb.tax_basis, cb.scope_note,
                  cb.source_note, cb.status, cb.supersedes_id, cb.created_at,
                  cb.confirmed_at, cb.confirmed_by, cb.confirmed_reason,
                  cd.title AS doc_title
           FROM control_baselines cb
           LEFT JOIN contract_docs cd ON cd.id=cb.doc_id
           WHERE cb.project_id=?
           ORDER BY cb.id DESC""",
        (int(project_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def create_candidate_from_fact(
    conn: sqlite3.Connection,
    project_id: int,
    fact_id: int,
    *,
    tax_basis: str = "unknown",
    scope_note: str = "",
    supersedes_id: int | None = None,
) -> int:
    """从一条已确认合同事实创建控制基准候选。

    宪章原则：只有 confirmed 事实才能用于控制规则；候选本身仍需人工确认。
    """
    fact = conn.execute(
        """SELECT cf.id, cf.fact_value, cf.review_status, cf.fact_key, cd.id AS doc_id,
                  cd.title AS doc_title
           FROM contract_facts cf JOIN contract_docs cd ON cd.id=cf.doc_id
           WHERE cf.id=? AND cd.project_id=?""",
        (int(fact_id), int(project_id)),
    ).fetchone()
    if fact is None:
        raise ValueError(f"合同事实不存在或不属于当前项目：fact_id={fact_id}")
    if (fact["review_status"] or "candidate") != "confirmed":
        raise ValueError(
            "只有已确认（confirmed）的合同事实才能登记为控制基准候选；"
            "请先在条款确认中确认该事实"
        )
    try:
        amount = to_decimal(fact["fact_value"])
    except Exception as exc:  # noqa: BLE001 — 非金额事实必须显式失败
        raise ValueError(f"事实值不是可识别金额：{fact['fact_value']!r}") from exc
    return _insert_candidate(
        conn, int(project_id), int(fact["doc_id"]), int(fact_id), amount,
        tax_basis=tax_basis, scope_note=scope_note,
        source_note=f"来自已确认事实《{fact['doc_title']}》·{fact['fact_key']}",
        supersedes_id=supersedes_id,
    )


def create_candidate_manual(
    conn: sqlite3.Connection,
    project_id: int,
    amount,
    *,
    tax_basis: str = "unknown",
    scope_note: str = "",
    source_note: str,
    supersedes_id: int | None = None,
) -> int:
    """人工显式登记基准候选（金额与出处由人工负责，仍需确认后才生效）。"""
    if not (source_note or "").strip():
        raise ValueError("人工登记控制基准候选必须说明金额出处")
    try:
        amount_dec = to_decimal(amount)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"金额不是可识别数值：{amount!r}") from exc
    return _insert_candidate(
        conn, int(project_id), None, None, amount_dec,
        tax_basis=tax_basis, scope_note=scope_note,
        source_note=source_note.strip(), supersedes_id=supersedes_id,
    )


def _insert_candidate(
    conn: sqlite3.Connection,
    project_id: int,
    doc_id: int | None,
    fact_id: int | None,
    amount: Decimal,
    *,
    tax_basis: str,
    scope_note: str,
    source_note: str,
    supersedes_id: int | None,
) -> int:
    tax_basis_canonical = normalize_tax_basis(tax_basis)
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            """INSERT INTO control_baselines(
                   project_id, doc_id, fact_id, amount, tax_basis, scope_note,
                   source_note, status, supersedes_id, created_at)
               VALUES (?,?,?,?,?,?,?, 'candidate', ?,?)""",
            (project_id, doc_id, fact_id, str(amount), tax_basis_canonical,
             scope_note.strip(), source_note, supersedes_id, now),
        )
        baseline_id = int(cur.lastrowid)
        evidence_api.add_evidence(
            conn, project_id, "control_baseline",
            f"登记控制基准候选 {amount} 元（{source_note}）",
            steps=[{
                "step": "登记控制基准候选",
                "baseline_id": baseline_id,
                "amount": str(amount),
                "tax_basis": tax_basis_canonical,
                "supersedes_id": supersedes_id,
            }],
            sources=([{"doc_id": doc_id, "fact_id": fact_id}] if doc_id else [{"manual": True}]),
            commit=False,
        )
    return baseline_id


def set_baseline_review(
    conn: sqlite3.Connection,
    project_id: int,
    baseline_id: int,
    decision: str,
    *,
    reviewed_by: str = "user",
    reason: str = "",
) -> dict:
    """确认或拒绝控制基准候选；确认必须给出依据（范围/税口径/版本核对结论）。"""
    if decision not in (BASELINE_CONFIRMED, BASELINE_REJECTED):
        raise ValueError(f"未知的基准决定：{decision}")
    row = conn.execute(
        "SELECT id, status, amount FROM control_baselines WHERE id=? AND project_id=?",
        (int(baseline_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise ValueError(f"控制基准不存在或不属于当前项目：id={baseline_id}")
    if decision == BASELINE_CONFIRMED and not (reason or "").strip():
        raise ValueError("确认控制基准必须给出核对依据（范围/税口径/版本）")
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute(
            """UPDATE control_baselines
               SET status=?, confirmed_at=?, confirmed_by=?, confirmed_reason=?
               WHERE id=?""",
            (decision, now, reviewed_by, (reason or "").strip(), int(baseline_id)),
        )
        evidence_api.add_evidence(
            conn, int(project_id), "control_baseline_review",
            f"控制基准 #{baseline_id}（{row['amount']} 元）：{row['status']} → {decision}"
            + (f"（{(reason or '').strip()}）" if (reason or "").strip() else ""),
            steps=[{
                "step": "人工确认控制基准",
                "baseline_id": int(baseline_id),
                "decision": decision,
                "reviewed_by": reviewed_by,
                "reason": (reason or "").strip(),
            }],
            sources=[{"baseline_id": int(baseline_id)}],
            commit=False,
        )
    return {"baseline_id": int(baseline_id), "decision": decision}


def _active_confirmed_baselines(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """已确认且未被另一个已确认基准显式取代（supersedes）的有效基准。"""
    baselines = [
        b for b in list_baselines(conn, project_id)
        if b["status"] == BASELINE_CONFIRMED
    ]
    superseded_ids = {
        int(b["supersedes_id"]) for b in baselines if b["supersedes_id"]
    }
    return [b for b in baselines if int(b["id"]) not in superseded_ids]


def list_upward_periods(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """对上结算期次与明细合计（Decimal；小计行在导入时已被排除，不在 line_items）。"""
    rows = conn.execute(
        """SELECT sp.id, sp.period_no, sp.title, sp.tax_mode,
                  COUNT(li.id) AS detail_rows
           FROM settlement_periods sp
           LEFT JOIN line_items li ON li.period_id=sp.id
           WHERE sp.project_id=? AND sp.direction='upward'
           GROUP BY sp.id ORDER BY sp.period_no""",
        (int(project_id),),
    ).fetchall()
    result = []
    for row in rows:
        amounts = [
            r["amount"] for r in conn.execute(
                "SELECT amount FROM line_items WHERE period_id=? AND amount IS NOT NULL",
                (row["id"],),
            ).fetchall()
        ]
        total = sum((to_decimal(a) for a in amounts), Decimal("0"))
        result.append(
            {
                "period_id": int(row["id"]),
                "period_no": int(row["period_no"]),
                "title": row["title"],
                "tax_mode": row["tax_mode"] or "unknown",
                "detail_rows": int(row["detail_rows"]),
                "amount_total": str(total),
            }
        )
    return result


def compare_upward_result(
    conn: sqlite3.Connection,
    project_id: int,
    baseline_id: int,
    settlement_amount,
    *,
    settlement_tax_basis: str = "unknown",
    settlement_scope_note: str = "",
    period_id: int | None = None,
) -> dict:
    """把对上结算结果与指定控制基准比较，输出五态结论（不自动挑选基准）。

    五态：PASS / FAIL（超出金额，不认定违规）/ PENDING（未确认）/
    INCOMPARABLE（范围或税口径明确不同）/ CONTROL_CONFLICT（有效基准并存）。
    每次比较把结论快照追加写入 control_conclusions（历史不覆盖）；
    税口径两侧先归一到 canonical 词表（incl_tax/excl_tax）再比较。
    """
    baseline = conn.execute(
        "SELECT * FROM control_baselines WHERE id=? AND project_id=?",
        (int(baseline_id), int(project_id)),
    ).fetchone()
    if baseline is None:
        raise ValueError(f"控制基准不存在或不属于当前项目：id={baseline_id}")
    if period_id is not None:
        period = conn.execute(
            "SELECT id FROM settlement_periods WHERE id=? AND project_id=? AND direction='upward'",
            (int(period_id), int(project_id)),
        ).fetchone()
        if period is None:
            raise ValueError(f"对上结算期次不存在或不属于当前项目：period_id={period_id}")
    result_amount = to_decimal(settlement_amount)

    status = None
    reason = ""
    delta = None

    if str(baseline["status"]) != BASELINE_CONFIRMED:
        status, reason = COMPARE_PENDING, "控制基准尚未经人工确认"
    else:
        active = _active_confirmed_baselines(conn, project_id)
        if len(active) > 1:
            status, reason = COMPARE_CONTROL_CONFLICT, (
                "存在 "
                + "、".join(f"#{b['id']}（{b['amount']} 元）" for b in active)
                + " 多个有效已确认基准且无 supersedes 替代关系；请人工确认取代关系"
            )
    if status is None:
        base_tax = normalize_tax_basis(baseline["tax_basis"])
        settle_tax = normalize_tax_basis(settlement_tax_basis)
        if base_tax != "unknown" and settle_tax != "unknown" and base_tax != settle_tax:
            status, reason = COMPARE_INCOMPARABLE, (
                f"税口径不同：基准 {base_tax} vs 结算 {settle_tax}"
            )
        elif base_tax == "unknown" or settle_tax == "unknown":
            status, reason = COMPARE_PENDING, "税口径未确认；请先确认基准与结算的含税口径"
        elif (baseline["scope_note"] or "").strip() and (settlement_scope_note or "").strip() \
                and baseline["scope_note"] != settlement_scope_note:
            status, reason = COMPARE_INCOMPARABLE, (
                f"范围不同：基准「{baseline['scope_note']}」vs 结算「{settlement_scope_note}」"
            )
        else:
            base_amount = to_decimal(baseline["amount"])
            delta = result_amount - base_amount
            if delta > 0:
                status = COMPARE_FAIL
                reason = f"结算结果较已确认控制基准高 {delta} 元（不构成违规或责任认定）"
            else:
                status = COMPARE_PASS
                reason = f"结算结果未超过已确认控制基准（结余 {abs(delta)} 元）"

    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        ev_id = evidence_api.add_evidence(
            conn, int(project_id), "control_baseline_compare",
            f"对上结果 {result_amount} 元 vs 基准 #{baseline_id}：{status}"
            + (f"（差额 {delta} 元）" if delta is not None else ""),
            steps=[{
                "step": "对上控制基准比较",
                "baseline_id": int(baseline_id),
                "baseline_amount": str(baseline["amount"]),
                "settlement_amount": str(result_amount),
                "status": status,
                "delta": None if delta is None else str(delta),
                "reason": reason,
            }],
            sources=[{"baseline_id": int(baseline_id)}],
            commit=False,
        )
        cur = conn.execute(
            """INSERT INTO control_conclusions(
                   project_id, baseline_id, period_id, baseline_amount,
                   settlement_amount, status, delta, reason, evidence_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (int(project_id), int(baseline_id),
             None if period_id is None else int(period_id),
             str(baseline["amount"]), str(result_amount), status,
             None if delta is None else str(delta), reason, ev_id, now),
        )
        conclusion_id = int(cur.lastrowid)
    return {
        "conclusion_id": conclusion_id,
        "baseline_id": int(baseline_id),
        "baseline_amount": str(baseline["amount"]),
        "settlement_amount": str(result_amount),
        "status": status,
        "delta": None if delta is None else str(delta),
        "reason": reason,
        "evidence_id": ev_id,
    }


def list_control_conclusions(
    conn: sqlite3.Connection, project_id: int, *, baseline_id: int | None = None
) -> list[dict]:
    """对上控制基准结论快照（只追加历史，按时间倒序返回全部）。"""
    sql = """SELECT cc.id, cc.baseline_id, cc.period_id, cc.baseline_amount,
                    cc.settlement_amount, cc.status, cc.delta, cc.reason,
                    cc.evidence_id, cc.created_at,
                    sp.period_no, sp.title AS period_title,
                    cb.source_note AS baseline_source
             FROM control_conclusions cc
             LEFT JOIN settlement_periods sp ON sp.id=cc.period_id
             LEFT JOIN control_baselines cb ON cb.id=cc.baseline_id
             WHERE cc.project_id=?"""
    params: list[object] = [int(project_id)]
    if baseline_id is not None:
        sql += " AND cc.baseline_id=?"
        params.append(int(baseline_id))
    sql += " ORDER BY cc.id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def set_period_tax_mode(
    conn: sqlite3.Connection,
    project_id: int,
    period_id: int,
    tax_mode: str,
    *,
    reason: str,
    actor: str = "user",
) -> dict:
    """人工确认期次税口径（incl_tax/excl_tax）。

    税口径是比较与费率基数的前置事实，必须人工确认并给出依据；
    确认动作写审计 Evidence，改变的是期次当前口径并保留前后值。
    """
    canonical = normalize_tax_basis(tax_mode)
    if canonical == "unknown":
        raise ValueError(
            "税口径只接受：incl_tax（含税）/ excl_tax（不含税）；未知值不得写入"
        )
    if not (reason or "").strip():
        raise ValueError("确认期次税口径必须填写核对依据（如结算表表头「不含税单价」）")
    row = conn.execute(
        "SELECT id, period_no, tax_mode, direction FROM settlement_periods"
        " WHERE id=? AND project_id=?",
        (int(period_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise ValueError(f"结算期次不存在或不属于当前项目：period_id={period_id}")
    before = row["tax_mode"] or "unknown"
    with conn:
        conn.execute(
            "UPDATE settlement_periods SET tax_mode=? WHERE id=?",
            (canonical, int(period_id)),
        )
        evidence_api.add_evidence(
            conn, int(project_id), "period_tax_mode_review",
            f"期次 #{period_id}（第 {row['period_no']} 期）税口径：{before} → {canonical}"
            f"（{(reason or '').strip()}）",
            steps=[{
                "step": "人工确认期次税口径",
                "period_id": int(period_id),
                "period_no": int(row["period_no"]),
                "before": before,
                "after": canonical,
                "actor": actor,
                "reason": (reason or "").strip(),
            }],
            sources=[{"period_id": int(period_id)}],
            commit=False,
        )
    return {"period_id": int(period_id), "tax_mode": canonical, "before": before}
