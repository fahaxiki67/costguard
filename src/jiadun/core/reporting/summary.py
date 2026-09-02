"""UI、Excel、Word 共用的项目摘要模型。

这里集中定义“当前运行签名下看到什么”。导出层和界面层只负责排版，不再各自
重写风险、校核、待办和审批门控的统计口径。所有业务确认/审批字段保持未请求
或待人工状态，摘要不会因自动计算而升级结论。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from jiadun.core.anomalies import coverage as detection_coverage
from jiadun.core.contracts import run_contract
from jiadun.core.engine import settlement_io
from jiadun.core.engine.money import NotANumberError, to_decimal
from jiadun.core.evidence import finding_lifecycle
from jiadun.core.parsing import import_manifest
from jiadun.core.reporting import state as reporting_state

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
    run_id: str | None
    source_files: int
    directions: dict[str, int]
    amounts: dict[str, dict[str, Any]]
    verification: dict[str, Any]
    risk: dict[str, Any]
    pending: dict[str, Any]
    detection_coverage: dict[str, Any]
    aggregate_coverage: dict[str, Any]
    statuses: dict[str, Any]
    # P2-03/P2-01 的资产状态只读汇总；版本快照和历史单价不会改变当前
    # 结论，也不能把历史结果重新纳入 current_scope。将其放进共享摘要，
    # 让工作台、Excel 和 Word 使用同一口径展示“当前版本/是否已关闭/可用
    # 历史单价条数”，避免各输出通道各自查询后产生漂移。
    version_chain: dict[str, Any] = field(default_factory=dict)
    historical_price_assets: dict[str, Any] = field(default_factory=dict)
    # 运行级可用性不是某一张结果表的派生值；保留在共享摘要上，避免
    # current_scope 返回空集后把“当前不可用”误降级为 not_started。
    run_availability: dict[str, Any] = field(default_factory=lambda: {
        "available": True,
        "status": "available",
        "fail_closed": False,
        "reason": None,
        "run_signature": None,
        "run_id": None,
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
            "run_id": self.run_id,
            "source_files": self.source_files,
            "directions": dict(self.directions),
            "amounts": {key: dict(value) for key, value in self.amounts.items()},
            "verification": dict(self.verification),
            "risk": dict(self.risk),
            "pending": dict(self.pending),
            "detection_coverage": dict(self.detection_coverage),
            "aggregate_coverage": dict(self.aggregate_coverage),
            "statuses": dict(self.statuses),
            "version_chain": dict(self.version_chain),
            "historical_price_assets": dict(self.historical_price_assets),
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

    @property
    def run_id(self) -> str | None:
        return self.project_summary.run_id

    def as_dict(self) -> dict[str, Any]:
        summary = self.project_summary.as_dict()
        return {
            "project_summary": summary,
            "management_summary": summary,
            "run_signature": self.run_signature,
            "run_id": self.run_id,
        }


def _direction_counts(conn: sqlite3.Connection, project_id: int) -> dict[str, int]:
    rows = conn.execute(
        """SELECT COALESCE(direction, 'unknown') AS direction, COUNT(*) AS n
           FROM settlement_periods WHERE project_id=? GROUP BY COALESCE(direction, 'unknown')""",
        (project_id,),
    ).fetchall()
    return {str(row["direction"]): int(row["n"]) for row in rows}


def _json_contains_ref(value: str | None, key: str, expected: int) -> bool:
    """判断 Evidence/Audit JSON 是否明确指向指定对象。"""
    try:
        payload = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return False
    entries = payload if isinstance(payload, list) else [payload]
    return any(
        isinstance(entry, dict)
        and entry.get(key) is not None
        and str(entry.get(key)) == str(expected)
        for entry in entries
    )


def _json_contains_version_ref(value: str | None, version_id: int) -> bool:
    return _json_contains_ref(value, "version_id", version_id)


def _version_evidence_chain(
    conn: sqlite3.Connection,
    project_id: int,
    version: sqlite3.Row,
) -> tuple[bool, tuple[str, ...]]:
    """验证版本责任记录确实属于该 version_id，而非同运行错配。"""
    reasons: list[str] = []
    version_id = int(version["id"])
    evidence = (
        conn.execute(
            """SELECT project_id, kind, scope, run_id, run_signature,
                              steps_json, sources_json
                 FROM evidence WHERE id=?""",
            (version["evidence_id"],),
        ).fetchone()
        if version["evidence_id"] is not None else None
    )
    if (
        evidence is None
        or int(evidence["project_id"]) != int(project_id)
        or evidence["kind"] != "project_version"
        or evidence["scope"] not in {"current", "human", "historical"}
        or evidence["run_id"] != version["run_id"]
        or evidence["run_signature"] != version["run_signature"]
    ):
        reasons.append("version_evidence_mismatch")
    else:
        if (
            version["version_kind"] == "final_approval"
            and evidence["scope"] not in {"current", "human"}
        ):
            reasons.append("final_version_evidence_not_current")
        if not _json_contains_version_ref(evidence["sources_json"], version_id):
            reasons.append("version_evidence_source_target_mismatch")
        if not _json_contains_version_ref(evidence["steps_json"], version_id):
            reasons.append("version_evidence_step_target_mismatch")
    audit = (
        conn.execute(
            """SELECT project_id, action, target, run_id, run_signature, after_json
                 FROM audit_log WHERE id=?""",
            (version["audit_id"],),
        ).fetchone()
        if version["audit_id"] is not None else None
    )
    if (
        audit is None
        or int(audit["project_id"]) != int(project_id)
        or audit["action"] != "create_project_version"
        or audit["target"] != f"project_version:{version_id}"
        or audit["run_id"] != version["run_id"]
        or audit["run_signature"] != version["run_signature"]
        or not _json_contains_version_ref(audit["after_json"], version_id)
    ):
        reasons.append("version_audit_target_mismatch")
    return not reasons, tuple(dict.fromkeys(reasons))


def _closure_evidence_chain(
    conn: sqlite3.Connection,
    project_id: int,
    closure: sqlite3.Row,
) -> tuple[bool, tuple[str, ...]]:
    """验证项目关闭记录的 Evidence/Audit 也指向同一关闭事实。"""
    reasons: list[str] = []
    closure_id = int(closure["id"])
    evidence = (
        conn.execute(
            """SELECT project_id, kind, scope, run_id, run_signature,
                              steps_json, sources_json
                 FROM evidence WHERE id=?""",
            (closure["evidence_id"],),
        ).fetchone()
        if closure["evidence_id"] is not None else None
    )
    if (
        evidence is None
        or int(evidence["project_id"]) != int(project_id)
        or evidence["kind"] != "project_closure"
        or evidence["scope"] not in {"current", "human", "historical"}
        or evidence["run_id"] != closure["run_id"]
        or evidence["run_signature"] != closure["run_signature"]
    ):
        reasons.append("closure_evidence_mismatch")
    else:
        if not _json_contains_ref(evidence["sources_json"], "closure_id", closure_id):
            reasons.append("closure_evidence_source_target_mismatch")
        if not _json_contains_ref(
            evidence["sources_json"], "final_version_id", int(closure["final_version_id"])
        ):
            reasons.append("closure_evidence_final_version_missing")
        if not _json_contains_ref(evidence["steps_json"], "closure_id", closure_id):
            reasons.append("closure_evidence_step_target_mismatch")
        if not _json_contains_ref(
            evidence["steps_json"], "final_version_id", int(closure["final_version_id"])
        ):
            reasons.append("closure_evidence_step_final_version_missing")
    audit = (
        conn.execute(
            """SELECT project_id, action, target, run_id, run_signature, after_json
                 FROM audit_log WHERE id=?""",
            (closure["audit_id"],),
        ).fetchone()
        if closure["audit_id"] is not None else None
    )
    if (
        audit is None
        or int(audit["project_id"]) != int(project_id)
        or audit["action"] != "close_project_for_history"
        or audit["target"] != f"project_closure:{closure_id}"
        or audit["run_id"] != closure["run_id"]
        or audit["run_signature"] != closure["run_signature"]
        or not _json_contains_ref(audit["after_json"], "final_version_id", int(closure["final_version_id"]))
        or not _json_contains_ref(audit["after_json"], "closure_id", closure_id)
    ):
        reasons.append("closure_audit_target_mismatch")
    return not reasons, tuple(dict.fromkeys(reasons))


def _version_snapshot_chain(
    conn: sqlite3.Connection,
    project_id: int,
    version_id: int,
) -> tuple[bool, tuple[str, ...]]:
    """把版本 Evidence 链与不可变快照完整性合并为一个读取闸门。

    ``_version_evidence_chain`` 只能证明责任记录指向了版本本身，不能
    发现版本行数/hash 与实际快照已经分离。摘要、工作台和导出必须共用
    与版本比较相同的完整性规则，否则直接追加一条版本明细即可让 UI
    显示“责任链有效”而比较层才降级，形成跨层 false-green。
    """
    from jiadun.core.versions.project import _version_integrity, get_project_version

    version = get_project_version(conn, int(project_id), int(version_id))
    if version is None:
        return False, ("version_missing",)
    valid, reasons = _version_integrity(conn, version)
    return valid, tuple(reasons)


def _version_chain(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """只读汇总项目版本链，不把历史版本混入当前运行结果。"""
    current = run_contract.get_current_contract(conn, project_id)
    rows = conn.execute(
        """SELECT id, version_no, version_kind, title, description,
                  snapshot_sha256, item_count, created_by, created_at, reason,
                  run_id, run_signature, evidence_id, audit_id
             FROM project_versions
            WHERE project_id=? ORDER BY version_no DESC, id DESC""",
        (int(project_id),),
    ).fetchall()
    latest = None
    if rows:
        row = rows[0]
        latest_evidence_valid, latest_evidence_reasons = _version_evidence_chain(
            conn, int(project_id), row
        )
        latest_snapshot_valid, latest_snapshot_reasons = _version_snapshot_chain(
            conn, int(project_id), int(row["id"])
        )
        latest_chain_reasons = tuple(dict.fromkeys(
            (*latest_evidence_reasons, *latest_snapshot_reasons)
        ))
        latest = {
            "id": int(row["id"]),
            "version_no": int(row["version_no"]),
            "version_kind": str(row["version_kind"]),
            "title": str(row["title"]),
            "item_count": int(row["item_count"] or 0),
            "created_by": str(row["created_by"]),
            "created_at": str(row["created_at"]),
            "reason": str(row["reason"]),
            "snapshot_sha256": str(row["snapshot_sha256"]),
            "run_id": row["run_id"],
            "run_signature": row["run_signature"],
            "evidence_id": int(row["evidence_id"]) if row["evidence_id"] is not None else None,
            "audit_id": int(row["audit_id"]) if row["audit_id"] is not None else None,
            "chain_valid": bool(latest_evidence_valid and latest_snapshot_valid),
            "chain_reason_codes": list(latest_chain_reasons),
            "is_current_run": bool(current and row["run_id"] == current.run_id
                                    and row["run_signature"] == current.signature),
        }
    closure = conn.execute(
        """SELECT id, final_version_id, status, region, project_type,
                  observed_at, closed_by, closed_at, run_id, run_signature,
                  evidence_id, audit_id
             FROM project_closures WHERE project_id=? ORDER BY id DESC LIMIT 1""",
        (int(project_id),),
    ).fetchone()
    closure_payload = None
    if closure is not None:
        closure_chain_valid, closure_chain_reasons = _closure_evidence_chain(
            conn, int(project_id), closure
        )
        closure_payload = {
            "id": int(closure["id"]),
            "final_version_id": int(closure["final_version_id"]),
            "status": str(closure["status"]),
            "region": str(closure["region"] or ""),
            "project_type": str(closure["project_type"] or ""),
            "observed_at": closure["observed_at"],
            "closed_by": str(closure["closed_by"]),
            "closed_at": str(closure["closed_at"]),
            "run_id": closure["run_id"],
            "run_signature": closure["run_signature"],
            "evidence_id": int(closure["evidence_id"]) if closure["evidence_id"] is not None else None,
            "audit_id": int(closure["audit_id"]) if closure["audit_id"] is not None else None,
            "chain_valid": closure_chain_valid,
            "chain_reason_codes": list(closure_chain_reasons),
        }
    final = conn.execute(
        """SELECT id, version_no, version_kind, title, run_id, run_signature, evidence_id, audit_id
             FROM project_versions
            WHERE project_id=? AND version_kind='final_approval'
            ORDER BY version_no DESC, id DESC LIMIT 1""",
        (int(project_id),),
    ).fetchone()
    final_version = None
    if final is not None:
        final_evidence_valid, final_evidence_reasons = _version_evidence_chain(
            conn, int(project_id), final
        )
        final_snapshot_valid, final_snapshot_reasons = _version_snapshot_chain(
            conn, int(project_id), int(final["id"])
        )
        final_chain_reasons = tuple(dict.fromkeys(
            (*final_evidence_reasons, *final_snapshot_reasons)
        ))
        final_version = {
            "id": int(final["id"]),
            "version_no": int(final["version_no"]),
            "title": str(final["title"]),
            "is_current_run": bool(current and final["run_id"] == current.run_id
                                    and final["run_signature"] == current.signature),
            "evidence_complete": bool(final_evidence_valid and final_snapshot_valid),
            "chain_reason_codes": list(final_chain_reasons),
        }
    closure_matches_final = bool(
        closure_payload
        and final_version
        and closure_payload["final_version_id"] == final_version["id"]
    )
    closure_chain_valid = bool(closure_payload and closure_payload["chain_valid"])
    closure_valid = bool(
        closure_payload
        and closure_payload["status"] == "closed"
        and closure_matches_final
        and closure_chain_valid
        and final_version
        and final_version["evidence_complete"]
        and current
        and closure_payload.get("run_id") == current.run_id
        and final_version.get("is_current_run")
    )
    if not rows:
        status = "not_started"
    elif closure_valid:
        status = "closed"
    elif final_version and final_version["evidence_complete"]:
        status = "final_approval_recorded"
    else:
        status = "in_progress"
    return {
        "status": status,
        "version_count": len(rows),
        "latest": latest,
        "final_approval": final_version,
        "closure": closure_payload,
        "history_eligible": bool(
            closure_payload
            and closure_payload["status"] == "closed"
            and closure_valid
        ),
    }


def _historical_price_assets(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """汇总已沉淀的历史单价资产；来源不完整的旧快照不参与当前统计。"""
    from jiadun.core.pricing.history import list_historical_unit_prices

    records = list_historical_unit_prices(
        conn, source_project_id=int(project_id), include_revoked=True
    )
    active = sum(1 for item in records if item.status == "active")
    revoked = sum(1 for item in records if item.status == "revoked")
    invalid = sum(1 for item in records if item.status == "invalid")
    return {
        "status": "available" if active else "not_available",
        "review_only": True,
        "total": len(records),
        "active": active,
        "revoked": revoked,
        "invalid": invalid,
        "message": (
            "历史单价仅用于提示复核，不直接认定当前价格错误"
            + (f"；发现 {invalid} 条来源不一致历史快照，仅保留档案" if invalid else "")
            if active else (
                f"发现 {invalid} 条来源不一致历史快照，仅保留档案"
                if invalid else "尚未从已关闭项目沉淀可用历史单价"
            )
        ),
    }


def _amounts(
    conn: sqlite3.Connection,
    project_id: int,
    directions: dict[str, int],
    *,
    read_only: bool = False,
) -> dict[str, dict[str, Any]]:
    """按方向调用同一 A 路径汇总；不跨方向净额或相加。"""
    from jiadun.core.engine.aggregate import aggregate_project

    result: dict[str, dict[str, Any]] = {}
    for direction in directions:
        aggs = aggregate_project(
            conn, project_id, direction=direction, persist_derived_flags=not read_only
        )
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


def _evidence_refers_to_period(
    sources_json: str | None,
    period_id: int,
    period_no: int | None,
    direction: str | None = None,
) -> bool:
    """确认 Evidence 的结构化来源确实指向当前期次。

    运行身份只能证明 Evidence 属于当前合同，不能证明它就是该期 A/B/C
    校核或 C 控制来源。兼容旧格式时同时接受 ``period_id`` 和 ``period``；
    无法解析或没有期次定位的 Evidence 统一按不完整处理。对当前期次的
    运行摘要还必须同时命中方向；同一期的“对上”证据不能支撑“对下”结果。
    """
    try:
        payload = json.loads(sources_json or "")
    except (TypeError, json.JSONDecodeError):
        return False
    entries = payload if isinstance(payload, list) else [payload]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            if entry.get("period_id") is not None and int(entry["period_id"]) == int(period_id):
                if direction is None or str(entry.get("direction") or "unknown") == str(direction or "unknown"):
                    return True
        except (TypeError, ValueError):
            pass
        if period_no is not None:
            try:
                if entry.get("period") is not None and int(entry["period"]) == int(period_no):
                    if direction is None or str(entry.get("direction") or "unknown") == str(direction or "unknown"):
                        return True
            except (TypeError, ValueError):
                pass
    return False


def _evidence_refers_to_sheet(
    sources_json: str | None,
    *,
    sheet_id: int,
    period_id: int,
    period_no: int | None,
    direction: str | None = None,
) -> bool:
    """确认覆盖证明 Evidence 同时定位到对应期次和 Sheet。

    覆盖证明是逐 Sheet 事实。仅命中当前 Run Contract、甚至仅携带期次
    的 Evidence，都不能证明当前期次的每个 Sheet 已被完整处理；缺少结构化
    Sheet/期次定位时按不完整处理，避免把一份通用说明冒充范围证明。
    """
    try:
        payload = json.loads(sources_json or "")
    except (TypeError, json.JSONDecodeError):
        return False
    entries = payload if isinstance(payload, list) else [payload]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            if entry.get("sheet_id") is None or int(entry["sheet_id"]) != int(sheet_id):
                continue
        except (TypeError, ValueError):
            continue
        period_match = False
        try:
            period_match = (
                entry.get("period_id") is not None
                and int(entry["period_id"]) == int(period_id)
            )
        except (TypeError, ValueError):
            period_match = False
        if not period_match and period_no is not None:
            try:
                period_match = (
                    entry.get("period") is not None
                    and int(entry["period"]) == int(period_no)
                )
            except (TypeError, ValueError):
                period_match = False
        if period_match and (
            direction is None
            or str(entry.get("direction") or "unknown") == str(direction or "unknown")
        ):
            return True
    return False


def _evidence_contains_control_source(
    sources_json: str | None,
    *,
    period_id: int,
    period_no: int | None,
    direction: str | None,
) -> bool:
    """验证 C 控制来源包含文件、Sheet、行列、原文和选择规则。

    ``line_item_source`` 的来源条目同时保留兼容的文件/Sheet 名称和新式
    数值 ID。只携带 ``period_id`` 的摘要不能证明究竟取了哪一个控制单元格，
    因此不允许 sufficient 结果越过项目闸门。
    """
    try:
        payload = json.loads(sources_json or "")
    except (TypeError, json.JSONDecodeError):
        return False
    entries = payload if isinstance(payload, list) else [payload]
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("field") != "amount":
            continue
        period_match = False
        try:
            period_match = (
                entry.get("period_id") is not None
                and int(entry["period_id"]) == int(period_id)
            )
        except (TypeError, ValueError):
            period_match = False
        if not period_match and period_no is not None:
            try:
                period_match = (
                    entry.get("period") is not None
                    and int(entry["period"]) == int(period_no)
                )
            except (TypeError, ValueError):
                period_match = False
        if not period_match:
            continue
        if direction is not None and str(entry.get("direction") or "unknown") != str(direction or "unknown"):
            continue
        # 名称只是展示文本，不能证明来源属于当前项目。当前运行的正式 C
        # 控制来源必须带可回读的 file_id/sheet_id；项目、期次、方向和文件
        # 链在 _control_source_consistency 中继续按数据库关系校验。
        has_file = entry.get("file_id") is not None
        has_sheet = entry.get("sheet_id") is not None
        has_row = entry.get("row") is not None
        has_col = entry.get("col") is not None or entry.get("amount_col") is not None
        has_raw = entry.get("raw_value") is not None or entry.get("raw") is not None
        has_rule = bool(str(entry.get("selection_rule") or "").strip())
        if has_file and has_sheet and has_row and has_col and has_raw and has_rule:
            return True
    return False


def _crosscheck_path_scope_gaps(steps_json: str | None) -> list[str]:
    """验证 A/B 路径 Evidence 是否记录了真实来源和过滤范围。"""
    try:
        payload = json.loads(steps_json or "")
    except (TypeError, json.JSONDecodeError):
        return ["期次校核 Evidence 缺少可解析的路径范围"]
    # 旧版/人工构造的 Evidence 可能把步骤保存在列表中，而正式交叉校核
    # 会把完整结果快照写成对象。两种形态都只接受明确的 path_scopes，
    # 不因容器形状差异放松来源、行范围和独立性检查。
    if isinstance(payload, list):
        candidates = [
            item for item in payload
            if isinstance(item, dict)
            and ("path_scopes" in item or "path_independence_level" in item)
        ]
        payload = candidates[-1] if candidates else None
    if not isinstance(payload, dict):
        return ["期次校核 Evidence 路径范围不是对象"]
    scopes = payload.get("path_scopes")
    if not isinstance(scopes, dict):
        return ["期次校核 Evidence 缺少 A/B/C 路径范围"]
    gaps: list[str] = []
    expected_sources = {"path_a": "line_items", "path_b": "raw_cells"}
    for key, source_kind in expected_sources.items():
        scope = scopes.get(key)
        if not isinstance(scope, dict):
            gaps.append(f"{key} 路径范围缺失")
            continue
        if scope.get("source_kind") != source_kind:
            gaps.append(f"{key} 路径来源不是 {source_kind}")
        if not str(scope.get("selection_rule") or "").strip():
            gaps.append(f"{key} 路径缺少行集选择规则")
        filters = scope.get("filters")
        if not isinstance(filters, list) or not filters:
            gaps.append(f"{key} 路径缺少过滤条件")
        sheets = scope.get("sheets")
        if not isinstance(sheets, list) or not sheets:
            gaps.append(f"{key} 路径缺少文件/Sheet/行范围")
            continue
        for item in sheets:
            if not isinstance(item, dict):
                gaps.append(f"{key} 路径 Sheet 范围条目不是对象")
                continue
            for field_name in ("file_id", "sheet_id", "file_name", "sheet_name"):
                if item.get(field_name) is None or not str(item.get(field_name)).strip():
                    gaps.append(f"{key} 路径缺少 {field_name}")
            if item.get("row_start") is None or item.get("row_end") is None:
                gaps.append(f"{key} 路径缺少物理行范围")
    path_level = str(payload.get("path_independence_level") or "unknown")
    if path_level != "source_independent_raw_scan":
        gaps.append(f"A/B 本次路径独立性为 {path_level}，不是独立 raw_cells 扫描")
    return gaps


def _control_source_consistency(
    conn: sqlite3.Connection,
    project_id: int,
    result_row: sqlite3.Row,
    evidence_row: sqlite3.Row,
) -> list[str]:
    """回读 C 控制来源的单元格/清单行，并重算控制值。

    ``kind``、方向和定位字段只能证明“指向了某处”，不能证明 Evidence
    里的金额仍是该处的原始值。这里以 Evidence 来源条目为输入，逐项读取
    raw_cells 或 line_items，再与 raw_subtotal、A 路径和存储的 control_diff
    勾稽；任何直接 SQL 伪造值都会降级为不充分。
    """
    try:
        payload = json.loads(evidence_row["sources_json"] or "")
    except (TypeError, json.JSONDecodeError):
        return ["C 控制来源 Evidence JSON 无法解析"]
    entries = payload if isinstance(payload, list) else [payload]
    selected: list[dict[str, Any]] = []
    period_id = int(result_row["period_id"])
    period_no = result_row["period_no"]
    direction = str(result_row["direction"] or "unknown")
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("field") != "amount":
            continue
        period_match = False
        try:
            period_match = (
                entry.get("period_id") is not None
                and int(entry["period_id"]) == period_id
            )
        except (TypeError, ValueError):
            pass
        if not period_match and period_no is not None:
            try:
                period_match = (
                    entry.get("period") is not None
                    and int(entry["period"]) == int(period_no)
                )
            except (TypeError, ValueError):
                period_match = False
        if period_match and str(entry.get("direction") or "unknown") == direction:
            selected.append(entry)
    if not selected:
        return ["C 控制来源 Evidence 缺少对应期次/方向金额条目"]

    gaps: list[str] = []
    values: list[D] = []
    for entry in selected:
        expected = _decimal_value(entry.get("normalized_value"))
        if expected is None:
            expected = _decimal_value(entry.get("value"))
        raw_expected = _decimal_value(entry.get("raw_value"))
        if expected is None or raw_expected is None:
            gaps.append("C 控制来源金额缺失或无法解析")
            continue
        if expected != raw_expected:
            gaps.append("C 控制来源原文金额与规范金额不一致")
        source_value: D | None = None
        sheet_id = entry.get("sheet_id")
        row_no = entry.get("row")
        col_no = entry.get("col") or entry.get("amount_col")
        source_scope = None
        if sheet_id is not None:
            try:
                source_scope = conn.execute(
                    """SELECT sf.id AS file_id, sf.project_id AS file_project_id,
                              pb.file_id AS batch_file_id, rs.id AS sheet_id,
                              rs.period_id AS sheet_period_id,
                              COALESCE(sp.project_id, sf.project_id) AS period_project_id,
                              COALESCE(sp.direction, 'unknown') AS sheet_direction,
                              rs.n_rows AS sheet_n_rows, rs.n_cols AS sheet_n_cols
                         FROM raw_sheets rs
                         JOIN parse_batches pb ON pb.id=rs.batch_id
                         JOIN source_files sf ON sf.id=pb.file_id
                         LEFT JOIN settlement_periods sp ON sp.id=rs.period_id
                        WHERE rs.id=?""",
                    (int(sheet_id),),
                ).fetchone()
            except (TypeError, ValueError):
                source_scope = None
        if source_scope is None:
            gaps.append("C 控制来源 Sheet 不存在或无法读取来源链")
        else:
            try:
                source_file_id = int(entry.get("file_id"))
                source_sheet_id = int(sheet_id)
            except (TypeError, ValueError):
                source_file_id = source_sheet_id = None
            if (
                source_file_id is None
                or source_sheet_id is None
                or source_scope["file_id"] != source_file_id
                or source_scope["batch_file_id"] != source_file_id
                or source_scope["sheet_id"] != source_sheet_id
                or source_scope["file_project_id"] != int(project_id)
                or source_scope["period_project_id"] != int(project_id)
                or source_scope["sheet_period_id"] != period_id
                or str(source_scope["sheet_direction"] or "unknown") != direction
            ):
                gaps.append("C 控制来源文件、Sheet、项目、期次或方向不一致")
            try:
                row_int = int(row_no)
                col_int = int(col_no)
            except (TypeError, ValueError):
                row_int = col_int = None
            if (
                row_int is None
                or col_int is None
                or row_int < 1
                or col_int < 1
                or (source_scope["sheet_n_rows"] is not None and row_int > int(source_scope["sheet_n_rows"]))
                or (source_scope["sheet_n_cols"] is not None and col_int > int(source_scope["sheet_n_cols"]))
            ):
                gaps.append("C 控制来源行列超出当前 Sheet 范围")
        if sheet_id is not None and row_no is not None and col_no is not None:
            try:
                cell = conn.execute(
                    """SELECT raw_value, cached_value, is_formula FROM raw_cells
                       WHERE sheet_id=? AND row=? AND col=?""",
                    (int(sheet_id), int(row_no), int(col_no)),
                ).fetchone()
            except (TypeError, ValueError):
                cell = None
            if cell is None:
                gaps.append("C 控制来源定位的原始单元格不存在")
            else:
                actual_text = (
                    cell["cached_value"]
                    if cell["is_formula"] and cell["cached_value"] is not None
                    else cell["raw_value"]
                )
                source_value = _decimal_value(actual_text)
                if source_value is None or source_value != expected:
                    gaps.append("C 控制来源金额与原始单元格不一致")
        line_item_id = entry.get("line_item_id")
        if line_item_id is not None:
            try:
                item = conn.execute(
                    """SELECT li.amount, li.period_id, sp.period_no, sp.direction,
                              sp.project_id, li.flags_json
                         FROM line_items li
                       JOIN settlement_periods sp ON sp.id=li.period_id
                       WHERE li.id=?""",
                    (int(line_item_id),),
                ).fetchone()
            except (TypeError, ValueError):
                item = None
            if (
                item is None
                or int(item["project_id"]) != int(project_id)
                or int(item["period_id"]) != period_id
                or str(item["direction"] or "unknown") != direction
            ):
                gaps.append("C 控制来源清单行不属于当前项目期次或方向")
            else:
                item_value = _decimal_value(item["amount"])
                if item_value is None or item_value != expected:
                    gaps.append("C 控制来源金额与清单行合价不一致")
                flags = _loads_flags(item["flags_json"])
                if not bool(flags.get("subtotal")):
                    gaps.append("C 控制来源清单行不是小计/合计行")
                selection_rule = str(entry.get("selection_rule") or "")
                if "flags.grand_total=true" in selection_rule and not bool(flags.get("grand_total")):
                    gaps.append("C 控制来源选择规则要求合计级行，但清单行未标记 grand_total")
                if "页级小计" in selection_rule and bool(flags.get("grand_total")):
                    gaps.append("C 控制来源选择规则要求页级小计，但清单行标记为 grand_total")
                # line_items.flags_json 是当前可变业务投影，不能作为 C
                # 控制行身份的唯一依据。再回读同一当前运行下不可变的
                # SheetCoverageProof/RowClassification，以原始网格分类证明
                # 绑定 subtotal/grand_total；普通明细行即使被 UPDATE 成
                # subtotal=true 也不能把 C 重新刷绿。
                current_contract = run_contract.get_current_contract(conn, project_id)
                classified = None
                if current_contract is not None:
                    proof_row = conn.execute(
                        """SELECT p.id
                             FROM sheet_coverage_proofs p
                            WHERE p.project_id=? AND p.sheet_id=? AND p.period_id=?
                              AND p.run_id=? AND p.run_signature=?
                            ORDER BY p.id DESC LIMIT 1""",
                        (
                            project_id,
                            int(sheet_id) if sheet_id is not None else -1,
                            period_id,
                            current_contract.run_id,
                            current_contract.signature,
                        ),
                    ).fetchone()
                    if proof_row is not None and row_no is not None:
                        try:
                            classified = conn.execute(
                                """SELECT class_code
                                     FROM row_classifications
                                    WHERE proof_id=? AND row_number=?""",
                                (int(proof_row["id"]), int(row_no)),
                            ).fetchone()
                        except (TypeError, ValueError):
                            classified = None
                if classified is None:
                    gaps.append("C 控制来源缺少不可变逐行分类证明")
                else:
                    class_code = str(classified["class_code"] or "")
                    if "flags.grand_total=true" in selection_rule:
                        expected_classes = {"grand_total"}
                    elif "页级小计" in selection_rule:
                        expected_classes = {"subtotal"}
                    else:
                        expected_classes = {"subtotal", "grand_total"}
                    if class_code not in expected_classes:
                        gaps.append(
                            "C 控制来源不可变逐行分类与选择规则不一致"
                        )
        else:
            # 只有 raw-cell 定位、没有 line_item_id 时，无法证明该数值行确实
            # 是小计/合计控制行；名称或 selection_rule 文本不是独立分类事实。
            gaps.append("C 控制来源缺少小计/合计清单行引用")
        if source_value is None and line_item_id is None:
            gaps.append("C 控制来源缺少可回读的单元格或清单行")
        values.append(expected)

    source_total = sum(values, D("0")) if values else None
    stored_control = _decimal_value(result_row["raw_subtotal"])
    if source_total is None or stored_control is None:
        gaps.append("C 控制值缺少可复算金额")
    elif source_total != stored_control:
        gaps.append("C 控制来源合计与 C 控制值不一致")
    path_a = _decimal_value(result_row["path_a_total"])
    if path_a is None or stored_control is None or abs(path_a - stored_control) > D("0.02"):
        gaps.append("C 控制值与 A 路径金额不一致")
    stored_diff = _decimal_value(result_row["control_diff"])
    recomputed_diff = path_a - stored_control if path_a is not None and stored_control is not None else None
    if stored_diff is None or recomputed_diff is None or stored_diff != recomputed_diff:
        gaps.append("C 控制差额与 A-C 重算值不一致")
    return gaps


def _raw_source_value(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    return str(value).strip() if value is not None else ""


def _decimal_value(value: Any) -> D | None:
    if value is None or value == "":
        return None
    try:
        return to_decimal(value)
    except (NotANumberError, TypeError, ValueError):
        # Decimal 持久化值可能以科学计数法写入（例如 ``3E-12``）。
        # 这仍是 Decimal 解析，不放宽到 float；仅接受有限数，避免 NaN/Inf
        # 进入摘要比较。
        if isinstance(value, str):
            try:
                parsed = D(value.strip())
            except (InvalidOperation, TypeError, ValueError):
                return None
            return parsed if parsed.is_finite() else None
        return None


def _loads_flags(value: str | None) -> dict[str, Any]:
    """读取清单行分类标记；损坏 JSON 按空标记处理并由调用方 fail-closed。"""
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coverage_source_consistency(
    conn: sqlite3.Connection,
    proof: sqlite3.Row,
    class_rows: list[sqlite3.Row],
) -> list[str]:
    """把逐行证明回读到 raw_cells，防止伪造 proof 与伪造 row 同时变绿。"""
    sheet_id = int(proof["sheet_id"])
    prefix = f"Sheet {sheet_id}"
    header = conn.execute(
        """SELECT col_map_json FROM table_headers
            WHERE sheet_id=? ORDER BY id DESC LIMIT 1""",
        (sheet_id,),
    ).fetchone()
    if header is None:
        return [f"{prefix} 缺少表头字段映射，无法回读原始单元格"]
    try:
        col_map = json.loads(header["col_map_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return [f"{prefix} 表头字段映射无法解析"]
    if not isinstance(col_map, dict) or not col_map:
        return [f"{prefix} 表头字段映射为空"]
    try:
        merged_ranges = json.loads(proof["sheet_merged_ranges"] or "[]")
    except (TypeError, json.JSONDecodeError):
        merged_ranges = []
    from jiadun.core.parsing.header_detect import build_anchor_map

    anchors = build_anchor_map(merged_ranges if isinstance(merged_ranges, list) else [])
    cells = {
        (int(item["row"]), int(item["col"])): (
            item["cached_value"] if item["is_formula"] and item["cached_value"] is not None
            else item["raw_value"]
        )
        for item in conn.execute(
            """SELECT row, col, raw_value, cached_value, is_formula
                 FROM raw_cells WHERE sheet_id=?""",
            (sheet_id,),
        ).fetchall()
    }
    gaps: list[str] = []
    for item in class_rows:
        try:
            row_number = int(item["row_number"])
        except (TypeError, ValueError):
            continue
        try:
            raw_values = json.loads(item["raw_values_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(raw_values, dict):
            gaps.append(f"{prefix} 第{row_number}行原始值不是对象")
            continue
        for field_name, col in col_map.items():
            try:
                col_no = int(col)
            except (TypeError, ValueError):
                gaps.append(f"{prefix} 字段 {field_name} 列号非法")
                continue
            anchor = anchors.get((row_number, col_no), (row_number, col_no))
            actual = str(cells.get(anchor, "") or "").strip()
            expected = str(raw_values.get(field_name, "") or "").strip()
            if field_name in {"quantity", "unit_price", "amount", "tax_rate"}:
                actual_decimal = _decimal_value(actual)
                expected_decimal = _decimal_value(expected)
                same = (
                    actual_decimal is not None
                    and expected_decimal is not None
                    and actual_decimal == expected_decimal
                ) or (not actual and not expected)
            else:
                same = "".join(actual.split()) == "".join(expected.split())
            if not same:
                gaps.append(
                    f"{prefix} 第{row_number}行字段 {field_name} 与原始单元格不一致"
                )
    return gaps


def _coverage_semantic_consistency(
    conn: sqlite3.Connection,
    proof: sqlite3.Row,
    class_rows: list[sqlite3.Row],
) -> list[str]:
    """从不可变原始网格独立重算逐行分类，拒绝成对伪造的 proof/rows。

    只校验 ``row_classifications`` 自身的计数、金额和 ``raw_values`` 会让
    攻击者把明细行与合计行一起交换，重新生成一份内部自洽的不可变证明，
    再把 C Evidence 指向伪合计行。这里复用确定性的覆盖证明生成器，但输入
    只来自 raw_cells、raw_sheets 和已确认表头；任何 class_code、原因、金额
    或参与标志与重算结果不一致，都必须让当前项目降级。
    """
    prefix = f"Sheet {int(proof['sheet_id'])}"
    header = conn.execute(
        """SELECT header_row_lo, header_row_hi, col_map_json, confidence,
                          needs_review, data_row_start, data_row_end,
                          data_range_status, data_range_method
             FROM table_headers WHERE sheet_id=? ORDER BY id DESC LIMIT 1""",
        (int(proof["sheet_id"]),),
    ).fetchone()
    if header is None:
        return [f"{prefix} 缺少表头，无法独立复算逐行分类"]
    try:
        col_map = json.loads(header["col_map_json"] or "{}")
        header_lo = int(header["header_row_lo"])
        header_hi = int(header["header_row_hi"])
        data_start = int(header["data_row_start"])
        data_end = int(header["data_row_end"])
        confidence = float(header["confidence"])
        needs_review = bool(header["needs_review"])
        manual_range_confirmed = (
            str(header["data_range_status"] or "") == "confirmed"
            and str(header["data_range_method"] or "") == "manual_confirmation"
        )
    except (TypeError, ValueError, json.JSONDecodeError, KeyError):
        return [f"{prefix} 表头字段无法独立复算逐行分类"]
    if not isinstance(col_map, dict) or not col_map:
        return [f"{prefix} 表头字段映射为空，无法独立复算逐行分类"]

    try:
        merged_ranges = json.loads(proof["sheet_merged_ranges"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return [f"{prefix} 合并单元格范围无法独立复算逐行分类"]
    if not isinstance(merged_ranges, list):
        return [f"{prefix} 合并单元格范围不是列表"]
    try:
        hidden_rows = json.loads(proof["sheet_hidden_rows"] or "[]")
        hidden_cols = json.loads(proof["sheet_hidden_cols"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return [f"{prefix} 隐藏行列元数据无法独立复算逐行分类"]
    if not isinstance(hidden_rows, list) or not isinstance(hidden_cols, list):
        return [f"{prefix} 隐藏行列元数据不是列表"]
    try:
        sheet_n_rows = int(proof["sheet_n_rows"])
        sheet_n_cols = int(proof["sheet_n_cols"])
    except (TypeError, ValueError):
        return [f"{prefix} Sheet 网格尺寸无法独立复算逐行分类"]
    if data_start < 1 or data_end < data_start:
        return [f"{prefix} 表头确认数据范围非法，无法独立复算逐行分类"]

    raw_rows = conn.execute(
        """SELECT row, col, raw_value, cached_value, is_formula
             FROM raw_cells WHERE sheet_id=? ORDER BY row, col""",
        (int(proof["sheet_id"]),),
    ).fetchall()
    cells: dict[tuple[int, int], str] = {}
    gaps: list[str] = []
    for cell in raw_rows:
        try:
            row_no = int(cell["row"])
            col_no = int(cell["col"])
        except (TypeError, ValueError):
            gaps.append(f"{prefix} 原始单元格行列非法，无法独立复算逐行分类")
            continue
        if not (1 <= row_no <= sheet_n_rows and 1 <= col_no <= sheet_n_cols):
            gaps.append(f"{prefix} 原始单元格超出 Sheet 声明网格")
        value = (
            cell["cached_value"]
            if cell["is_formula"] and cell["cached_value"] is not None
            else cell["raw_value"]
        )
        if value is not None and str(value) != "":
            cells[(row_no, col_no)] = str(value)

    from jiadun.core.parsing.coverage_proof import build_sheet_coverage_proof
    from jiadun.core.parsing.header_detect import HeaderDetection

    det = HeaderDetection(
        sheet_index=0,
        header_row_lo=header_lo,
        header_row_hi=header_hi,
        col_map={str(key): int(value) for key, value in col_map.items()},
        confidence=confidence,
        needs_review=needs_review,
    )
    expected_proof, expected_rows = build_sheet_coverage_proof(
        cells,
        det,
        sheet_n_rows,
        merged_ranges=merged_ranges,
        data_range=(data_start, data_end),
        hidden_rows=[int(item) for item in hidden_rows],
        hidden_cols=[int(item) for item in hidden_cols],
        manual_range_confirmed=manual_range_confirmed,
    )

    def _same_decimal_or_empty(left: Any, right: Any) -> bool:
        left_empty = left in (None, "")
        right_empty = right in (None, "")
        if left_empty or right_empty:
            return left_empty and right_empty
        left_value = _decimal_value(left)
        right_value = _decimal_value(right)
        return left_value is not None and right_value is not None and left_value == right_value

    proof_fields = (
        "raw_row_start", "raw_row_end", "raw_col_start", "raw_col_end",
        "raw_data_row_count", "classified_row_count", "business_rows_used",
    )
    expected_values = {
        "raw_row_start": expected_proof.raw_row_start,
        "raw_row_end": expected_proof.raw_row_end,
        "raw_col_start": expected_proof.raw_col_start,
        "raw_col_end": expected_proof.raw_col_end,
        "raw_data_row_count": expected_proof.raw_data_row_count,
        "classified_row_count": expected_proof.classified_row_count,
        "business_rows_used": expected_proof.business_rows_used,
    }
    for field_name in proof_fields:
        try:
            actual_value = int(proof[field_name])
        except (TypeError, ValueError):
            gaps.append(f"{prefix} 覆盖证明字段 {field_name} 无法独立复算")
            continue
        if actual_value != expected_values[field_name]:
            gaps.append(f"{prefix} 覆盖证明字段 {field_name} 与原始网格重算不一致")

    expected_count_fields = {
        "classified_detail_rows": expected_proof.counts.get("detail", 0),
        "excluded_subtotal_rows": (
            expected_proof.counts.get("subtotal", 0)
            + expected_proof.counts.get("grand_total", 0)
        ),
        "excluded_title_rows": expected_proof.counts.get("title", 0),
        "excluded_note_rows": expected_proof.counts.get("note", 0),
        "excluded_blank_rows": expected_proof.counts.get("blank", 0),
        "excluded_tail_note_rows": expected_proof.counts.get("tail_note", 0),
        "excluded_orphan_numeric_rows": expected_proof.counts.get("orphan_numeric", 0),
        "excluded_parse_failed_rows": expected_proof.counts.get("parse_failed", 0),
        "pending_rows": (
            expected_proof.counts.get("pending_review", 0)
            + expected_proof.counts.get("tail_note", 0)
        ),
        "unrecognized_rows": expected_proof.counts.get("unrecognized", 0),
    }
    for field_name, expected_value in expected_count_fields.items():
        try:
            actual_value = int(proof[field_name] or 0)
        except (TypeError, ValueError):
            gaps.append(f"{prefix} 覆盖证明字段 {field_name} 无法独立复算")
            continue
        if actual_value != expected_value:
            gaps.append(f"{prefix} 覆盖证明字段 {field_name} 与原始分类不一致")
    if str(proof["proof_status"] or "") != expected_proof.proof_status:
        gaps.append(f"{prefix} 覆盖证明状态与原始分类不一致")
    try:
        actual_reasons = json.loads(proof["proof_reason"] or "[]")
    except (TypeError, json.JSONDecodeError):
        actual_reasons = None
    if actual_reasons != expected_proof.proof_reason:
        gaps.append(f"{prefix} 覆盖证明原因与原始分类不一致")
    if not _same_decimal_or_empty(proof["raw_amount_total"], expected_proof.raw_amount_total):
        gaps.append(f"{prefix} 原始金额合计与原始网格重算不一致")
    if not _same_decimal_or_empty(proof["detail_amount_total"], expected_proof.detail_amount_total):
        gaps.append(f"{prefix} 明细金额合计与原始网格重算不一致")
    if str(proof["ab_row_set_status"] or "") != expected_proof.ab_row_set_status:
        gaps.append(f"{prefix} A/B 行集状态与原始分类不一致")
    if (proof["ab_row_set_hash"] or None) != (expected_proof.ab_row_set_hash or None):
        gaps.append(f"{prefix} A/B 行集指纹与原始分类不一致")
    if str(proof["ab_independence_level"] or "") != expected_proof.ab_independence_level:
        gaps.append(f"{prefix} A/B 独立性级别与原始分类不一致")

    actual_by_row: dict[int, sqlite3.Row] = {}
    for row in class_rows:
        try:
            row_no = int(row["row_number"])
        except (TypeError, ValueError):
            continue
        if row_no in actual_by_row:
            gaps.append(f"{prefix} 逐行分类存在重复行号 {row_no}")
        actual_by_row[row_no] = row
    expected_by_row = {item.row_number: item for item in expected_rows}
    if set(actual_by_row) != set(expected_by_row):
        gaps.append(f"{prefix} 逐行分类行号集合与原始网格不一致")
    for row_no, expected in expected_by_row.items():
        actual = actual_by_row.get(row_no)
        if actual is None:
            continue
        if str(actual["class_code"] or "") != expected.class_code:
            gaps.append(f"{prefix} 第{row_no}行分类与原始网格不一致")
        if str(actual["reason_code"] or "") != expected.reason_code:
            gaps.append(f"{prefix} 第{row_no}行分类原因与原始网格不一致")
        try:
            actual_source_range = json.loads(actual["source_range_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            actual_source_range = None
        if actual_source_range != expected.source_range:
            gaps.append(f"{prefix} 第{row_no}行来源范围与原始网格不一致")
        try:
            actual_raw_values = json.loads(actual["raw_values_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            actual_raw_values = None
        if actual_raw_values != expected.raw_values:
            gaps.append(f"{prefix} 第{row_no}行原始字段与原始网格不一致")
        for field_name in ("calculated_amount", "effective_amount"):
            if not _same_decimal_or_empty(actual[field_name], getattr(expected, field_name)):
                gaps.append(f"{prefix} 第{row_no}行 {field_name} 与原始网格不一致")
        expected_bits = {
            "participates_in_a": int(expected.participates_in_a),
            "participates_in_b": int(expected.participates_in_b),
            "participates_in_c": int(expected.participates_in_c),
            "is_pending": int(expected.is_pending),
            "is_parse_failed": int(expected.is_parse_failed),
        }
        for field_name, expected_value in expected_bits.items():
            try:
                actual_value = int(actual[field_name])
            except (TypeError, ValueError):
                gaps.append(f"{prefix} 第{row_no}行字段 {field_name} 无法独立复算")
                continue
            if actual_value != expected_value:
                gaps.append(f"{prefix} 第{row_no}行字段 {field_name} 与原始分类不一致")
    return gaps


def _coverage_proof_consistency(
    conn: sqlite3.Connection,
    project_id: int,
    proof: sqlite3.Row,
) -> tuple[list[str], D | None]:
    """独立勾稽覆盖证明的来源、范围、行分类和 Decimal 金额。"""
    gaps: list[str] = []
    sheet_id = int(proof["sheet_id"])
    prefix = f"Sheet {sheet_id}"
    if (
        proof["file_id"] is None
        or proof["batch_id"] is None
        or proof["file_id"] != proof["sheet_file_id"]
        or proof["batch_id"] != proof["sheet_batch_id"]
    ):
        gaps.append(f"{prefix} 覆盖证明文件或解析批次归属不一致")

    def _int_field(name: str) -> int | None:
        try:
            value = int(proof[name])
        except (TypeError, ValueError):
            gaps.append(f"{prefix} 覆盖证明字段 {name} 非法")
            return None
        if value < 0:
            gaps.append(f"{prefix} 覆盖证明字段 {name} 不能为负数")
            return None
        return value

    start = _int_field("raw_row_start")
    end = _int_field("raw_row_end")
    col_start = _int_field("raw_col_start")
    col_end = _int_field("raw_col_end")
    raw_count = _int_field("raw_data_row_count")
    classified_count = _int_field("classified_row_count")
    if start is not None and end is not None:
        if start < 1 or end < start:
            gaps.append(f"{prefix} 覆盖证明行范围非法")
        elif raw_count is not None and raw_count != end - start + 1:
            gaps.append(f"{prefix} 原始数据区行数与行范围不一致")
        if proof["sheet_n_rows"] is not None and end > int(proof["sheet_n_rows"]):
            gaps.append(f"{prefix} 覆盖证明超出原始 Sheet 行范围")
    if col_start is not None and col_end is not None:
        if col_start < 1 or col_end < col_start:
            gaps.append(f"{prefix} 覆盖证明列范围非法")
        if proof["sheet_n_cols"] is not None and col_end > int(proof["sheet_n_cols"]):
            gaps.append(f"{prefix} 覆盖证明超出原始 Sheet 列范围")

    # 证明范围不能只与自身的 raw_data_row_count 自洽，还必须与表头确认时
    # 的 data_row_start/end 和当前不可变 raw_cells 网格勾稽。否则旧证明把
    # 行范围写窄，再追加一行真实明细，A/B/C 仍可能碰巧相等而显示绿色。
    manual_range_confirmed = False
    header = conn.execute(
        """SELECT header_row_hi, data_row_start, data_row_end,
                          data_range_status, data_range_method
             FROM table_headers WHERE sheet_id=? ORDER BY id DESC LIMIT 1""",
        (sheet_id,),
    ).fetchone()
    if header is None:
        gaps.append(f"{prefix} 缺少当前表头与数据范围确认记录")
    else:
        if start is not None and end is not None:
            if header["data_row_start"] is None or header["data_row_end"] is None:
                gaps.append(f"{prefix} 表头确认缺少数据范围边界")
            elif (
                int(header["data_row_start"]) != start
                or int(header["data_row_end"]) != end
            ):
                gaps.append(f"{prefix} 覆盖证明范围与表头确认数据范围不一致")
            try:
                header_hi = int(header["header_row_hi"])
            except (TypeError, ValueError):
                header_hi = None
                gaps.append(f"{prefix} 表头末行非法")
            if header_hi is not None:
                if start <= header_hi:
                    gaps.append(f"{prefix} 表头末行必须早于数据区起始行")
                cells = conn.execute(
                    """SELECT row, raw_value, cached_value, is_formula
                         FROM raw_cells WHERE sheet_id=? ORDER BY row, col""",
                    (sheet_id,),
                ).fetchall()
                non_empty_rows: set[int] = set()
                for cell in cells:
                    value = (
                        cell["cached_value"]
                        if cell["is_formula"] and cell["cached_value"] is not None
                        else cell["raw_value"]
                    )
                    if value is not None and str(value).strip():
                        try:
                            row_no = int(cell["row"])
                        except (TypeError, ValueError):
                            gaps.append(f"{prefix} 原始网格行号非法")
                            continue
                        if row_no > header_hi:
                            non_empty_rows.add(row_no)
                        if proof["sheet_n_rows"] is not None and row_no > int(proof["sheet_n_rows"]):
                            gaps.append(f"{prefix} 原始单元格超出 Sheet 声明行范围")
                outside_rows = sorted(
                    row_no for row_no in non_empty_rows
                    if not (start <= row_no <= end)
                )
                if outside_rows:
                    gaps.append(
                        f"{prefix} 表头后的非空原始行未被覆盖：{outside_rows[:10]}"
                    )
        range_status = str(header["data_range_status"] or "unproven")
        range_method = str(header["data_range_method"] or "unknown")
        manual_range_confirmed = (
            range_status == "confirmed" and range_method == "manual_confirmation"
        )
        if range_status not in {"confirmed", "inferred"}:
            gaps.append(f"{prefix} 表头数据范围状态不是 confirmed/inferred")
        if range_method not in {"manual_confirmation", "last_non_empty_row"}:
            gaps.append(f"{prefix} 表头数据范围方法无法证明")

    class_rows = conn.execute(
        """SELECT row_number, class_code, reason_code, source_range_json,
                          raw_values_json, calculated_amount, effective_amount,
                          participates_in_a, participates_in_b, participates_in_c,
                          is_pending, is_parse_failed
             FROM row_classifications WHERE proof_id=? ORDER BY row_number""",
        (int(proof["id"]),),
    ).fetchall()
    gaps.extend(_coverage_source_consistency(conn, proof, class_rows))
    gaps.extend(_coverage_semantic_consistency(conn, proof, class_rows))

    # ``proof_reason`` 与逐行分类是覆盖证明的状态来源之一，不能只把
    # proof_status 改成 ``complete`` 就绕过解析/待确认原因。生成器对完整
    # 证明写入 ``[]``；其它状态必须保留至少一条可解析的原因，供报告与
    # 审计回溯。未知状态或非字符串原因一律 fail-closed。
    proof_status = str(proof["proof_status"] or "")
    allowed_statuses = {"complete", "partial", "unproven"}
    if proof_status not in allowed_statuses:
        gaps.append(f"{prefix} 覆盖证明状态无法识别")
    try:
        proof_reasons = json.loads(proof["proof_reason"] or "[]")
    except (TypeError, json.JSONDecodeError):
        proof_reasons = None
    if not isinstance(proof_reasons, list) or any(
        not isinstance(reason, str) or not reason.strip() for reason in proof_reasons
    ):
        gaps.append(f"{prefix} 覆盖证明原因无法解析")
        proof_reasons = []
    if proof_status == "complete" and proof_reasons:
        # 人工确认隐藏范围会保留一条“已解决的可见性风险”作为审计痕迹。
        # 它不是未解决原因，但只有当前表头确实绑定了人工范围确认时才
        # 允许出现在 complete 证明中；其它任意原因仍然 fail-closed。
        if not (
            manual_range_confirmed
            and proof_reasons == ["hidden_rows_or_columns_manually_confirmed"]
        ):
            gaps.append(f"{prefix} 完整覆盖证明仍保留未解决原因")
    elif proof_status in {"partial", "unproven"} and not proof_reasons:
        gaps.append(f"{prefix} 非完整覆盖证明缺少原因")

    # 逐行状态字段是冗余但可审计的分类事实。它们必须与 class_code 和
    # 是否参与 A/B 累计相互一致；否则攻击者可保留金额/计数并仅伪造标志，
    # 使完整证明在读取层呈现绿色。C 当前没有逐行累计路径，故要求其保持
    # false，控制值必须由独立的 C Evidence 回读。
    def _bit(value: Any, field_name: str, row_number: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            gaps.append(f"{prefix} 第{row_number}行字段 {field_name} 非法")
            return None
        if parsed not in {0, 1}:
            gaps.append(f"{prefix} 第{row_number}行字段 {field_name} 不是 0/1")
            return None
        return parsed

    pending_codes = {"pending_review", "tail_note"}
    unresolved_codes = {
        "parse_failed", "pending_review", "tail_note", "unrecognized", "orphan_numeric"
    }
    for item in class_rows:
        row_number = item["row_number"]
        code = str(item["class_code"] or "")
        pending = _bit(item["is_pending"], "is_pending", row_number)
        parse_failed = _bit(item["is_parse_failed"], "is_parse_failed", row_number)
        part_a = _bit(item["participates_in_a"], "participates_in_a", row_number)
        part_b = _bit(item["participates_in_b"], "participates_in_b", row_number)
        part_c = _bit(item["participates_in_c"], "participates_in_c", row_number)
        expected_pending = int(code in pending_codes)
        expected_parse_failed = int(code == "parse_failed")
        if pending is not None and pending != expected_pending:
            gaps.append(f"{prefix} 第{row_number}行待确认标志与分类不一致")
        if parse_failed is not None and parse_failed != expected_parse_failed:
            gaps.append(f"{prefix} 第{row_number}行解析失败标志与分类不一致")
        if pending == 1 and parse_failed == 1:
            gaps.append(f"{prefix} 第{row_number}行同时标记待确认和解析失败")
        expected_participates = 0
        if code == "detail" and pending == 0 and parse_failed == 0:
            effective = _decimal_value(item["effective_amount"])
            expected_participates = int(effective is not None)
            if item["effective_amount"] not in (None, "") and effective is None:
                gaps.append(f"{prefix} 第{row_number}行有效金额无法按 Decimal 解析")
        for field_name, value in (
            ("participates_in_a", part_a),
            ("participates_in_b", part_b),
        ):
            if value is not None and value != expected_participates:
                gaps.append(f"{prefix} 第{row_number}行 {field_name} 与分类/金额不一致")
        if part_a is not None and part_b is not None and part_a != part_b:
            gaps.append(f"{prefix} 第{row_number}行 A/B 参与标志不一致")
        if part_c == 1:
            gaps.append(f"{prefix} 第{row_number}行 C 路径参与标志不受支持")
        if code in unresolved_codes and any(value == 1 for value in (part_a, part_b, part_c)):
            gaps.append(f"{prefix} 第{row_number}行未解决分类不得参与累计")

    if classified_count is not None and len(class_rows) != classified_count:
        gaps.append(f"{prefix} 逐行分类数量与证明摘要不一致")
    if raw_count is not None and proof["proof_status"] == "complete" and len(class_rows) != raw_count:
        gaps.append(f"{prefix} 完整证明未覆盖原始数据区每一行")

    def _stored_count(name: str) -> int:
        try:
            value = int(proof[name] or 0)
        except (TypeError, ValueError):
            gaps.append(f"{prefix} 覆盖证明字段 {name} 非法")
            return 0
        if value < 0:
            gaps.append(f"{prefix} 覆盖证明字段 {name} 不能为负数")
            return 0
        return value

    expected_counts = {
        "detail": _stored_count("classified_detail_rows"),
        "subtotal": _stored_count("excluded_subtotal_rows"),
        "grand_total": 0,
        "title": _stored_count("excluded_title_rows"),
        "note": _stored_count("excluded_note_rows"),
        "blank": _stored_count("excluded_blank_rows"),
        "tail_note": _stored_count("excluded_tail_note_rows"),
        "orphan_numeric": _stored_count("excluded_orphan_numeric_rows"),
        "parse_failed": _stored_count("excluded_parse_failed_rows"),
        "unrecognized": _stored_count("unrecognized_rows"),
    }
    actual_counts: dict[str, int] = {}
    detail_total = D(0)
    raw_total = D(0)
    detail_effective_count = 0
    raw_amount_seen = False
    used_count = 0
    for item in class_rows:
        try:
            row_number = int(item["row_number"])
        except (TypeError, ValueError):
            gaps.append(f"{prefix} 逐行分类行号非法")
            row_number = None
        if row_number is not None and start is not None and end is not None and not (start <= row_number <= end):
            gaps.append(f"{prefix} 逐行分类超出证明行范围")
        code = str(item["class_code"] or "")
        actual_counts[code] = actual_counts.get(code, 0) + 1
        if code == "detail":
            effective = _decimal_value(item["effective_amount"])
            if item["effective_amount"] not in (None, "") and effective is None:
                gaps.append(f"{prefix} 明细有效金额无法按 Decimal 解析")
            if effective is not None:
                detail_total += effective
                detail_effective_count += 1
            try:
                raw_values = json.loads(item["raw_values_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                raw_values = {}
                gaps.append(f"{prefix} 明细原始值 JSON 无法解析")
            if isinstance(raw_values, dict) and raw_values.get("amount") not in (None, ""):
                raw_amount = _decimal_value(raw_values.get("amount"))
                if raw_amount is None:
                    gaps.append(f"{prefix} 明细原始金额无法按 Decimal 解析")
                else:
                    raw_total += raw_amount
                    raw_amount_seen = True
        if int(item["participates_in_a"] or 0):
            used_count += 1
    if actual_counts.get("grand_total", 0) + actual_counts.get("subtotal", 0) != expected_counts["subtotal"]:
        gaps.append(f"{prefix} 小计/合计排除行数与逐行分类不一致")
    for code, expected in expected_counts.items():
        if code in {"grand_total", "subtotal"}:
            continue
        if actual_counts.get(code, 0) != expected:
            gaps.append(f"{prefix} 分类 {code} 数量与证明摘要不一致")
    for code in actual_counts:
        if code not in expected_counts:
            gaps.append(f"{prefix} 出现未定义的逐行分类 {code}")
    if proof_status == "complete":
        unresolved_count = sum(actual_counts.get(code, 0) for code in unresolved_codes)
        if unresolved_count:
            gaps.append(f"{prefix} 完整覆盖证明含未解决行分类")
        if any(expected_counts.get(code, 0) for code in unresolved_codes):
            gaps.append(f"{prefix} 完整覆盖证明摘要仍含未解决行数量")
    if used_count != _stored_count("business_rows_used"):
        gaps.append(f"{prefix} 参与累计行数与逐行分类不一致")
    stored_detail = _decimal_value(proof["detail_amount_total"])
    if stored_detail is None:
        if detail_effective_count:
            gaps.append(f"{prefix} 明细金额合计缺失或无法解析")
    elif stored_detail != detail_total:
        gaps.append(f"{prefix} 明细金额合计与逐行 Decimal 合计不一致")
    stored_raw = _decimal_value(proof["raw_amount_total"])
    if stored_raw is None:
        if raw_amount_seen:
            gaps.append(f"{prefix} 原始金额合计缺失或无法解析")
    elif stored_raw != raw_total:
        gaps.append(f"{prefix} 原始金额合计与逐行 Decimal 合计不一致")
    return gaps, (stored_detail if stored_detail is not None else None)


def _verification(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    availability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    availability = availability or run_contract.current_results_available(conn, project_id)
    scope, params = run_contract.current_scope(conn, project_id, "cr")
    rows = conn.execute(
        f"""SELECT cr.id, cr.period_id, sp.period_no, sp.direction,
                          cr.verification_level, cr.status, cr.detail_rows,
                          cr.pending_sheets, cr.range_unproven_sheets,
                          cr.classified_detail_rows, cr.business_rows_used,
                          cr.path_a_total, cr.path_b_total, cr.raw_subtotal,
                      cr.diff_ab, cr.ab_status, cr.control_diff, cr.control_status,
                          cr.c_control_evidence_id, cr.c_control_source_json, cr.evidence_id,
                      cr.coverage_proof_status, cr.ab_independence_level
               FROM crosscheck_results cr
               JOIN settlement_periods sp
                 ON sp.id=cr.period_id AND sp.project_id=cr.project_id
               WHERE cr.project_id=? AND {scope}""",
        (project_id, *params),
    ).fetchall()
    diagnostic_rows: list[sqlite3.Row] = []
    if not rows and not availability["available"]:
        # 输入/契约漂移时 current_scope 有意返回 1=0，防止旧结果继续参与
        # 当前结论。仍保留一份只读诊断快照，用于回读 C 来源并把“为什么被
        # 降级”写进 evidence_gaps；这些行绝不进入 levels、periods_checked
        # 或任何当前金额统计。
        candidates = conn.execute(
            """SELECT cr.id, cr.period_id, sp.period_no, sp.direction,
                              cr.verification_level, cr.status, cr.detail_rows,
                              cr.pending_sheets, cr.range_unproven_sheets,
                              cr.classified_detail_rows, cr.business_rows_used,
                              cr.path_a_total, cr.path_b_total, cr.raw_subtotal,
                              cr.diff_ab, cr.ab_status, cr.control_diff, cr.control_status,
                              cr.c_control_evidence_id, cr.c_control_source_json, cr.evidence_id,
                              cr.coverage_proof_status, cr.ab_independence_level
                       FROM crosscheck_results cr
                       JOIN settlement_periods sp
                         ON sp.id=cr.period_id AND sp.project_id=cr.project_id
                      WHERE cr.project_id=?
                      ORDER BY cr.id DESC""",
            (project_id,),
        ).fetchall()
        seen_periods: set[int] = set()
        for candidate in candidates:
            period_key = int(candidate["period_id"])
            if period_key in seen_periods:
                continue
            seen_periods.add(period_key)
            diagnostic_rows.append(candidate)
    levels = {"sufficient": 0, "findings": 0, "insufficient": 0}
    unknown_levels: dict[str, int] = {}
    for row in rows:
        level = row["verification_level"]
        if level in levels:
            levels[level] += 1
        else:
            # 数据库没有 CHECK 约束时也必须在读取层 fail-closed；未知值不
            # 能落入后面的 ``else: sufficient`` 分支。
            unknown_levels[str(level)] = unknown_levels.get(str(level), 0) + 1
    total_periods = _count(
        conn, "SELECT COUNT(*) FROM settlement_periods WHERE project_id=?", (project_id,)
    )
    periods_unchecked = max(0, total_periods - len(rows))
    # 期次校核的状态和金额必须有同一当前运行下的 Evidence 支撑。仅有
    # ``verification_level='sufficient'`` 的硬编码结果不能越过项目结论闸门。
    # 覆盖证明也先读出，再把其 Evidence 纳入同一组结构化检查；不能只看
    # “当前运行存在任意一行证明”，否则错误期次、unproven 行或错误类型的
    # Evidence 都可能造成 false-green。
    current = run_contract.get_current_contract(conn, project_id)
    # source_files 是原始证据根。数据库 v43 触发器阻止新库直接改写身份，
    # 读取层仍需复核旧库/外部绕过触发器的情况，不能让被改名、改 SHA、改
    # 大小或改项目归属的文件继续支撑当前绿色结果。
    evidence_gaps: list[str] = run_contract.source_file_contract_gaps(
        conn, project_id, current
    )
    evidence_gaps.extend(
        run_contract.line_item_contract_gaps(conn, project_id, current)
    )
    evidence_gaps.extend(
        run_contract.mapping_contract_gaps(conn, project_id, current)
    )
    evidence_gaps.extend(
        run_contract.period_contract_gaps(conn, project_id, current)
    )
    evidence_gaps.extend(
        run_contract.sheet_scope_contract_gaps(conn, project_id, current)
    )
    evidence_gaps.extend(
        run_contract.contract_input_gaps(conn, project_id, current)
    )
    current_proofs: list[sqlite3.Row] = []
    if current is not None:
        current_proofs = conn.execute(
            """SELECT p.id, p.project_id, p.run_id, p.run_signature,
                      p.file_id, p.batch_id, p.period_id, p.sheet_id, p.direction,
                      p.raw_row_start, p.raw_row_end, p.raw_col_start, p.raw_col_end,
                      p.raw_data_row_count, p.classified_row_count,
                      p.classified_detail_rows, p.excluded_subtotal_rows,
                      p.excluded_title_rows, p.excluded_note_rows, p.excluded_blank_rows,
                      p.excluded_tail_note_rows, p.excluded_orphan_numeric_rows,
                      p.excluded_parse_failed_rows, p.pending_rows, p.unrecognized_rows,
                      p.proof_status, p.proof_reason, p.raw_amount_total,
                      p.detail_amount_total, p.ab_row_set_status, p.ab_row_set_hash,
                      p.ab_independence_level, p.evidence_id, p.business_rows_used,
                      rs.period_id AS sheet_period_id, rs.batch_id AS sheet_batch_id,
                      rs.n_rows AS sheet_n_rows, rs.n_cols AS sheet_n_cols,
                      rs.merged_ranges_json AS sheet_merged_ranges,
                      rs.hidden_rows_json AS sheet_hidden_rows,
                      rs.hidden_cols_json AS sheet_hidden_cols,
                      pb.file_id AS sheet_file_id, sf.project_id AS sheet_project_id
                 FROM sheet_coverage_proofs p
                 LEFT JOIN raw_sheets rs ON rs.id=p.sheet_id
                 LEFT JOIN parse_batches pb ON pb.id=rs.batch_id
                 LEFT JOIN source_files sf ON sf.id=pb.file_id
                WHERE p.project_id=? AND p.run_id=? AND p.run_signature=?
                ORDER BY p.sheet_id, p.id DESC""",
            (project_id, current.run_id, current.signature),
        ).fetchall()
    # 同一运行、同一 Sheet 的覆盖证明是不可变追加快照。当前读取面只认
    # 最新快照；旧快照仍保留在数据库中供审计，但不能因原始数据已修复或
    # 证明已重新生成而反过来阻断当前结论。
    latest_proof_by_sheet: dict[int, sqlite3.Row] = {}
    for proof in current_proofs:
        latest_proof_by_sheet.setdefault(int(proof["sheet_id"]), proof)
    current_latest_proofs = list(latest_proof_by_sheet.values())
    evidence_scope, evidence_params = run_contract.current_scope(conn, project_id, "e")
    valid_evidence_ids = {
        int(item["id"])
        for item in conn.execute(
            f"""SELECT e.id FROM evidence e
                WHERE e.project_id=? AND {evidence_scope}""",
            (project_id, *evidence_params),
        ).fetchall()
    }
    referenced_evidence_ids = {
        int(item)
        for row in rows
        for item in (row["evidence_id"], row["c_control_evidence_id"])
        if item is not None
    }
    referenced_evidence_ids.update(
        int(item)
        for row in diagnostic_rows
        for item in (row["evidence_id"], row["c_control_evidence_id"])
        if item is not None
    )
    referenced_evidence_ids.update(
        int(row["evidence_id"])
        for row in current_proofs
        if row["evidence_id"] is not None
    )
    evidence_rows = {
        int(item["id"]): item
        for item in conn.execute(
            """SELECT id, kind, scope, run_id, run_signature, sources_json, steps_json
               FROM evidence WHERE project_id=? AND id IN ({})""".format(
                ",".join("?" for _ in referenced_evidence_ids) or "NULL"
            ),
            (project_id, *sorted(referenced_evidence_ids)),
        ).fetchall()
    }
    proof_detail_totals: dict[int, D | None] = {}
    for proof in current_latest_proofs:
        proof_gaps, detail_total = _coverage_proof_consistency(conn, project_id, proof)
        proof_detail_totals[int(proof["id"])] = detail_total
        evidence_gaps.extend(f"当前覆盖证明 {message}" for message in proof_gaps)
    for row in rows:
        evidence_id = row["evidence_id"]
        main_evidence = evidence_rows.get(int(evidence_id)) if evidence_id is not None else None
        if evidence_id is None or int(evidence_id) not in valid_evidence_ids:
            evidence_gaps.append("期次校核主 Evidence 缺失或不属于当前运行")
        elif main_evidence is None or main_evidence["kind"] != "cross_check":
            evidence_gaps.append("期次校核主 Evidence 类型不是 cross_check")
        elif main_evidence["scope"] != "current":
            evidence_gaps.append("期次校核主 Evidence 非当前自动范围")
        elif not _evidence_refers_to_period(
            main_evidence["sources_json"], int(row["period_id"]), row["period_no"], row["direction"]
        ):
            evidence_gaps.append("期次校核主 Evidence 未定位到对应期次或方向")
        if row["verification_level"] == "sufficient" and row["control_status"] != "match":
            evidence_gaps.append(
                f"第{row['period_no']}期声明校核充分但 C 控制状态不是 match"
            )
        if row["verification_level"] == "sufficient" and row["status"] != "match":
            evidence_gaps.append(
                f"第{row['period_no']}期声明校核充分但 A/B 状态不是 match"
            )
        if row["verification_level"] == "sufficient" and row["ab_status"] != "match":
            evidence_gaps.append(
                f"第{row['period_no']}期声明校核充分但 AB 路径状态不是 match"
            )
        if row["verification_level"] == "sufficient":
            if int(row["pending_sheets"] or 0) != 0:
                evidence_gaps.append(
                    f"第{row['period_no']}期声明校核充分但仍有待确认 Sheet"
                )
        if row["verification_level"] == "sufficient":
            if main_evidence is None:
                # 主 Evidence 缺失的具体错误已在上方登记，这里只避免重复
                # 访问 None；缺 Evidence 本身已经是 fail-closed。
                pass
            else:
                evidence_gaps.extend(
                    f"第{row['period_no']}期 {message}"
                    for message in _crosscheck_path_scope_gaps(main_evidence["steps_json"])
                )
            if int(row["range_unproven_sheets"] or 0) != 0:
                evidence_gaps.append(
                    f"第{row['period_no']}期声明校核充分但取数范围未证明"
                )
        if row["verification_level"] == "sufficient":
            control_diff = _decimal_value(row["control_diff"])
            if control_diff is None or abs(control_diff) > D("0.02"):
                evidence_gaps.append(
                    f"第{row['period_no']}期声明校核充分但 C 控制差额无法证明在容差内"
                )
            diff_ab = _decimal_value(row["diff_ab"])
            if diff_ab is None or abs(diff_ab) > D("0.02"):
                evidence_gaps.append(
                    f"第{row['period_no']}期声明校核充分但 A/B 差额无法证明在容差内"
                )
            if row["path_a_total"] is None or row["path_b_total"] is None:
                evidence_gaps.append(
                    f"第{row['period_no']}期声明校核充分但 A/B 路径金额缺失"
                )
        if row["control_status"] == "match":
            control_id = row["c_control_evidence_id"]
            control_evidence = evidence_rows.get(int(control_id)) if control_id is not None else None
            if control_id is None or int(control_id) not in valid_evidence_ids:
                evidence_gaps.append("C 控制值虽一致但缺少控制来源 Evidence")
            elif control_evidence is None or control_evidence["kind"] != "line_item_source":
                evidence_gaps.append("C 控制来源 Evidence 类型不是 line_item_source")
            elif (
                current is None
                or control_evidence["scope"] != "current"
                or control_evidence["run_id"] != current.run_id
                or control_evidence["run_signature"] != current.signature
            ):
                evidence_gaps.append("C 控制来源 Evidence 未绑定当前 Run Contract")
            elif not _evidence_refers_to_period(
                control_evidence["sources_json"], int(row["period_id"]), row["period_no"], row["direction"]
            ):
                evidence_gaps.append("C 控制来源 Evidence 未定位到对应期次或方向")
            elif not _evidence_contains_control_source(
                control_evidence["sources_json"],
                period_id=int(row["period_id"]),
                period_no=row["period_no"],
                direction=row["direction"],
            ):
                evidence_gaps.append(
                    "C 控制来源 Evidence 缺少文件、Sheet、行列、原文或选择规则"
                )
            else:
                evidence_gaps.extend(
                    f"第{row['period_no']}期 {message}"
                    for message in _control_source_consistency(
                        conn, project_id, row, control_evidence
                    )
                )
    for result_row in diagnostic_rows:
        control_id = result_row["c_control_evidence_id"]
        control_evidence = (
            evidence_rows.get(int(control_id))
            if control_id is not None else None
        )
        if (
            result_row["control_status"] == "match"
            and control_evidence is not None
            and control_evidence["kind"] == "line_item_source"
        ):
            evidence_gaps.extend(
                f"第{result_row['period_no']}期历史结果诊断：{message}"
                for message in _control_source_consistency(
                    conn, project_id, result_row, control_evidence
                )
            )
    # 不能相信结果表自己声明“校核充分”而忽略 coverage 状态。完整覆盖证明
    # 是 P0-01 的必要条件；任何当前期次只要不是明确 complete，就必须保留
    # “校核不充分”，即使 A/B 数字和主 Evidence 恰好相等。
    for result_row in rows:
        if result_row["coverage_proof_status"] != "complete":
            evidence_gaps.append(
                f"第{result_row['period_no']}期覆盖证明状态不是 complete"
            )
    if any(row["coverage_proof_status"] == "complete" for row in rows):
        # 与 crosscheck._coverage_proofs_for_period 相同地按 Sheet 只取当前
        # 运行最新快照，但这里额外校验 period/sheet、proof_status、Evidence
        # 类型、scope 和来源定位。期次没有任何结算 Sheet 时也不接受
        # “complete” 字样，因为没有可证明的范围。
        evidence_by_id = evidence_rows
        for result_row in rows:
            if result_row["coverage_proof_status"] != "complete":
                continue
            period_id = int(result_row["period_id"])
            expected_sheet_rows = conn.execute(
                """SELECT rs.id AS sheet_id
                     FROM raw_sheets rs
                     JOIN parse_batches pb ON pb.id=rs.batch_id
                     JOIN source_files sf ON sf.id=pb.file_id
                    WHERE sf.project_id=? AND rs.period_id=?
                    ORDER BY rs.id""",
                (project_id, period_id),
            ).fetchall()
            if not expected_sheet_rows:
                evidence_gaps.append(
                    f"第{result_row['period_no']}期声明覆盖证明完整但没有可验证结算 Sheet"
                )
                continue
            for expected in expected_sheet_rows:
                sheet_id = int(expected["sheet_id"])
                proof = latest_proof_by_sheet.get(sheet_id)
                if proof is None:
                    evidence_gaps.append(
                        f"第{result_row['period_no']}期 Sheet {sheet_id} 缺少当前覆盖证明"
                    )
                    continue
                if (
                    proof["period_id"] != period_id
                    or proof["sheet_period_id"] != period_id
                    or proof["sheet_project_id"] != project_id
                    or (proof["direction"] or "unknown") != (result_row["direction"] or "unknown")
                ):
                    evidence_gaps.append(
                        f"第{result_row['period_no']}期 Sheet {sheet_id} 覆盖证明期次、方向或项目不一致"
                    )
                if proof["proof_status"] != "complete":
                    evidence_gaps.append(
                        f"第{result_row['period_no']}期 Sheet {sheet_id} 覆盖证明未完成"
                    )
                proof_evidence_id = proof["evidence_id"]
                proof_evidence = (
                    evidence_by_id.get(int(proof_evidence_id))
                    if proof_evidence_id is not None else None
                )
                if (
                    proof_evidence_id is None
                    or int(proof_evidence_id) not in valid_evidence_ids
                ):
                    evidence_gaps.append(
                        f"第{result_row['period_no']}期 Sheet {sheet_id} 覆盖证明 Evidence 缺失或不属于当前运行"
                    )
                elif proof_evidence is None or proof_evidence["kind"] != "sheet_coverage_proof":
                    evidence_gaps.append(
                        f"第{result_row['period_no']}期 Sheet {sheet_id} 覆盖证明 Evidence 类型错误"
                    )
                elif proof_evidence["scope"] != "current":
                    evidence_gaps.append(
                        f"第{result_row['period_no']}期 Sheet {sheet_id} 覆盖证明 Evidence 非当前范围"
                    )
                elif not _evidence_refers_to_sheet(
                    proof_evidence["sources_json"],
                    sheet_id=sheet_id,
                    period_id=period_id,
                    period_no=result_row["period_no"],
                    direction=result_row["direction"],
                ):
                    evidence_gaps.append(
                        f"第{result_row['period_no']}期 Sheet {sheet_id} 覆盖证明 Evidence 未定位到对应期次和 Sheet"
                    )
            period_proofs = [
                latest_proof_by_sheet[int(expected["sheet_id"])]
                for expected in expected_sheet_rows
                if int(expected["sheet_id"]) in latest_proof_by_sheet
            ]
            classified_detail_rows = sum(
                int(proof["classified_detail_rows"] or 0) for proof in period_proofs
            )
            business_rows_used = sum(
                int(proof["business_rows_used"] or 0) for proof in period_proofs
            )
            if int(result_row["detail_rows"] or 0) != classified_detail_rows:
                evidence_gaps.append(
                    f"第{result_row['period_no']}期 A 路径明细行数与覆盖证明不一致"
                )
            if int(result_row["classified_detail_rows"] or 0) != classified_detail_rows:
                evidence_gaps.append(
                    f"第{result_row['period_no']}期结果快照明细行数与覆盖证明不一致"
                )
            if int(result_row["business_rows_used"] or 0) != business_rows_used:
                evidence_gaps.append(
                    f"第{result_row['period_no']}期业务累计行数与覆盖证明不一致"
                )
            detail_totals = [
                proof_detail_totals.get(int(proof["id"])) for proof in period_proofs
            ]
            if any(total is None for total in detail_totals):
                evidence_gaps.append(
                    f"第{result_row['period_no']}期覆盖证明明细金额缺失或无法复算"
                )
            else:
                proof_amount = sum(
                    (total for total in detail_totals if total is not None), D(0)
                )
                for path_name in ("path_a_total", "path_b_total"):
                    path_amount = _decimal_value(result_row[path_name])
                    if path_amount is None or abs(path_amount - proof_amount) > D("0.02"):
                        evidence_gaps.append(
                            f"第{result_row['period_no']}期覆盖证明明细金额与{path_name}不一致"
                        )
            if any(
                int(proof[field] or 0) != 0
                for proof in period_proofs
                for field in (
                    "pending_rows", "unrecognized_rows",
                    "excluded_parse_failed_rows", "excluded_orphan_numeric_rows",
                )
            ):
                evidence_gaps.append(
                    f"第{result_row['period_no']}期覆盖证明仍含待确认或解析失败行"
                )
    evidence_complete = not evidence_gaps
    # 运行级侧车（例如数据库不可写、上次运行被明确置为不可用）是比期次
    # 校核更高一级的硬边界，必须保留 ``unavailable``。仅仅因为当前
    # Run Contract 的输入组成发生漂移时，期次语义仍应落在四级状态中的
    # ``insufficient``，同时由 ``current_results_available=False`` 和
    # evidence_gaps 明确告知旧结果已退出当前结论；不能把所有输入漂移都
    # 混成一个超出期次状态枚举的 unavailable。
    if not availability["available"] and availability.get("state") is not None:
        status = run_contract.FAIL_CLOSED_STATUS
    elif not rows and periods_unchecked > 0 and not availability["available"]:
        # 输入/契约漂移会让 current_scope 主动返回空集，以避免把旧结果继续
        # 当作当前证据。项目已有期次时，这个空集应解释为“当前校核不充分”，
        # 而不是“从未开始”；详细缺口已经保存在 evidence_gaps。
        status = "insufficient"
    elif not rows:
        status = "not_started"
    elif periods_unchecked > 0:
        # 当前签名下即使所有已读取期次都为 sufficient，也不能把未参与本次
        # 运行的期次隐去。A/B/C 只覆盖子集时，项目级校核必须保持不充分。
        status = "insufficient"
    elif unknown_levels:
        status = "insufficient"
    elif levels.get("insufficient", 0):
        status = "insufficient"
    elif levels.get("findings", 0):
        status = "findings"
    elif not evidence_complete:
        status = "insufficient"
    else:
        status = "sufficient"
    period_state = reporting_state.period_state(
        status,
        periods_total=total_periods,
        periods_unchecked=periods_unchecked,
        run_available=bool(availability["available"]),
    )
    return {
        "periods_total": total_periods,
        "periods_checked": len(rows),
        "periods_unchecked": periods_unchecked,
        "levels": levels,
        "unknown_levels": unknown_levels,
        "range_unproven_sheets": sum(int(row["range_unproven_sheets"] or 0) for row in rows),
        "evidence_complete": evidence_complete,
        "evidence_gaps": sorted(set(evidence_gaps)),
        "status": status,
        "period_status": period_state["label"],
        "period_status_code": period_state["code"],
        "availability_status": availability["status"],
        "current_results_available": availability["available"],
        "availability_reason": availability.get("reason"),
        "physical_limitations": list(availability.get("physical_limitations") or []),
        "paths": ["A_database", "B_raw_grid", "C_control_total"],
    }


def _risk(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    scope, params = run_contract.current_scope(conn, project_id, "a")
    rows = conn.execute(
        f"""SELECT severity, status, lifecycle_status, COUNT(*) AS n FROM anomalies a
            WHERE a.project_id=? AND {scope}
            GROUP BY severity, status, lifecycle_status""",
        (project_id, *params),
    ).fetchall()
    severity = {"high": 0, "medium": 0, "low": 0, "info": 0}
    pending_severity = {"high": 0, "medium": 0, "low": 0, "info": 0}
    status = {"open": 0, "deferred": 0, "processed": 0, "other": 0}
    status_counts: dict[str, int] = {}
    lifecycle_counts: dict[str, int] = {}
    pending_lifecycle = {
        "new", "pending_review", "confirmed_issue", "pending_data", "unknown"
    }
    processed_lifecycle = {"legitimate_business", "rectified", "closed"}
    for row in rows:
        severity[row["severity"]] = severity.get(row["severity"], 0) + int(row["n"])
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + int(row["n"])
        lifecycle = finding_lifecycle.lifecycle_status(row)
        lifecycle_counts[lifecycle] = lifecycle_counts.get(lifecycle, 0) + int(row["n"])
        if lifecycle in pending_lifecycle:
            pending_severity[row["severity"]] = (
                pending_severity.get(row["severity"], 0) + int(row["n"])
            )
        if row["status"] in {"open", "deferred"}:
            # 兼容旧调用方：保留 legacy status 计数，但项目门控使用下面
            # 的统一 lifecycle 状态，避免 confirmed_issue 被显示成绿色。
            status[row["status"]] += int(row["n"])
        if lifecycle in processed_lifecycle:
            status["processed"] += int(row["n"])
        elif lifecycle not in pending_lifecycle:
            status["other"] += int(row["n"])
    status["pending"] = sum(
        lifecycle_counts.get(code, 0) for code in pending_lifecycle
    ) + status["other"]
    return {
        "severity": severity,
        "pending_severity": pending_severity,
        "status": status,
        "status_counts": status_counts,
        "lifecycle_status_counts": lifecycle_counts,
        "total": sum(severity.values()),
    }


def _pending(
    conn: sqlite3.Connection, project_id: int, *, read_only: bool = False
) -> dict[str, Any]:
    anomaly = _risk(conn, project_id)
    match_scope, match_params = run_contract.current_scope(conn, project_id, "m")
    pending_matches = _count(
        conn,
        f"SELECT COUNT(*) FROM matches m WHERE m.project_id=? AND {match_scope} AND m.status='pending'",
        (project_id, *match_params),
    )
    manifest = import_manifest.manifest_summary(conn, project_id, read_only=read_only)
    aggregate_coverage = detection_coverage.coverage_summary(
        conn,
        project_id,
        run_kind=detection_coverage.AGGREGATE_VALIDATION,
        read_only=read_only,
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


def _sheet_states(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    """读取最新解析批次的持久化 Sheet 状态。

    同一源文件可以因为修复、重试或重新打开而产生多个 ``parse_batches``；
    旧批次仍是审计历史，但不能继续以“待确认/解析失败”污染当前项目状态。
    当最新批次没有可落库的 Sheet（例如损坏工作簿或未实现的文件类型）时，
    追加一个明确标注为 ``parse_batch`` 的解析失败状态，保持错误可追溯而不
    虚构一个不存在的源 Sheet。
    """
    batch_rows = conn.execute(
        """SELECT pb.id AS batch_id, pb.file_id, pb.parser, pb.parsed_at,
                  pb.status AS batch_status, pb.stats_json, sf.original_name
           FROM parse_batches pb
           JOIN source_files sf ON sf.id=pb.file_id
          WHERE sf.project_id=?
            AND pb.id=(
                SELECT latest.id FROM parse_batches latest
                WHERE latest.file_id=pb.file_id
                ORDER BY latest.parsed_at DESC, latest.id DESC LIMIT 1
            )
          ORDER BY sf.id, pb.id""",
        (project_id,),
    ).fetchall()
    states: list[dict[str, Any]] = []
    for batch in batch_rows:
        sheets = conn.execute(
            """SELECT id, sheet_name, period_id, sheet_status,
                      sheet_status_reason, sheet_status_updated_at, sheet_status_actor
                 FROM raw_sheets WHERE batch_id=? ORDER BY id""",
            (int(batch["batch_id"]),),
        ).fetchall()
        for row in sheets:
            raw_code = row["sheet_status"] or "pending"
            # 旧库或外部 SQL 可能写入未来状态；保留原码，同时用 pending
            # 的中文标签提示人工，不把未知值包装成已确认。
            canonical_code = (
                raw_code if raw_code in reporting_state.SHEET_STATE_CODES else "pending"
            )
            label = reporting_state.SHEET_LABELS[canonical_code]
            reason = row["sheet_status_reason"] or ""
            if canonical_code != raw_code:
                reason = (
                    f"数据库状态码 {raw_code!r} 未定义，已按待确认处理"
                    + (f"；{reason}" if reason else "")
                )
            states.append(
                {
                    "key": f"sheet:{int(row['id'])}",
                    "entity_type": "sheet",
                    "sheet_id": int(row["id"]),
                    "batch_id": int(batch["batch_id"]),
                    "file_id": int(batch["file_id"]),
                    "sheet_name": row["sheet_name"],
                    "original_name": batch["original_name"],
                    "period_id": int(row["period_id"]) if row["period_id"] is not None else None,
                    "code": canonical_code,
                    "raw_code": raw_code,
                    "label": label,
                    "reason": reason,
                    "updated_at": row["sheet_status_updated_at"],
                    "actor": row["sheet_status_actor"],
                }
            )
        if batch["batch_status"] != "ok":
            try:
                stats = json.loads(batch["stats_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                stats = {}
            reason = stats.get("error") if isinstance(stats, dict) else None
            states.append(
                {
                    "key": f"batch:{int(batch['batch_id'])}",
                    "entity_type": "parse_batch",
                    "sheet_id": None,
                    "batch_id": int(batch["batch_id"]),
                    "file_id": int(batch["file_id"]),
                    "sheet_name": "文件级解析",
                    "original_name": batch["original_name"],
                    "period_id": None,
                    "code": "parse_failed",
                    "label": reporting_state.SHEET_LABELS["parse_failed"],
                    "reason": reason or f"解析批次状态：{batch['batch_status']}",
                    "updated_at": batch["parsed_at"],
                    "actor": "system",
                }
            )
    return states


def _statuses(
    verification: dict[str, Any],
    risk: dict[str, Any],
    pending: dict[str, Any],
    coverage: dict[str, Any],
    aggregate_coverage: dict[str, Any],
    source_files: int,
    period_count: int,
    run_availability: dict[str, Any] | None = None,
    direction_states: dict[str, dict[str, Any]] | None = None,
    sheet_states: list[dict[str, Any]] | None = None,
    # 与底层 project_state 保持 fail-closed：新调用方必须显式确认金额单位，
    # 否则只能返回有条件结论。
    amount_unit_confirmed: bool = False,
) -> dict[str, Any]:
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
    period_state = reporting_state.period_state(
        verification["status"],
        periods_total=verification.get("periods_total", period_count),
        periods_unchecked=verification.get("periods_unchecked", 0),
        run_available=not unavailable,
    )
    direction_states = direction_states or {}
    pending_count = int(
        pending.get("sheets", 0)
        or pending.get("matches", 0)
        or pending.get("anomalies", 0)
    )
    manifest_blocked = pending.get("manifest_status") in {"incomplete", "mismatch"}
    sheet_states = sheet_states or []
    parse_failed_sheet_count = sum(
        1 for item in sheet_states if item.get("code") == "parse_failed"
    )
    unknown_sheet_states = [
        item["code"] for item in sheet_states
        if item.get("code") not in reporting_state.SHEET_STATE_CODES
    ]
    if unknown_sheet_states:
        pending_count = max(pending_count, 1)
    project = reporting_state.project_state(
        source_files=source_files,
        period_count=period_count,
        run_available=not unavailable,
        current_periods_checked=int(
            verification.get(
                "periods_checked",
                max(0, period_count - int(verification.get("periods_unchecked", 0))),
            )
        ),
        period_code=period_state["code"],
        direction_states=direction_states,
        detection_complete=coverage.get("status") == "complete",
        aggregate_complete=aggregate_coverage.get("status") == "complete",
        pending_count=pending_count,
        manifest_blocked=manifest_blocked,
        sheet_parse_failed_count=parse_failed_sheet_count,
        evidence_complete=bool(verification.get("evidence_complete", True)),
        amount_unit_confirmed=amount_unit_confirmed,
    )
    return {
        "automatic_analysis": automatic,
        "human_review": human,
        "business_confirmation": "not_requested",
        "approval": "not_requested",
        # 旧的内部流程状态保留；以下字段是对外统一状态合同。
        "period_status": period_state["label"],
        "period_status_code": period_state["code"],
        "direction_status": {
            direction: item["label"] for direction, item in direction_states.items()
        },
        "direction_status_code": {
            direction: item["code"] for direction, item in direction_states.items()
        },
        "project_status": project["label"],
        "project_status_code": project["code"],
        "project_status_reason_codes": list(project["reason_codes"]),
        "sheet_status": {
            str(item["sheet_id"]): item["label"] for item in sheet_states
        },
        "sheet_status_code": {
            str(item["sheet_id"]): item["code"] for item in sheet_states
        },
        "sheet_states": [dict(item) for item in sheet_states],
    }


def _direction_states(
    conn: sqlite3.Connection,
    project_id: int,
    directions: dict[str, int],
    *,
    availability: dict[str, Any],
    aggregate_coverage: dict[str, Any],
    pending: dict[str, Any],
    evidence_complete: bool = True,
) -> dict[str, dict[str, Any]]:
    """按方向统计当前签名下的期次状态，不读取历史结果。"""
    scope, params = run_contract.current_scope(conn, project_id, "cr")
    rows = conn.execute(
        f"""SELECT sp.direction, cr.verification_level
            FROM settlement_periods sp
            LEFT JOIN crosscheck_results cr
              ON cr.project_id=sp.project_id AND cr.period_id=sp.id AND {scope}
            WHERE sp.project_id=?""",
        (*params, project_id),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    coverage_complete = aggregate_coverage.get("status") == "complete"
    pending_any = bool(
        pending.get("sheets")
        or pending.get("matches")
        or pending.get("anomalies")
        or pending.get("manifest_status") in {"incomplete", "mismatch"}
    )
    # pending_any is deliberately folded into coverage_complete: a direction with
    # an unresolved Sheet/match cannot be advertised as complete even if its rows
    # happen to have a matching A/B/C total.
    direction_coverage_complete = coverage_complete and not pending_any and evidence_complete
    for direction in directions:
        direction_rows = [row for row in rows if (row["direction"] or "unknown") == direction]
        levels = {"sufficient": 0, "findings": 0, "insufficient": 0}
        checked = 0
        for row in direction_rows:
            level = row["verification_level"]
            if level is not None:
                checked += 1
                levels[level] = levels.get(level, 0) + 1
        result[direction] = reporting_state.direction_state(
            direction=direction,
            periods_total=directions[direction],
            periods_checked=checked,
            sufficient=levels["sufficient"],
            findings=levels["findings"],
            insufficient=levels["insufficient"],
            run_available=bool(availability.get("available", True)),
            coverage_complete=direction_coverage_complete,
        )
    return result


def build_project_summary(
    conn: sqlite3.Connection, project_id: int, *, read_only: bool = False
) -> ProjectSummary:
    project = conn.execute(
        "SELECT name FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    project_name = project["name"] if project else "未命名项目"
    source_files = _count(conn, "SELECT COUNT(*) FROM source_files WHERE project_id=?", (project_id,))
    directions = _direction_counts(conn, project_id)
    run_availability = run_contract.current_results_available(
        conn, project_id, allow_state_clear=not read_only
    )
    verification = _verification(conn, project_id, availability=run_availability)
    risk = _risk(conn, project_id)
    pending = _pending(conn, project_id, read_only=read_only)
    sheet_states = _sheet_states(conn, project_id)
    coverage = detection_coverage.coverage_summary(conn, project_id, read_only=read_only)
    aggregate_coverage = detection_coverage.coverage_summary(
        conn,
        project_id,
        run_kind=detection_coverage.AGGREGATE_VALIDATION,
        read_only=read_only,
    )
    version_chain = _version_chain(conn, project_id)
    historical_price_assets = _historical_price_assets(conn, project_id)
    period_count = sum(directions.values())
    direction_states = _direction_states(
        conn,
        project_id,
        directions,
        availability=run_availability,
        aggregate_coverage=aggregate_coverage,
        pending=pending,
        evidence_complete=bool(verification.get("evidence_complete", True)),
    )
    current_contract = run_contract.get_current_contract(conn, project_id)
    contract_components = (
        current_contract.components if current_contract is not None else {}
    )
    amount_unit = contract_components.get("amount_unit")
    amount_unit_confirmed = bool(amount_unit and amount_unit != "unknown")
    return ProjectSummary(
        project_id=int(project_id),
        project_name=project_name,
        report_date=datetime.now().isoformat(timespec="seconds"),
        # 导入时间不等于业务数据截止日；没有明确截止字段时保持未知。
        data_cutoff=None,
        data_cutoff_status="not_available",
        run_signature=run_availability.get("run_signature"),
        run_id=run_availability.get("run_id"),
        source_files=source_files,
        directions=directions,
        amounts=_amounts(conn, project_id, directions, read_only=read_only),
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
            direction_states,
            sheet_states,
            amount_unit_confirmed=amount_unit_confirmed,
        ),
        version_chain=version_chain,
        historical_price_assets=historical_price_assets,
        run_availability=run_availability,
    )


def build_report_model(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    read_only: bool = False,
) -> ReportModel:
    """构造统一报告模型。

    工作台状态、概览和导出状态刷新属于读取路径，必须显式传入
    ``read_only=True``，避免摘要计算为了补写派生标记或清除 fail-closed
    侧车而改变数据库状态。正式导出入口仍使用默认可写模式，由其受控的
    Run Contract/登记流程负责必要的派生数据落盘。
    """
    summary = build_project_summary(conn, project_id, read_only=read_only)
    return ReportModel(
        project_summary=summary,
        management_summary=ManagementSummary(project_summary=summary),
    )
