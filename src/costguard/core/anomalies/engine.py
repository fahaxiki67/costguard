"""异常检测引擎：运行全部规则 → 落库 anomalies + evidence。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from costguard.core.anomalies.rules import ALL_RULES, Finding
from costguard.core.labels import direction_label


def _parse_json(value):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _finding_sources(conn: sqlite3.Connection, project_id: int, finding: Finding) -> list[dict]:
    """把异常对象解析到真实文件、Sheet、行列和原始值。

    规则详情中的对象标识只是定位入口；证据记录再补齐保真层位置，避免
    问题中心只能看到 ``subject_id`` 而无法回到原始单元格。
    """
    sources: list[dict] = [{
        "subject_type": finding.subject_type,
        "subject_id": finding.subject_id,
    }]
    if finding.subject_type == "line_item":
        row = conn.execute(
            """SELECT li.id, li.quantity, li.unit_price, li.amount, li.qty_evid,
                      li.price_evid, li.amount_evid, li.flags_json,
                      sp.period_no, sp.direction,
                      rs.sheet_name, sf.original_name
               FROM line_items li
               JOIN settlement_periods sp ON sp.id=li.period_id
               LEFT JOIN raw_sheets rs ON rs.id=li.sheet_id
               LEFT JOIN parse_batches pb ON pb.id=rs.batch_id
               LEFT JOIN source_files sf ON sf.id=pb.file_id
               WHERE li.id=? AND sp.project_id=?""",
            (finding.subject_id, project_id),
        ).fetchone()
        if row:
            try:
                row_flags = _parse_json(row["flags_json"]) or {}
            except (TypeError, AttributeError):
                row_flags = {}
            row_evidence_id = row_flags.get("source_evidence_id") if isinstance(row_flags, dict) else None
            for field, evidence_key in (
                ("quantity", "qty_evid"), ("unit_price", "price_evid"),
                ("amount", "amount_evid"),
            ):
                provenance = _parse_json(row[evidence_key])
                if not isinstance(provenance, dict):
                    continue
                sources.append({
                    "field": field,
                    "file": row["original_name"],
                    "sheet": row["sheet_name"],
                    "period": row["period_no"],
                    "direction": direction_label(row["direction"]),
                    "row": provenance.get("row"),
                    "col": provenance.get("col"),
                    "raw_value": provenance.get("raw", provenance.get("value")),
                    "evidence_id": provenance.get("evidence_id") or row_evidence_id,
                })
            if len(sources) == 1:
                sources.append({
                    "file": row["original_name"], "sheet": row["sheet_name"],
                    "period": row["period_no"], "direction": direction_label(row["direction"]),
                    "location": "清单行（行列来源未记录）",
                })
    elif finding.subject_type == "period":
        row = conn.execute(
            """SELECT period_no, direction FROM settlement_periods
               WHERE id=? AND project_id=?""",
            (finding.subject_id, project_id),
        ).fetchone()
        if row:
            sheets = conn.execute(
                """SELECT rs.sheet_name, sf.original_name
                   FROM raw_sheets rs
                   JOIN parse_batches pb ON pb.id=rs.batch_id
                   JOIN source_files sf ON sf.id=pb.file_id
                   WHERE rs.period_id=? ORDER BY rs.id""",
                (finding.subject_id,),
            ).fetchall()
            if sheets:
                sources.extend({
                    "file": sheet["original_name"],
                    "sheet": sheet["sheet_name"],
                    "period": row["period_no"],
                    "direction": direction_label(row["direction"]),
                    "location": "期次来源工作表",
                } for sheet in sheets[:8])
            else:
                sources.append({
                    "period": row["period_no"],
                    "direction": direction_label(row["direction"]),
                    "location": "期次记录",
                })
    elif finding.subject_type == "sheet":
        row = conn.execute(
            """SELECT rs.sheet_name, rs.period_id, sp.period_no, sp.direction,
                      sf.original_name
               FROM raw_sheets rs
               JOIN parse_batches pb ON pb.id=rs.batch_id
               JOIN source_files sf ON sf.id=pb.file_id
               LEFT JOIN settlement_periods sp ON sp.id=rs.period_id
               WHERE rs.id=? AND sf.project_id=?""",
            (finding.subject_id, project_id),
        ).fetchone()
        if row:
            sources.append({
                "file": row["original_name"], "sheet": row["sheet_name"],
                "period": row["period_no"],
                "direction": direction_label(row["direction"]),
                "location": "工作表",
            })
    return sources


def run_anomalies(conn: sqlite3.Connection, project_id: int, rules=None) -> list[Finding]:
    """运行异常检测。默认清空该项目旧检测记录后重跑（检测是纯函数，可重复执行）。"""
    findings: list[Finding] = []
    for rule in rules or ALL_RULES:
        try:
            findings.extend(rule(conn, project_id))
        except Exception as exc:  # 单条规则失败不阻断整体，但必须留下记录
            findings.append(Finding(
                f"rule_error_{rule.__name__}", "low", "project", project_id,
                "审核规则暂时无法完成，请在高级信息中查看技术详情。",
                {"technical_error": str(exc), "rule_name": rule.__name__},
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
                 json.dumps(
                     [{"step": "审核规则", "rule": f.rule_id, "details": f.details}],
                     ensure_ascii=False, default=str,
                 ),
                 json.dumps(_finding_sources(conn, project_id, f), ensure_ascii=False, default=str),
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
