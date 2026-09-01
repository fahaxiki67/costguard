"""Finding 闭环状态与人工处理事件。

异常发现和业务处理严格分离。规则运行只产生“新发现”；状态变更必须由
人工操作、原因、Evidence 和审计事件共同记录。相同 fingerprint 再次出现
时只提供历史处理参考，不自动关闭或继承历史结论。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jiadun.core.contracts import run_contract
from jiadun.core.evidence import audit as audit_log
from jiadun.core.evidence import evidence as evidence_api

LIFECYCLE_STATUS_CODES = (
    "new",
    "pending_review",
    "confirmed_issue",
    "legitimate_business",
    "pending_data",
    "rectified",
    "closed",
    "historical",
)

LIFECYCLE_STATUS_ZH = {
    "new": "新发现",
    "pending_review": "待复核",
    "confirmed_issue": "已确认问题",
    "legitimate_business": "合理业务情形",
    "pending_data": "待补资料",
    "rectified": "已整改",
    "closed": "已关闭",
    "historical": "历史结果——当前数据或运行契约已经变化，不参与当前结论",
}

LEGACY_TO_LIFECYCLE = {
    "open": "new",
    "deferred": "pending_review",
    "verified_no_issue": "legitimate_business",
    "supplemented": "pending_data",
    "corrected": "rectified",
    "resolved": "closed",
    "stale": "historical",
    "superseded": "historical",
}

LIFECYCLE_TO_LEGACY = {
    "new": "open",
    "pending_review": "deferred",
    # 旧列没有“已确认问题”状态，保留为 open，避免现有待核实清单把它
    # 当成已关闭；正式展示使用 lifecycle_status。
    "confirmed_issue": "open",
    "legitimate_business": "verified_no_issue",
    "pending_data": "deferred",
    "rectified": "corrected",
    "closed": "resolved",
    "historical": "stale",
}


class FindingLifecycleError(RuntimeError):
    """Finding 不允许执行当前状态变更。"""


@dataclass(frozen=True)
class FindingStatusEvent:
    event_id: int
    anomaly_id: int
    before_status: str
    after_status: str
    reason: str
    actor: str
    occurred_at: str
    evidence_id: int | None
    audit_id: int | None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def lifecycle_status(row_or_status: sqlite3.Row | dict[str, Any] | str | None) -> str:
    """返回统一生命周期码；兼容迁移前/外部写入的旧 status。"""
    if isinstance(row_or_status, (sqlite3.Row, dict)):
        value = (
            row_or_status.get("lifecycle_status")
            if isinstance(row_or_status, dict)
            else row_or_status["lifecycle_status"]
            if "lifecycle_status" in row_or_status.keys()
            else None
        )
        legacy = (
            row_or_status.get("status")
            if isinstance(row_or_status, dict)
            else row_or_status["status"]
            if "status" in row_or_status.keys()
            else None
        )
        if value in LIFECYCLE_STATUS_CODES:
            if value != "new" or legacy in (None, "", "open"):
                return str(value)
            # 迁移前部分行只有 ``status``，或外部旧脚本把 lifecycle 留在
            # 默认 ``new`` 却写入了已结束 legacy status；此时仍按 legacy
            # 兼容映射，但映射失败必须继续保持 unknown。
        elif value not in (None, ""):
            return "unknown"
        # 未知生命周期码不能回退成 ``new`` 或 legacy 的已关闭状态；否则
        # 损坏/未来 schema 行会被风险摘要当成已处理。统一返回 unknown，
        # 由读取门和项目状态按待确认/不可形成结论处理。
        return LEGACY_TO_LIFECYCLE.get(str(legacy or ""), "unknown")
    value = str(row_or_status or "")
    if value in LIFECYCLE_STATUS_CODES:
        return value
    return LEGACY_TO_LIFECYCLE.get(value, "unknown")


def lifecycle_label(value: str | None) -> str:
    return LIFECYCLE_STATUS_ZH.get(str(value or ""), "待人工确认")


def legacy_status(value: str) -> str:
    if value not in LIFECYCLE_TO_LEGACY:
        raise ValueError(f"不支持的 Finding 状态: {value!r}")
    return LIFECYCLE_TO_LEGACY[value]


def _loads(value: str | None, fallback: Any) -> Any:
    try:
        data = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return data


def repeat_history(value: str | None) -> list[dict[str, Any]]:
    data = _loads(value, [])
    if not isinstance(data, list):
        return []
    return [dict(item) for item in data if isinstance(item, dict)]


def _current_anomaly(
    conn: sqlite3.Connection, project_id: int, anomaly_id: int
) -> sqlite3.Row:
    scope, params = run_contract.current_scope(conn, project_id, "a")
    row = conn.execute(
        f"""SELECT a.* FROM anomalies a
            WHERE a.id=? AND a.project_id=? AND {scope}""",
        (int(anomaly_id), int(project_id), *params),
    ).fetchone()
    if row is None:
        raise run_contract.CurrentResultsUnavailableError(
            "处理审核问题不可用：问题已不在当前运行范围，请刷新后重试。"
        )
    current_status = lifecycle_status(row)
    if current_status == "historical":
        raise FindingLifecycleError("历史 Finding 不得再次处理")
    if current_status == "unknown":
        raise FindingLifecycleError("未知 Finding 生命周期不得直接处理，请先人工核对数据")
    return row


def update_finding_status(
    conn: sqlite3.Connection,
    project_id: int,
    anomaly_id: int,
    new_status: str,
    *,
    actor: str,
    reason: str,
) -> FindingStatusEvent:
    """原子更新 Finding 生命周期，并追加 Evidence/Audit/事件。"""
    normalized = str(new_status).strip().lower()
    if normalized not in LIFECYCLE_STATUS_CODES or normalized == "historical":
        raise ValueError(f"不支持的 Finding 状态: {new_status!r}")
    if not str(actor).strip():
        raise ValueError("Finding 状态变更必须记录操作人")
    if not str(reason).strip():
        raise audit_log.AuditReasonRequiredError("Finding 状态变更必须记录原因")
    row = _current_anomaly(conn, project_id, anomaly_id)
    before = lifecycle_status(row)
    if before == normalized:
        raise ValueError("Finding 已处于该状态，无需重复变更")
    occurred_at = _now()
    run_signature = row["run_signature"]
    run_id = row["run_id"]
    with run_contract._transaction(conn, "update_finding_status"):
        evidence_id = evidence_api.add_evidence(
            conn,
            project_id,
            "finding_status_change",
            f"Finding #{anomaly_id}：{lifecycle_label(before)} → {lifecycle_label(normalized)}",
            steps=[{
                "step": "人工处理 Finding",
                "anomaly_id": int(anomaly_id),
                "before_status": before,
                "after_status": normalized,
                "reason": str(reason).strip(),
                "actor": str(actor).strip(),
            }],
            sources=[{
                "anomaly_id": int(anomaly_id),
                "finding_id": row["finding_id"],
                "fingerprint": row["fingerprint"],
            }],
            commit=False,
            run_signature=run_signature,
            run_id=run_id,
            finding_id=row["finding_id"],
            scope="human",
        )
        audit_id = audit_log.record_audit(
            conn,
            project_id,
            str(actor).strip(),
            "update_finding_status",
            f"anomaly:{int(anomaly_id)}",
            {
                "lifecycle_status": before,
                "legacy_status": row["status"],
            },
            {
                "lifecycle_status": normalized,
                "legacy_status": legacy_status(normalized),
                "evidence_id": evidence_id,
            },
            reason,
            commit=False,
            run_id=run_id,
            run_signature=run_signature,
        )
        event_cur = conn.execute(
            """INSERT INTO finding_status_events(
                   project_id, anomaly_id, finding_id, fingerprint,
                   before_status, after_status, reason, actor, occurred_at,
                   run_signature, run_id, evidence_id, audit_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(project_id), int(anomaly_id), row["finding_id"], row["fingerprint"],
                before, normalized, str(reason).strip(), str(actor).strip(), occurred_at,
                run_signature, run_id, evidence_id, audit_id,
            ),
        )
        event_id = int(event_cur.lastrowid)
        # 先落不可变的 Evidence/Audit/事件，再改写 Finding 当前快照。
        # 数据库触发器据此拒绝绕过闭环的直接 SQL 更新；同一事务失败时
        # 这些辅助记录与状态变化一起回滚，不会留下半成品。
        cur = conn.execute(
            """UPDATE anomalies
               SET status=?, lifecycle_status=?, resolved_note=?,
                   lifecycle_updated_at=?, lifecycle_updated_by=?
               WHERE id=? AND project_id=?""",
            (
                legacy_status(normalized), normalized, str(reason).strip(),
                occurred_at, str(actor).strip(), int(anomaly_id), int(project_id),
            ),
        )
        if cur.rowcount != 1:
            raise run_contract.CurrentResultsUnavailableError(
                "处理审核问题不可用：状态未写入，请刷新后重试。"
            )
    return FindingStatusEvent(
        event_id=event_id,
        anomaly_id=int(anomaly_id),
        before_status=before,
        after_status=normalized,
        reason=str(reason).strip(),
        actor=str(actor).strip(),
        occurred_at=occurred_at,
        evidence_id=evidence_id,
        audit_id=audit_id,
    )


def status_history(
    conn: sqlite3.Connection, project_id: int, anomaly_id: int
) -> list[FindingStatusEvent]:
    rows = conn.execute(
        """SELECT id, anomaly_id, before_status, after_status, reason,
                  actor, occurred_at, evidence_id, audit_id
           FROM finding_status_events
           WHERE project_id=? AND anomaly_id=? ORDER BY id""",
        (int(project_id), int(anomaly_id)),
    ).fetchall()
    return [
        FindingStatusEvent(
            event_id=int(row["id"]),
            anomaly_id=int(row["anomaly_id"]),
            before_status=row["before_status"],
            after_status=row["after_status"],
            reason=row["reason"],
            actor=row["actor"],
            occurred_at=row["occurred_at"],
            evidence_id=int(row["evidence_id"]) if row["evidence_id"] is not None else None,
            audit_id=int(row["audit_id"]) if row["audit_id"] is not None else None,
        )
        for row in rows
    ]
