"""审计日志（原则 14）：人工修改必须留痕，修改原因必填。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class AuditReasonRequiredError(Exception):
    """人工修改必须给出原因（原则 14）。"""


@dataclass(frozen=True)
class AuditEntry:
    entry_id: int
    actor: str
    action: str
    target: str
    reason: str


def record_audit(
    conn: sqlite3.Connection,
    project_id: int,
    actor: str,
    action: str,
    target: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    reason: str,
    commit: bool = True,
    run_id: str | None = None,
    run_signature: str | None = None,
) -> int:
    """记录一次人工修改。reason 为空直接拒绝。

    ``commit=False`` 供需要与业务更新原子提交的人工操作使用；普通调用方
    仍保持默认的独立事务行为。
    """
    if not reason or not reason.strip():
        raise AuditReasonRequiredError("人工修改必须记录原因（原则 14）")
    now = datetime.now().isoformat(timespec="seconds")
    def _insert() -> int:
        cur = conn.execute(
            """INSERT INTO audit_log(project_id, ts, actor, action, target, before_json, after_json, reason, run_id, run_signature)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (project_id, now, actor, action, target,
             json.dumps(before, ensure_ascii=False, default=str) if before else None,
             json.dumps(after, ensure_ascii=False, default=str) if after else None,
             reason.strip(), run_id, run_signature),
        )
        return int(cur.lastrowid)
    if commit:
        with conn:
            return _insert()
    return _insert()


def history_for(conn: sqlite3.Connection, project_id: int, target: str | None = None) -> list[AuditEntry]:
    if target:
        rows = conn.execute(
            "SELECT id, actor, action, target, reason FROM audit_log WHERE project_id=? AND target=? ORDER BY id",
            (project_id, target),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, actor, action, target, reason FROM audit_log WHERE project_id=? ORDER BY id",
            (project_id,),
        ).fetchall()
    return [AuditEntry(r["id"], r["actor"], r["action"], r["target"], r["reason"]) for r in rows]
