"""UI、Excel、Word 共用的项目摘要模型。

这里集中定义“当前运行签名下看到什么”。导出层和界面层只负责排版，不再各自
重写风险、校核、待办和审批门控的统计口径。所有业务确认/审批字段保持未请求
或待人工状态，摘要不会因自动计算而升级结论。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from jiadun.core.anomalies import coverage as detection_coverage
from jiadun.core.contracts import run_contract
from jiadun.core.engine import settlement_io
from jiadun.core.parsing import import_manifest

D = Decimal


def _money(value: D | None) -> str | None:
    return str(value) if value is not None else None


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0)


@dataclass(frozen=True)
class ProjectSummary:
    project_id: int
    project_name: str
    report_date: str
    data_cutoff: str | None
    data_cutoff_status: str
    run_signature: str | None
    source_files: int
    directions: dict[str, int]
    amounts: dict[str, dict[str, Any]]
    verification: dict[str, Any]
    risk: dict[str, Any]
    pending: dict[str, Any]
    detection_coverage: dict[str, Any]
    aggregate_coverage: dict[str, Any]
    statuses: dict[str, str]
    # 运行级可用性不是某一张结果表的派生值；保留在共享摘要上，避免
    # current_scope 返回空集后把“当前不可用”误降级为 not_started。
    run_availability: dict[str, Any] = field(default_factory=lambda: {
        "available": True,
        "status": "available",
        "fail_closed": False,
        "reason": None,
        "run_signature": None,
        "persisted": None,
        "persistence": None,
        "physical_limitations": [],
        "state": None,
    })

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "report_date": self.report_date,
            "data_cutoff": self.data_cutoff,
            "data_cutoff_status": self.data_cutoff_status,
            "run_signature": self.run_signature,
            "source_files": self.source_files,
            "directions": dict(self.directions),
            "amounts": {key: dict(value) for key, value in self.amounts.items()},
            "verification": dict(self.verification),
            "risk": dict(self.risk),
            "pending": dict(self.pending),
            "detection_coverage": dict(self.detection_coverage),
            "aggregate_coverage": dict(self.aggregate_coverage),
            "statuses": dict(self.statuses),
            "run_availability": dict(self.run_availability),
        }


@dataclass(frozen=True)
class ManagementSummary:
    """面向管理层的同一摘要事实，不另外计算一套数字。"""

    project_summary: ProjectSummary

    @property
    def project_id(self) -> int:
        return self.project_summary.project_id

    def as_dict(self) -> dict[str, Any]:
        return self.project_summary.as_dict()


@dataclass(frozen=True)
class ReportModel:
    """所有输出通道共用的事实模型与门控状态。"""

    project_summary: ProjectSummary
    management_summary: ManagementSummary

    @property
    def run_signature(self) -> str | None:
        return self.project_summary.run_signature

    def as_dict(self) -> dict[str, Any]:
        summary = self.project_summary.as_dict()
        return {
            "project_summary": summary,
            "management_summary": summary,
            "run_signature": self.run_signature,
        }


def _direction_counts(conn: sqlite3.Connection, project_id: int) -> dict[str, int]:
    rows = conn.execute(
        """SELECT COALESCE(direction, 'unknown') AS direction, COUNT(*) AS n
           FROM settlement_periods WHERE project_id=? GROUP BY COALESCE(direction, 'unknown')""",
        (project_id,),
    ).fetchall()
    return {str(row["direction"]): int(row["n"]) for row in rows}


def _amounts(conn: sqlite3.Connection, project_id: int, directions: dict[str, int]) -> dict[str, dict[str, Any]]:
    """按方向调用同一 A 路径汇总；不跨方向净额或相加。"""
    from jiadun.core.engine.aggregate import aggregate_project

    result: dict[str, dict[str, Any]] = {}
    for direction in directions:
        aggs = aggregate_project(conn, project_id, direction=direction)
        available = [item.cum_amount for item in aggs if item.cum_amount is not None]
        total = sum(available, D(0)) if available else None
        result[direction] = {
            "groups": len(aggs),
            "amount": _money(total),
            "amount_status": "available" if total is not None else "not_available",
            "ok_groups": sum(1 for item in aggs if item.status == "ok"),
            "incomplete_groups": sum(1 for item in aggs if item.status == "incomplete"),
            "incomparable_groups": sum(1 for item in aggs if item.status == "incomparable"),
        }
    return result


def _verification(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    availability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    availability = availability or run_contract.current_results_available(conn, project_id)
    scope, params = run_contract.current_scope(conn, project_id, "cr")
    rows = conn.execute(
        f"""SELECT cr.verification_level, cr.status, cr.detail_rows,
                      cr.range_unproven_sheets, cr.path_a_total, cr.path_b_total,
                      cr.diff_ab, cr.control_diff
               FROM crosscheck_results cr
               WHERE cr.project_id=? AND {scope}""",
        (project_id, *params),
    ).fetchall()
    levels = {"sufficient": 0, "findings": 0, "insufficient": 0}
    for row in rows:
        levels[row["verification_level"]] = levels.get(row["verification_level"], 0) + 1
    total_periods = _count(
        conn, "SELECT COUNT(*) FROM settlement_periods WHERE project_id=?", (project_id,)
    )
    if not availability["available"]:
        status = run_contract.FAIL_CLOSED_STATUS
    elif not rows:
        status = "not_started"
    elif levels.get("insufficient", 0):
        status = "insufficient"
    elif levels.get("findings", 0):
        status = "findings"
    else:
        status = "sufficient"
    return {
        "periods_total": total_periods,
        "periods_checked": len(rows),
        "periods_unchecked": max(0, total_periods - len(rows)),
        "levels": levels,
        "range_unproven_sheets": sum(int(row["range_unproven_sheets"] or 0) for row in rows),
        "status": status,
        "availability_status": availability["status"],
        "current_results_available": availability["available"],
        "availability_reason": availability.get("reason"),
        "physical_limitations": list(availability.get("physical_limitations") or []),
        "paths": ["A_database", "B_raw_grid", "C_control_total"],
    }


def _risk(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    scope, params = run_contract.current_scope(conn, project_id, "a")
    rows = conn.execute(
        f"""SELECT severity, status, COUNT(*) AS n FROM anomalies a
            WHERE a.project_id=? AND {scope} GROUP BY severity, status""",
        (project_id, *params),
    ).fetchall()
    severity = {"high": 0, "medium": 0, "low": 0, "info": 0}
    pending_severity = {"high": 0, "medium": 0, "low": 0, "info": 0}
    status = {"open": 0, "deferred": 0, "processed": 0, "other": 0}
    status_counts: dict[str, int] = {}
    processed_statuses = {"verified_no_issue", "supplemented", "corrected", "resolved"}
    for row in rows:
        severity[row["severity"]] = severity.get(row["severity"], 0) + int(row["n"])
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + int(row["n"])
        if row["status"] in {"open", "deferred"}:
            pending_severity[row["severity"]] = (
                pending_severity.get(row["severity"], 0) + int(row["n"])
            )
        if row["status"] in status:
            status[row["status"]] += int(row["n"])
        elif row["status"] in processed_statuses:
            status["processed"] += int(row["n"])
        else:
            status["other"] += int(row["n"])
    status["pending"] = status["open"] + status["deferred"] + status["other"]
    return {
        "severity": severity,
        "pending_severity": pending_severity,
        "status": status,
        "status_counts": status_counts,
        "total": sum(severity.values()),
    }


def _pending(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    anomaly = _risk(conn, project_id)
    match_scope, match_params = run_contract.current_scope(conn, project_id, "m")
    pending_matches = _count(
        conn,
        f"SELECT COUNT(*) FROM matches m WHERE m.project_id=? AND {match_scope} AND m.status='pending'",
        (project_id, *match_params),
    )
    manifest = import_manifest.manifest_summary(conn, project_id)
    aggregate_coverage = detection_coverage.coverage_summary(
        conn, project_id, run_kind=detection_coverage.AGGREGATE_VALIDATION
    )
    return {
        "sheets": settlement_io.pending_sheet_count(conn, project_id),
        "matches": pending_matches,
        "anomalies": anomaly["status"]["pending"],
        "high_risk": anomaly["pending_severity"]["high"],
        "manifest_status": manifest["status"],
        "manifest_missing": manifest["missing"],
        "manifest_mismatch": manifest["mismatch"],
        "manifest_ambiguous": manifest["ambiguous"],
        "manifest_unbound": manifest["unbound"],
        "manifest_authoritative": manifest["authoritative"],
        "aggregate_coverage_status": aggregate_coverage["status"],
        "aggregate_coverage_failed": aggregate_coverage["failed_count"],
        "aggregate_coverage_skipped": aggregate_coverage["skipped_count"],
    }


def _statuses(
    verification: dict[str, Any],
    risk: dict[str, Any],
    pending: dict[str, Any],
    coverage: dict[str, Any],
    aggregate_coverage: dict[str, Any],
    source_files: int,
    period_count: int,
    run_availability: dict[str, Any] | None = None,
) -> dict[str, str]:
    unavailable = bool(
        run_availability is not None and not run_availability.get("available", True)
    )
    failed = (
        verification["status"] in {"failed", run_contract.FAIL_CLOSED_STATUS}
        or coverage["status"] in {"failed", run_contract.FAIL_CLOSED_STATUS}
        or aggregate_coverage["status"] in {"failed", run_contract.FAIL_CLOSED_STATUS}
    )
    if unavailable or failed:
        automatic = "failed"
    elif not source_files or not period_count:
        automatic = "not_started"
    elif (
        coverage["status"] != "complete"
        or aggregate_coverage["status"] != "complete"
        or verification["status"] == "not_started"
        or verification["status"] in {"findings", "insufficient"}
        or verification.get("periods_unchecked", 0) > 0
    ):
        automatic = "partial"
    else:
        automatic = "complete"
    human = "pending" if unavailable or any((
        pending["sheets"], pending["matches"], pending["anomalies"],
        pending["manifest_status"] in {"incomplete", "mismatch"},
        verification["status"] in {"insufficient", "findings"},
    )) else "not_started" if not period_count else "pending"
    return {
        "automatic_analysis": automatic,
        "human_review": human,
        "business_confirmation": "not_requested",
        "approval": "not_requested",
    }


def build_project_summary(conn: sqlite3.Connection, project_id: int) -> ProjectSummary:
    project = conn.execute(
        "SELECT name FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    project_name = project["name"] if project else "未命名项目"
    source_files = _count(conn, "SELECT COUNT(*) FROM source_files WHERE project_id=?", (project_id,))
    directions = _direction_counts(conn, project_id)
    run_availability = run_contract.current_results_available(conn, project_id)
    verification = _verification(conn, project_id, availability=run_availability)
    risk = _risk(conn, project_id)
    pending = _pending(conn, project_id)
    coverage = detection_coverage.coverage_summary(conn, project_id)
    aggregate_coverage = detection_coverage.coverage_summary(
        conn, project_id, run_kind=detection_coverage.AGGREGATE_VALIDATION
    )
    period_count = sum(directions.values())
    return ProjectSummary(
        project_id=int(project_id),
        project_name=project_name,
        report_date=datetime.now().isoformat(timespec="seconds"),
        # 导入时间不等于业务数据截止日；没有明确截止字段时保持未知。
        data_cutoff=None,
        data_cutoff_status="not_available",
        run_signature=run_availability.get("run_signature"),
        source_files=source_files,
        directions=directions,
        amounts=_amounts(conn, project_id, directions),
        verification=verification,
        risk=risk,
        pending=pending,
        detection_coverage=coverage,
        aggregate_coverage=aggregate_coverage,
        statuses=_statuses(
            verification,
            risk,
            pending,
            coverage,
            aggregate_coverage,
            source_files,
            period_count,
            run_availability,
        ),
        run_availability=run_availability,
    )


def build_report_model(conn: sqlite3.Connection, project_id: int) -> ReportModel:
    summary = build_project_summary(conn, project_id)
    return ReportModel(
        project_summary=summary,
        management_summary=ManagementSummary(project_summary=summary),
    )
