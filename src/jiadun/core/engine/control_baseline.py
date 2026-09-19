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

比较结论通过 :func:`record_comparison_finding` 进入审核问题中心
（``rule_id='control_baseline_cap'``），随导出进入异常清单与报告；结论
只报告差额与状态，不构成违规或责任认定。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from jiadun.core.contracts import run_contract
from jiadun.core.engine.money import to_decimal
from jiadun.core.evidence import evidence as evidence_api
from jiadun.core.evidence import finding_lifecycle
from jiadun.core.evidence.finding import Finding

BASELINE_CANDIDATE = "candidate"
BASELINE_CONFIRMED = "confirmed"
BASELINE_REJECTED = "rejected"

# 比较状态
COMPARE_PASS = "PASS"
COMPARE_FAIL = "FAIL"
COMPARE_PENDING = "PENDING"
COMPARE_INCOMPARABLE = "INCOMPARABLE"
COMPARE_CONTROL_CONFLICT = "CONTROL_CONFLICT"


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
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            """INSERT INTO control_baselines(
                   project_id, doc_id, fact_id, amount, tax_basis, scope_note,
                   source_note, status, supersedes_id, created_at)
               VALUES (?,?,?,?,?,?,?, 'candidate', ?,?)""",
            (project_id, doc_id, fact_id, str(amount), tax_basis, scope_note.strip(),
             source_note, supersedes_id, now),
        )
        baseline_id = int(cur.lastrowid)
        evidence_api.add_evidence(
            conn, project_id, "control_baseline",
            f"登记控制基准候选 {amount} 元（{source_note}）",
            steps=[{
                "step": "登记控制基准候选",
                "baseline_id": baseline_id,
                "amount": str(amount),
                "tax_basis": tax_basis,
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
) -> dict:
    """把对上结算结果与指定控制基准比较，输出五态结论（不自动挑选基准）。

    五态：PASS / FAIL（超出金额，不认定违规）/ PENDING（未确认）/
    INCOMPARABLE（范围或税口径明确不同）/ CONTROL_CONFLICT（有效基准并存）。
    """
    baseline = conn.execute(
        "SELECT * FROM control_baselines WHERE id=? AND project_id=?",
        (int(baseline_id), int(project_id)),
    ).fetchone()
    if baseline is None:
        raise ValueError(f"控制基准不存在或不属于当前项目：id={baseline_id}")
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
        base_tax = str(baseline["tax_basis"] or "unknown")
        if base_tax != "unknown" and settlement_tax_basis != "unknown" and base_tax != settlement_tax_basis:
            status, reason = COMPARE_INCOMPARABLE, (
                f"税口径不同：基准 {base_tax} vs 结算 {settlement_tax_basis}"
            )
        elif base_tax == "unknown" or settlement_tax_basis == "unknown":
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
    return {
        "baseline_id": int(baseline_id),
        "baseline_amount": str(baseline["amount"]),
        "settlement_amount": str(result_amount),
        "status": status,
        "delta": None if delta is None else str(delta),
        "reason": reason,
        "evidence_id": ev_id,
    }


# ---- 比较结论进入审核问题中心（ROADMAP v0.1.25 遗留项）----

COMPARISON_RULE_ID = "control_baseline_cap"

# 五态 → 审核问题中心级别。FAIL 是预算保护红线但只报差额；PASS 仅作
# 信息性结论留痕；PENDING/INCOMPARABLE 属于口径与确认问题。
_COMPARISON_SEVERITY = {
    COMPARE_FAIL: "high",
    COMPARE_CONTROL_CONFLICT: "medium",
    COMPARE_PENDING: "low",
    COMPARE_INCOMPARABLE: "low",
    COMPARE_PASS: "info",
}

_COMPARISON_IMPACT = {
    COMPARE_FAIL: "对上结算结果超过已确认控制基准，差额原因需人工复核",
    COMPARE_CONTROL_CONFLICT: "有效控制基准并存，人工裁决取代关系前不存在可信上限结论",
    COMPARE_PENDING: "基准或口径未确认，暂不能形成上限结论",
    COMPARE_INCOMPARABLE: "范围或税口径不同，不得直接比较差额",
    COMPARE_PASS: "结算结果在已确认控制基准内，供报告与审计留痕",
}

_COMPARISON_LIMITATIONS = [
    "比较结论只报告差额或状态，不构成违规、责任或最终审定结论",
    "基准金额、结算明细或口径变化后结论不再代表当前状态，需重新比较",
]

_COMPARISON_RECOMMENDATION = (
    "复核基准范围/税口径与对上结算期次的一致性；差额确认后按人工流程处理，"
    "不得自动调平或修改原始数据"
)


def _comparison_finding(
    result: dict,
    *,
    period_no: int | None,
) -> Finding:
    """把 compare_upward_result 输出整理成审核问题中心 Finding。"""
    status = str(result["status"])
    baseline_id = int(result["baseline_id"])
    delta = result.get("delta")
    prefix = f"对上结算第 {int(period_no)} 期合计 {result['settlement_amount']} 元" \
        if period_no is not None else f"对上结算合计 {result['settlement_amount']} 元"
    if status == COMPARE_FAIL:
        message = (
            f"{prefix}超过已确认控制基准 #{baseline_id}"
            f"（{result['baseline_amount']} 元）{delta} 元；仅报告差额，"
            "不构成违规或责任认定"
        )
    elif status == COMPARE_PASS and delta is not None:
        message = (
            f"{prefix}未超过已确认控制基准 #{baseline_id}"
            f"（{result['baseline_amount']} 元，结余 {abs(Decimal(delta))} 元）"
        )
    else:
        message = f"控制基准 #{baseline_id} 上限比较：{result['reason']}"
    raw_values = {
        "baseline_id": baseline_id,
        "baseline_amount": result["baseline_amount"],
        "settlement_amount": result["settlement_amount"],
        "delta": delta,
        "status": status,
    }
    if period_no is not None:
        raw_values["period_no"] = int(period_no)
    return Finding(
        COMPARISON_RULE_ID,
        _COMPARISON_SEVERITY[status],
        "control_baseline",
        baseline_id,
        message,
        {
            "raw_values": raw_values,
            "compare_evidence_id": result.get("evidence_id"),
            "reason": result["reason"],
            "impact": _COMPARISON_IMPACT[status],
            "limitations": list(_COMPARISON_LIMITATIONS),
            "recommendation": _COMPARISON_RECOMMENDATION,
            "confidence": "high",
        },
    )


def record_comparison_finding(
    conn: sqlite3.Connection,
    project_id: int,
    result: dict,
    *,
    period_no: int | None = None,
) -> dict:
    """把一次上限比较结论登记进审核问题中心（同一基准只保留最新快照）。

    快照语义与合同风险检查一致：同一基准再次比较时，旧结论连同证据转为
    历史（不删除、不篡改），新结论从"新发现"开始；不同基准的结论互不
    覆盖。运行异常检测不会清扫本规则（输入未变时结论仍然成立）。
    """
    status = str(result.get("status") or "")
    if status not in _COMPARISON_SEVERITY:
        raise ValueError(f"未知的控制基准比较状态：{status!r}")
    baseline_id = int(result["baseline_id"])
    active_contract = run_contract.ensure_run_contract(conn, project_id)
    finding = _comparison_finding(result, period_no=period_no)
    now = datetime.now().isoformat(timespec="seconds")
    history_rows = conn.execute(
        """SELECT id, finding_id, fingerprint, status, lifecycle_status,
                  resolved_note, created_at, run_signature, run_id
           FROM anomalies
           WHERE project_id=? AND rule_id=? AND subject_id=? AND subject_type=?
             AND fingerprint=? ORDER BY id""",
        (int(project_id), COMPARISON_RULE_ID, baseline_id, "control_baseline",
         finding.fingerprint),
    ).fetchall()
    repeated_history = [
        {
            "anomaly_id": int(row["id"]),
            "finding_id": row["finding_id"],
            "legacy_status": row["status"],
            "lifecycle_status": row["lifecycle_status"],
            "reason": row["resolved_note"],
            "created_at": row["created_at"],
            "run_signature": row["run_signature"],
            "run_id": row["run_id"],
        }
        for row in history_rows
    ]
    with run_contract._transaction(conn, "persist_control_baseline_compare"):
        old_rows = conn.execute(
            """SELECT id, finding_id, fingerprint, lifecycle_status, status,
                      evidence_id, run_signature, run_id
               FROM anomalies
               WHERE project_id=? AND rule_id=? AND subject_type='control_baseline'
                 AND subject_id=?
                 AND COALESCE(lifecycle_status, 'new') <> 'historical'""",
            (int(project_id), COMPARISON_RULE_ID, baseline_id),
        ).fetchall()
        evidence_api.mark_historical(
            conn,
            int(project_id),
            {
                int(row["evidence_id"])
                for row in old_rows if row["evidence_id"] is not None
            },
            "该基准已产生新的上限比较结论，旧结论保留为历史",
            actor="system",
            commit=False,
        )
        for row in old_rows:
            before_status = finding_lifecycle.lifecycle_status(row)
            conn.execute(
                """UPDATE anomalies
                   SET status='stale', lifecycle_status='historical',
                       resolved_note=COALESCE(
                           resolved_note,
                           '该基准已产生新的上限比较结论，旧结论保留为历史'
                       ), lifecycle_updated_at=?, lifecycle_updated_by='system'
                   WHERE id=? AND project_id=?""",
                (now, int(row["id"]), int(project_id)),
            )
            conn.execute(
                """INSERT INTO finding_status_events(
                       project_id, anomaly_id, finding_id, fingerprint,
                       before_status, after_status, reason, actor, occurred_at,
                       run_signature, run_id, evidence_id, audit_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    int(project_id), int(row["id"]), row["finding_id"], row["fingerprint"],
                    before_status, "historical",
                    "该基准已产生新的上限比较结论，旧结论保留为历史",
                    "system", now, row["run_signature"], row["run_id"],
                    row["evidence_id"], None,
                ),
            )
        record = finding.as_record()
        ev_id = evidence_api.add_evidence(
            conn,
            int(project_id),
            COMPARISON_RULE_ID,
            finding.message,
            steps=[{
                "step": "控制基准上限比较结论入册",
                "rule_id": COMPARISON_RULE_ID,
                "finding_id": finding.finding_id,
                "fingerprint": finding.fingerprint,
                "status": status,
                "delta": result.get("delta"),
                "reason": result["reason"],
                "compare_evidence_id": result.get("evidence_id"),
                "impact": finding.impact,
                "limitations": finding.limitations,
                "recommendation": finding.recommendation,
            }],
            sources=[{
                "baseline_id": baseline_id,
                "baseline_amount": result["baseline_amount"],
                "settlement_amount": result["settlement_amount"],
                "status": status,
                **({"period_no": int(period_no)} if period_no is not None else {}),
            }],
            commit=False,
            run_signature=active_contract.signature,
            run_id=active_contract.run_id,
            finding_id=finding.finding_id,
            scope="current",
        )
        cur = conn.execute(
            """INSERT INTO anomalies(
                   project_id, rule_id, severity, subject_type, subject_id,
                   evidence_id, message, status, created_at, run_signature, run_id,
                   finding_id, fingerprint, confidence, detection_mode,
                   raw_values_json, normalized_values_json, impact,
                   limitations_json, recommendation, lifecycle_status,
                   repeat_history_json)
               VALUES (?,?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(project_id), COMPARISON_RULE_ID, finding.severity,
                "control_baseline", baseline_id,
                ev_id, finding.message, now, active_contract.signature,
                active_contract.run_id,
                record["finding_id"], record["fingerprint"], record["confidence"],
                record["detection_mode"],
                json.dumps(record["raw_values"], ensure_ascii=False, default=str),
                json.dumps(record["normalized_values"], ensure_ascii=False, default=str),
                finding.impact,
                json.dumps(finding.limitations, ensure_ascii=False, default=str),
                finding.recommendation, "new",
                json.dumps(repeated_history, ensure_ascii=False, default=str),
            ),
        )
    return {
        "anomaly_id": int(cur.lastrowid),
        "finding_id": finding.finding_id,
        "evidence_id": ev_id,
        "severity": finding.severity,
        "status": status,
    }
