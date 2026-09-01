"""证据链 API（ADR-006）。

任何差异、异常、风险、结论都可生成证据记录：
结论 → 计算过程(steps) → 使用的数据 → 数据来源(文件+Sheet+单元格) → 原始文件。
Evidence ID 全项目唯一（自增主键），UI 据此展开全链路。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: int
    kind: str
    summary: str
    scope: str = "source"
    run_signature: str | None = None
    run_id: str | None = None


def add_evidence(
    conn: sqlite3.Connection,
    project_id: int,
    kind: str,
    summary: str,
    steps: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
    commit: bool = True,
    run_signature: str | None = None,
    run_id: str | None = None,
    finding_id: str | None = None,
    scope: str | None = None,
    historical_reason: str | None = None,
) -> int:
    """写入一条证据。

    ``commit`` 默认保持原有独立写入行为；需要把证据与业务状态、审计日志
    放进同一个事务的调用方传 ``commit=False``。这避免嵌套 ``with conn`` 在
    中途提交，导致人工操作失败后留下半条证据链。
    """
    if scope is None:
        scope = "current" if (run_signature or run_id) else "source"
    if scope not in {"source", "current", "historical", "human"}:
        raise ValueError(f"invalid evidence scope: {scope!r}")
    if scope == "historical" and not historical_reason:
        historical_reason = "当前数据或运行契约已经变化，不参与当前结论"
    now = datetime.now().isoformat(timespec="seconds")
    def _insert() -> int:
        cur = conn.execute(
            """INSERT INTO evidence(
                   project_id, kind, summary, steps_json, sources_json, created_at,
                   run_signature, run_id, finding_id, scope, historical_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (project_id, kind, summary,
             json.dumps(steps or [], ensure_ascii=False, default=str),
             json.dumps(sources or [], ensure_ascii=False, default=str),
             now, run_signature, run_id, finding_id, scope, historical_reason),
        )
        return int(cur.lastrowid)
    if commit:
        with conn:
            return _insert()
    return _insert()


def get_evidence(conn: sqlite3.Connection, evidence_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM evidence WHERE id=?", (evidence_id,)
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "kind": row["kind"],
        "summary": row["summary"],
        "steps": json.loads(row["steps_json"] or "[]"),
        "sources": json.loads(row["sources_json"] or "[]"),
        "created_at": row["created_at"],
        "run_signature": row["run_signature"] if "run_signature" in row.keys() else None,
        "run_id": row["run_id"] if "run_id" in row.keys() else None,
        "finding_id": row["finding_id"] if "finding_id" in row.keys() else None,
        "scope": row["scope"] if "scope" in row.keys() else "source",
        "historical_reason": row["historical_reason"] if "historical_reason" in row.keys() else None,
    }


