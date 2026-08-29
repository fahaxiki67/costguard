"""异常检测引擎：运行全部规则 → 落库 anomalies + evidence。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from costguard.core.anomalies.rules import ALL_RULES, Finding


def run_anomalies(conn: sqlite3.Connection, project_id: int, rules=None) -> list[Finding]:
    """运行异常检测。默认清空该项目旧检测记录后重跑（检测是纯函数，可重复执行）。"""
    findings: list[Finding] = []
    for rule in rules or ALL_RULES:
        try:
            findings.extend(rule(conn, project_id))
        except Exception as exc:  # 单条规则失败不阻断整体，但必须留下记录
            findings.append(Finding(
                f"rule_error_{rule.__name__}", "low", "project", project_id,
                f"规则 {rule.__name__} 执行失败：{exc}", {},
            ))
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        # 检测可重跑：仅重置自动生成的（人工处理过的保留）
        conn.execute(
            "DELETE FROM anomalies WHERE project_id=? AND status='open' AND resolved_note IS NULL",
            (project_id,),
        )
        for f in findings:
            cur = conn.execute(
                """INSERT INTO evidence(project_id, kind, summary, steps_json, sources_json, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (project_id, "anomaly", f.message,
                 json.dumps({"rule": f.rule_id, "details": f.details}, ensure_ascii=False, default=str),
                 json.dumps({"subject_type": f.subject_type, "subject_id": f.subject_id}),
                 now),
            )
            conn.execute(
                """INSERT INTO anomalies(project_id, rule_id, severity, subject_type, subject_id,
                   evidence_id, message, status, created_at) VALUES (?,?,?,?,?,?,?, 'open', ?)""",
                (project_id, f.rule_id, f.severity, f.subject_type, f.subject_id,
                 cur.lastrowid, f.message, now),
            )
    return findings


def anomaly_summary(findings: list[Finding]) -> dict:
    by_sev = {"high": 0, "medium": 0, "low": 0, "info": 0}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    return {"total": len(findings), **by_sev}
