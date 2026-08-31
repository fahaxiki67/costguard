"""清洗变更事件。

清洗建议先作为可追溯事件存在，接受也只表示“允许进入人工处理队列”；本
模块不会自动删除原值、填补缺失或改写 ``line_items``，从根上避免把清洗
建议误当成原始事实。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from costguard.core.contracts import run_contract
from costguard.core.evidence import audit as audit_log
from costguard.core.evidence import evidence as evidence_api
from costguard.core.evidence.finding import stable_fingerprint

PROPOSED = "proposed"
ACCEPTED = "accepted"
REJECTED = "rejected"


@dataclass(frozen=True)
class CleaningChange:
    change_id: int
    project_id: int
    run_signature: str
    event_key: str
    subject_type: str
    subject_id: int
    field_name: str
    before: Any
    proposed: Any
    status: str
    reason: str
    actor: str
    created_at: str
    decided_at: str | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    evidence_id: int | None = None
    audit_id: int | None = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _row_to_change(row: sqlite3.Row) -> CleaningChange:
    return CleaningChange(
        change_id=int(row["id"]),
        project_id=int(row["project_id"]),
        run_signature=row["run_signature"],
        event_key=row["event_key"],
        subject_type=row["subject_type"],
        subject_id=int(row["subject_id"]),
        field_name=row["field_name"],
        before=_loads(row["before_json"]),
        proposed=_loads(row["proposed_json"]),
        status=row["status"],
        reason=row["reason"],
        actor=row["actor"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        decision_note=row["decision_note"],
        evidence_id=row["evidence_id"],
        audit_id=row["audit_id"],
    )


def get_change(
    conn: sqlite3.Connection, project_id: int, change_id: int
) -> CleaningChange | None:
    row = conn.execute(
        "SELECT * FROM cleaning_changes WHERE project_id=? AND id=?",
        (project_id, change_id),
    ).fetchone()
    return _row_to_change(row) if row else None


def propose_change(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    subject_type: str,
    subject_id: int,
    field_name: str,
    before: Any,
    proposed: Any,
    actor: str,
    reason: str,
    event_key: str | None = None,
) -> CleaningChange:
    """提出清洗变更并原子写入 Evidence/Audit。"""
    if not reason or not reason.strip():
        raise audit_log.AuditReasonRequiredError("清洗变更必须记录原因")
    if not str(subject_type).strip() or not str(field_name).strip():
        raise ValueError("清洗变更必须指定对象类型和字段")
    signature = run_contract.current_run_signature(conn, project_id, ensure=True)
    key = event_key or stable_fingerprint({
        "project_id": project_id,
        "run_signature": signature,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "field_name": field_name,
        "before": before,
        "proposed": proposed,
    })
    existing = conn.execute(
        "SELECT * FROM cleaning_changes WHERE project_id=? AND event_key=?",
        (project_id, key),
    ).fetchone()
    if existing:
        return _row_to_change(existing)
    created_at = _now()
    with run_contract._transaction(conn, "propose_cleaning_change"):
        evidence_id = evidence_api.add_evidence(
            conn,
            project_id,
            "cleaning_change",
            "提出一项待人工审阅的清洗变更建议。",
            steps=[{
                "event_key": key,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "field_name": field_name,
                "before": before,
                "proposed": proposed,
                "status": PROPOSED,
                "reason": reason.strip(),
            }],
            sources=[{"subject_type": subject_type, "subject_id": subject_id}],
            commit=False,
            run_signature=signature,
        )
        audit_id = audit_log.record_audit(
            conn,
            project_id,
            actor,
            "propose_cleaning_change",
            f"cleaning_change:{key}",
            {"field": field_name, "value": before},
            {"field": field_name, "value": proposed, "evidence_id": evidence_id},
            reason,
            commit=False,
        )
        cur = conn.execute(
            """INSERT INTO cleaning_changes(
                   project_id, run_signature, event_key, subject_type, subject_id,
                   field_name, before_json, proposed_json, status, reason, actor,
                   created_at, evidence_id, audit_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                project_id, signature, key, subject_type, subject_id, field_name,
                _json(before), _json(proposed), PROPOSED, reason.strip(), actor,
                created_at, evidence_id, audit_id,
            ),
        )
        change_id = int(cur.lastrowid)
    return get_change(conn, project_id, change_id)  # type: ignore[return-value]


def decide_change(
    conn: sqlite3.Connection,
    project_id: int,
    change_id: int,
    *,
    status: str,
    actor: str,
    reason: str,
) -> CleaningChange:
    """接受/驳回建议；接受不会自动改写业务明细。"""
    if status not in {ACCEPTED, REJECTED}:
        raise ValueError("清洗变更决定只能是 accepted 或 rejected")
    if not reason or not reason.strip():
        raise audit_log.AuditReasonRequiredError("清洗变更决定必须记录原因")
    row = conn.execute(
        "SELECT * FROM cleaning_changes WHERE project_id=? AND id=?",
        (project_id, change_id),
    ).fetchone()
    if not row:
        raise ValueError(f"cleaning change {change_id} not found")
    if row["status"] != PROPOSED:
        raise ValueError(f"cleaning change {change_id} 已经作出决定")
    signature = run_contract.current_run_signature(conn, project_id, ensure=True)
    decided_at = _now()
    with run_contract._transaction(conn, "decide_cleaning_change"):
        evidence_id = evidence_api.add_evidence(
            conn,
            project_id,
            "cleaning_change_decision",
            f"清洗变更已{ '接受' if status == ACCEPTED else '驳回' }，仍需人工按流程处理。",
            steps=[{
                "change_id": change_id,
                "status": status,
                "reason": reason.strip(),
                "automatic_value_mutation": False,
            }],
            sources=[{"cleaning_change_id": change_id}],
            commit=False,
            run_signature=signature,
        )
        audit_id = audit_log.record_audit(
            conn,
            project_id,
            actor,
            "decide_cleaning_change",
            f"cleaning_change:{change_id}",
            {"status": row["status"]},
            {"status": status, "evidence_id": evidence_id},
            reason,
            commit=False,
        )
        conn.execute(
            """UPDATE cleaning_changes
               SET status=?, decided_at=?, decided_by=?, decision_note=?,
                   evidence_id=?, audit_id=? WHERE project_id=? AND id=?""",
            (status, decided_at, actor, reason.strip(), evidence_id, audit_id, project_id, change_id),
        )
    return get_change(conn, project_id, change_id)  # type: ignore[return-value]


def list_changes(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    status: str | None = None,
    current_signature_only: bool = False,
) -> list[CleaningChange]:
    sql = "SELECT * FROM cleaning_changes WHERE project_id=?"
    params: list[Any] = [project_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    if current_signature_only:
        signature = run_contract.current_run_signature(conn, project_id)
        if not signature:
            return []
        sql += " AND run_signature=?"
        params.append(signature)
    sql += " ORDER BY id"
    return [_row_to_change(row) for row in conn.execute(sql, params).fetchall()]
