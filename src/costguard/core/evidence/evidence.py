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


def add_evidence(
    conn: sqlite3.Connection,
    project_id: int,
    kind: str,
    summary: str,
    steps: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
    commit: bool = True,
    run_signature: str | None = None,
    finding_id: str | None = None,
) -> int:
    """写入一条证据。

    ``commit`` 默认保持原有独立写入行为；需要把证据与业务状态、审计日志
    放进同一个事务的调用方传 ``commit=False``。这避免嵌套 ``with conn`` 在
    中途提交，导致人工操作失败后留下半条证据链。
    """
    now = datetime.now().isoformat(timespec="seconds")
    def _insert() -> int:
        cur = conn.execute(
            """INSERT INTO evidence(
                   project_id, kind, summary, steps_json, sources_json, created_at,
                   run_signature, finding_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (project_id, kind, summary,
             json.dumps(steps or [], ensure_ascii=False, default=str),
             json.dumps(sources or [], ensure_ascii=False, default=str),
             now, run_signature, finding_id),
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
        "finding_id": row["finding_id"] if "finding_id" in row.keys() else None,
    }


def get_chain_for_anomaly(conn: sqlite3.Connection, anomaly_id: int) -> dict | None:
    """异常 → 证据全链路。"""
    row = conn.execute(
        "SELECT evidence_id FROM anomalies WHERE id=?", (anomaly_id,)
    ).fetchone()
    if not row or not row["evidence_id"]:
        return None
    return get_evidence(conn, row["evidence_id"])


def evidence_ref(conn: sqlite3.Connection, evidence_id: int) -> EvidenceRef | None:
    row = conn.execute("SELECT id, kind, summary FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    return EvidenceRef(row["id"], row["kind"], row["summary"]) if row else None