def record_event(
    conn: sqlite3.Connection,
    project_id: int,
    evidence_id: int,
    event_type: str,
    reason: str,
    *,
    actor: str = "system",
    run_id: str | None = None,
    run_signature: str | None = None,
    after_scope: str | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> int:
    """追加一条 Evidence 事件，保留事件发生前的完整证据快照。

    失效、撤销和修订属于事件，不应只依赖覆盖原 Evidence 的摘要文字。
    兼容旧库时调用方仍可同步更新旧行的 scope，但本表使原始快照可审计还原。
    """
    if not reason or not reason.strip():
        raise ValueError("Evidence 事件必须记录原因")
    row = conn.execute(
        """SELECT scope, summary, steps_json, sources_json,
                  run_id, run_signature
           FROM evidence WHERE id=? AND project_id=?""",
        (int(evidence_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise ValueError(f"evidence {evidence_id} 不属于 project {project_id}")
    now = datetime.now().isoformat(timespec="seconds")

    def _insert() -> int:
        cur = conn.execute(
            """INSERT INTO evidence_events(
                   project_id, evidence_id, event_type, occurred_at, actor,
                   run_id, run_signature, before_scope, after_scope,
                   before_summary, before_steps_json, before_sources_json,
                   reason, metadata_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                project_id, int(evidence_id), event_type, now, actor,
                run_id if run_id is not None else row["run_id"],
                run_signature if run_signature is not None else row["run_signature"],
                row["scope"] if "scope" in row.keys() else "source",
                after_scope,
                row["summary"], row["steps_json"], row["sources_json"],
                reason.strip(),
                json.dumps(metadata or {}, ensure_ascii=False, default=str),
            ),
        )
        return int(cur.lastrowid)

    if commit:
        with conn:
            return _insert()
    return _insert()


def mark_historical(
    conn: sqlite3.Connection,
    project_id: int,
    evidence_ids: list[int] | tuple[int, ...] | set[int],
    reason: str,
    *,
    actor: str = "system",
    commit: bool = True,
) -> int:
    """把旧 Evidence 移出当前读取面，并追加可审计的失效事件。

    业务输入、人工门控或运行契约变化时，结果表可能被删除、重算或替换；
    仅清理结果表会留下仍标记为 ``current`` 的 Evidence，导致历史计数和
    直接读取者误把旧校核当作当前结论。此入口先记录 Evidence 事件，再把
    原记录标记为 ``historical``，保留原始运行身份和来源，不修改源文件。
    ``commit=False`` 供调用方把证据边界与结果失效放入同一事务。
    """
    if not reason or not reason.strip():
        raise ValueError("Evidence 历史化必须记录原因")
    ids = sorted({int(evidence_id) for evidence_id in evidence_ids})
    if not ids:
        return 0

    def _mark() -> int:
        changed = 0
        now = datetime.now().isoformat(timespec="seconds")
        for evidence_id in ids:
            row = conn.execute(
                """SELECT scope, summary, steps_json, run_id, run_signature
                   FROM evidence WHERE id=? AND project_id=?""",
                (evidence_id, int(project_id)),
            ).fetchone()
            if row is None or row["scope"] == "historical":
                continue
            event_reason = reason.strip()
            record_event(
                conn,
                project_id,
                evidence_id,
                "invalidated",
                event_reason,
                actor=actor,
                run_id=row["run_id"],
                run_signature=row["run_signature"],
                after_scope="historical",
                metadata={"historical_at": now},
                commit=False,
            )
            try:
                steps = json.loads(row["steps_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                steps = {"original_steps_json": row["steps_json"]}
            if not isinstance(steps, dict):
                steps = {"original_steps": steps}
            steps["historicalization"] = {
                "status": "historical",
                "at": now,
                "reason": event_reason,
            }
            summary = row["summary"] or ""
            if not summary.startswith("【历史】"):
                summary = f"【历史】{summary}"
            conn.execute(
                """UPDATE evidence
                   SET summary=?, steps_json=?, scope='historical',
                       historical_reason=?
                   WHERE id=? AND project_id=? AND scope<> 'historical'""",
                (
                    summary,
                    json.dumps(steps, ensure_ascii=False, default=str),
                    event_reason,
                    evidence_id,
                    int(project_id),
                ),
            )
            changed += 1
        return changed

    if commit:
        with conn:
            return _mark()
    return _mark()


def get_chain_for_anomaly(conn: sqlite3.Connection, anomaly_id: int) -> dict | None:
    """异常 → 证据全链路。"""
    row = conn.execute(
        "SELECT evidence_id FROM anomalies WHERE id=?", (anomaly_id,)
    ).fetchone()
    if not row or not row["evidence_id"]:
        return None
    return get_evidence(conn, row["evidence_id"])


def evidence_ref(conn: sqlite3.Connection, evidence_id: int) -> EvidenceRef | None:
    row = conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    return (
        EvidenceRef(
            row["id"], row["kind"], row["summary"],
            row["scope"] if "scope" in row.keys() else "source",
            row["run_signature"] if "run_signature" in row.keys() else None,
            row["run_id"] if "run_id" in row.keys() else None,
        )
        if row else None
    )
