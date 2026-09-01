"""异常检测引擎：运行全部规则 → 落库 anomalies + evidence。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from jiadun.core.anomalies.coverage import (
    coverage_from_values,
    record_detection_run,
)
from jiadun.core.anomalies.rules import Finding
from jiadun.core.contracts import run_contract
from jiadun.core.evidence import evidence as evidence_api
from jiadun.core.evidence import finding_lifecycle
from jiadun.core.labels import direction_label


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
    """运行异常检测并单独记录覆盖率。

    ``rule_error`` 只作为兼容调用方的技术 Finding 返回，不写入普通
    ``anomalies`` 业务问题表；失败规则、失败原因和整体状态写入
    ``detection_runs`` 及一条技术证据。这样“有发现项”不会被误读成“全量
    规则已通过”，而现有调用方仍能看到具体失败信息。
    """
    active_contract = run_contract.ensure_run_contract(conn, project_id)
    findings: list[Finding] = []
    technical_findings: list[Finding] = []
    if rules is None:
        from jiadun.core.anomalies import catalog

        selected_rules = list(catalog.enabled_rule_functions(conn, project_id))
    else:
        selected_rules = list(rules)
    expected = [getattr(rule, "__name__", rule.__class__.__name__) for rule in selected_rules]
    executed: list[str] = []
    failed: dict[str, str] = {}
    started_at = datetime.now().isoformat(timespec="seconds")
    for rule, rule_name in zip(selected_rules, expected, strict=True):
        try:
            findings.extend(rule(conn, project_id))
            executed.append(rule_name)
        except Exception as exc:  # 单条规则失败不阻断整体，但必须进入覆盖率失败集
            message = str(exc)[:1000]
            failed[rule_name] = message
            technical_findings.append(Finding(
                f"rule_error_{rule_name}", "info", "project", project_id,
                "审核规则暂时无法完成，请在高级信息中查看技术详情。",
                {
                    "technical_error": message,
                    "rule_name": rule_name,
                    "detection_mode": "technical_failure",
                    "confidence": "low",
                    "impact": "检测覆盖率不完整，不能据此宣称全量检查通过。",
                    "limitations": ["该规则未完成，相关问题可能尚未被发现。"],
                },
            ))
    now = datetime.now().isoformat(timespec="seconds")
    coverage = coverage_from_values(
        expected=expected,
        executed=executed,
        failed=failed,
        critical_failed=list(failed),
    )
    # 在清理同一运行的自动缓存前，保留相同 fingerprint 的历史处理摘要。
    # 新 Finding 仍从“新发现”开始，历史状态只作为参考，绝不自动关闭。
    repeated_history: dict[str, list[dict]] = {}
    fingerprints = {
        finding.fingerprint for finding in findings if finding.fingerprint
    }
    if fingerprints:
        placeholders = ",".join("?" for _ in fingerprints)
        history_rows = conn.execute(
            f"""SELECT id, finding_id, fingerprint, status, lifecycle_status,
                       resolved_note, created_at, run_signature, run_id
                FROM anomalies
                WHERE project_id=? AND fingerprint IN ({placeholders})
                ORDER BY id""",
            (project_id, *sorted(fingerprints)),
        ).fetchall()
        for row in history_rows:
            repeated_history.setdefault(row["fingerprint"], []).append({
                "anomaly_id": int(row["id"]),
                "finding_id": row["finding_id"],
                "legacy_status": row["status"],
                "lifecycle_status": row["lifecycle_status"] if "lifecycle_status" in row.keys() else None,
                "reason": row["resolved_note"],
                "created_at": row["created_at"],
                "run_signature": row["run_signature"],
                "run_id": row["run_id"] if "run_id" in row.keys() else None,
            })
    with run_contract._transaction(conn, "run_anomalies"):
        # 技术失败证据挂在 detection_runs.metadata_json 而不在 anomalies
        # 表中，不能只靠 Finding 历史化覆盖。每次新检测开始时先把旧的
        # detection_failure Evidence 移出 current；失败重跑也会留下独立
        # 的历史事件，最新一条才代表本次覆盖率状态。
        old_failure_evidence_ids = {
            int(row["id"])
            for row in conn.execute(
                """SELECT id FROM evidence
                   WHERE project_id=? AND kind='detection_failure'
                     AND scope<>'historical'""",
                (project_id,),
            ).fetchall()
        }
        evidence_api.mark_historical(
            conn,
            project_id,
            old_failure_evidence_ids,
            "本次异常检测已重新执行，上一轮技术失败证据保留为历史",
            actor="system",
            commit=False,
        )
        # 每次检测都是一个新的不可变检测快照。旧自动 Finding 无论本次是否
        # 仍然命中，都必须保留为历史，不能 DELETE 后丢失关闭/复核轨迹；新
        # Finding 通过 repeat_history_json 引用这些历史行，但不得继承其状态。
        old_rows = conn.execute(
            """SELECT id, finding_id, fingerprint, lifecycle_status, status,
                      evidence_id, run_signature, run_id
            FROM anomalies
            WHERE project_id=? AND detection_mode IN ('automated', 'technical_failure')
                 AND rule_id <> 'contract_risk'
                 AND (run_signature IS NOT NULL OR run_id IS NOT NULL)
                 AND COALESCE(lifecycle_status, 'new') <> 'historical'""",
            (project_id,),
        ).fetchall()
        old_evidence_ids = {
            int(row["evidence_id"])
            for row in old_rows
            if row["evidence_id"] is not None
        }
        evidence_api.mark_historical(
            conn,
            project_id,
            old_evidence_ids,
            "本次异常检测已生成新的快照，旧 Finding 保留为历史，不参与当前结论",
            actor="system",
            commit=False,
        )
        historical_at = now
        for row in old_rows:
            before_status = finding_lifecycle.lifecycle_status(row)
            conn.execute(
                """UPDATE anomalies
                   SET status='stale', lifecycle_status='historical',
                       resolved_note=COALESCE(
                           resolved_note,
                           '本次异常检测已生成新的快照，旧 Finding 保留为历史'
                       ), lifecycle_updated_at=?, lifecycle_updated_by='system'
                   WHERE id=? AND project_id=?""",
                (historical_at, int(row["id"]), project_id),
            )
            conn.execute(
                """INSERT INTO finding_status_events(
                       project_id, anomaly_id, finding_id, fingerprint,
                       before_status, after_status, reason, actor, occurred_at,
                       run_signature, run_id, evidence_id, audit_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    project_id, int(row["id"]), row["finding_id"], row["fingerprint"],
                    before_status, "historical",
                    "本次异常检测已生成新的快照，旧 Finding 保留为历史",
                    "system", historical_at, row["run_signature"], row["run_id"],
                    row["evidence_id"], None,
                ),
            )
        for f in findings:
            record = f.as_record()
            ev_id = evidence_api.add_evidence(
                conn,
                project_id,
                "anomaly",
                f.message,
                steps=[{
                    "step": "审核规则",
                    "rule": f.rule_id,
                    "finding_id": f.finding_id,
                    "fingerprint": f.fingerprint,
                    "details": f.details,
                    "impact": f.impact,
                    "limitations": f.limitations,
                    "recommendation": f.recommendation,
                }],
                sources=_finding_sources(conn, project_id, f),
                commit=False,
                run_signature=active_contract.signature,
                run_id=active_contract.run_id,
                finding_id=f.finding_id,
            )
            conn.execute(
                """INSERT INTO anomalies(
                       project_id, rule_id, severity, subject_type, subject_id,
                       evidence_id, message, status, created_at, run_signature,
                       run_id,
                       finding_id, fingerprint, confidence, detection_mode,
                       raw_values_json, normalized_values_json, impact,
                       limitations_json, recommendation, suppression_reason,
                       lifecycle_status, repeat_history_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    project_id, f.rule_id, f.severity, f.subject_type, f.subject_id,
                    ev_id, f.message, "open", now, active_contract.signature, active_contract.run_id,
                    record["finding_id"], record["fingerprint"], record["confidence"],
                    record["detection_mode"],
                    json.dumps(record["raw_values"], ensure_ascii=False, default=str),
                    json.dumps(record["normalized_values"], ensure_ascii=False, default=str),
                    record["impact"],
                    json.dumps(record["limitations"], ensure_ascii=False, default=str),
                    record["recommendation"], record["suppression_reason"],
                    "new",
                    json.dumps(
                        repeated_history.get(record["fingerprint"], []),
                        ensure_ascii=False,
                        default=str,
                    ),
                ),
            )
        technical_evidence_id = None
        if failed:
            technical_evidence_id = evidence_api.add_evidence(
                conn,
                project_id,
                "detection_failure",
                "部分审核规则未完成，检测覆盖率为失败。",
                steps=[{
                    "step": "检测覆盖率",
                    "status": coverage.status,
                    "expected": expected,
                    "executed": executed,
                    "failed": failed,
                    "critical_failed": list(coverage.critical_failed),
                }],
                sources=[{"run_signature": active_contract.signature}],
                commit=False,
                run_signature=active_contract.signature,
                run_id=active_contract.run_id,
            )
        record_detection_run(
            conn,
            project_id,
            coverage,
            started_at=started_at,
            completed_at=now,
            run_signature=active_contract.signature,
            run_id=active_contract.run_id,
            error_summary=(
                f"{len(failed)} 条规则失败；技术证据 Evidence ID {technical_evidence_id}"
                if failed else None
            ),
            metadata={
                "technical_evidence_id": technical_evidence_id,
                "rule_count": len(expected),
            },
            commit=False,
        )
    return [*findings, *technical_findings]


def anomaly_summary(findings: list[Finding]) -> dict:
    by_sev = {"high": 0, "medium": 0, "low": 0, "info": 0}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    return {"total": len(findings), **by_sev}
