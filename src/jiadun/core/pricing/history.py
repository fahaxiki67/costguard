"""受控历史综合单价资产。

本模块把“已完成并确认的历史项目”与“当前清单复核提示”明确分开：

* 项目必须显式关闭，并绑定同一项目的 ``final_approval`` 版本；
* 资产只复制不可变版本快照，不回读会变化的当前清单；
* 数量、单价、金额一律经 ``Decimal`` 入口，缺失/无法解析保持空值；
* 地区、时间、项目类型、项目特征或计量单位缺失/不一致时标为不可直接比较；
* 查询只返回统计提示和 Evidence，绝不直接形成“当前价格错误”的业务结论。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from jiadun.core.contracts import run_contract
from jiadun.core.engine.money import NotANumberError, to_decimal
from jiadun.core.evidence import audit as audit_log
from jiadun.core.evidence import evidence as evidence_api

D = Decimal
VALID_DIRECTIONS = frozenset({"upward", "downward", "unknown"})


class HistoricalPriceError(ValueError):
    """历史价格资产输入、证据或比较范围不完整。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: str | None, fallback: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return parsed


def _json_has_ref(value: str | None, key: str, expected: int) -> bool:
    parsed = _loads(value, None)
    entries = parsed if isinstance(parsed, list) else [parsed]
    return any(
        isinstance(entry, dict)
        and entry.get(key) is not None
        and str(entry.get(key)) == str(expected)
        for entry in entries
    )


def _json_has_ref_deep(value: str | None, key: str, expected: int) -> bool:
    """递归检查 Evidence/Audit JSON 中的嵌套来源引用。"""
    parsed = _loads(value, None)

    def walk(node: Any) -> bool:
        if isinstance(node, dict):
            if node.get(key) is not None and str(node.get(key)) == str(expected):
                return True
            return any(walk(child) for child in node.values())
        if isinstance(node, list):
            return any(walk(child) for child in node)
        return False

    return walk(parsed)


def _raw_text(value: Any) -> str:
    return str(value or "").strip()


def _norm_text(value: Any) -> str:
    # 名称/特征/单位用于候选检索时只做确定性空白归一和大小写折叠；不
    # 自动套用别名知识，也不把不同计量口径强行合并。
    return "".join(_raw_text(value).split()).casefold()


def _required_decimal(value: Any, field_name: str) -> str:
    try:
        parsed = to_decimal(value)
        if not parsed.is_finite():
            raise NotANumberError("non-finite value")
    except (NotANumberError, TypeError, ValueError, InvalidOperation) as exc:
        raise HistoricalPriceError(f"{field_name} 缺失或无法解析，不能写入历史单价") from exc
    return str(parsed)


def _optional_decimal(value: Any) -> tuple[str | None, str | None]:
    if value is None or value == "":
        return None, None
    try:
        parsed = to_decimal(value)
        if not parsed.is_finite():
            return None, "not_finite"
        return str(parsed), None
    except (NotANumberError, TypeError, ValueError, InvalidOperation):
        # 缺失/损坏的数量或金额不补零；调用方把原值和原因放进元数据，
        # 仍可保存明确可用的单价，但统计不会使用该字段重新计算金额。
        return None, "not_numeric"


@dataclass(frozen=True)
class ProjectClosure:
    closure_id: int
    project_id: int
    final_version_id: int
    status: str
    region: str
    project_type: str
    observed_at: str | None
    closed_by: str
    closed_at: str
    reason: str
    run_id: str
    run_signature: str
    evidence_id: int | None
    audit_id: int | None
    metadata: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.closure_id,
            "project_id": self.project_id,
            "final_version_id": self.final_version_id,
            "status": self.status,
            "region": self.region,
            "project_type": self.project_type,
            "observed_at": self.observed_at,
            "closed_by": self.closed_by,
            "closed_at": self.closed_at,
            "reason": self.reason,
            "run_id": self.run_id,
            "run_signature": self.run_signature,
            "evidence_id": self.evidence_id,
            "audit_id": self.audit_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class HistoricalPriceRecord:
    price_id: int
    source_project_id: int
    closure_id: int
    source_version_id: int
    source_version_item_id: int
    raw_project_name: str
    normalized_project_name: str
    raw_name: str
    normalized_name: str
    feature: str
    normalized_feature: str
    unit: str
    normalized_unit: str
    region: str
    observed_at: str | None
    project_type: str
    direction: str
    unit_price: D | None
    quantity: D | None
    amount: D | None
    source_file_id: int | None
    source_sheet_id: int | None
    source_row: int | None
    source_evidence_id: int | None
    evidence_id: int
    audit_id: int
    created_by: str
    created_at: str
    status: str
    metadata: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.price_id,
            "source_project_id": self.source_project_id,
            "closure_id": self.closure_id,
            "source_version_id": self.source_version_id,
            "source_version_item_id": self.source_version_item_id,
            "raw_project_name": self.raw_project_name,
            "normalized_project_name": self.normalized_project_name,
            "raw_name": self.raw_name,
            "normalized_name": self.normalized_name,
            "feature": self.feature,
            "normalized_feature": self.normalized_feature,
            "unit": self.unit,
            "normalized_unit": self.normalized_unit,
            "region": self.region,
            "observed_at": self.observed_at,
            "project_type": self.project_type,
            "direction": self.direction,
            "unit_price": str(self.unit_price) if self.unit_price is not None else None,
            "quantity": str(self.quantity) if self.quantity is not None else None,
            "amount": str(self.amount) if self.amount is not None else None,
            "source_file_id": self.source_file_id,
            "source_sheet_id": self.source_sheet_id,
            "source_row": self.source_row,
            "source_evidence_id": self.source_evidence_id,
            "evidence_id": self.evidence_id,
            "audit_id": self.audit_id,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "status": self.status,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class HistoricalPriceCollection:
    records: tuple[HistoricalPriceRecord, ...]
    pending_items: tuple[dict[str, Any], ...]
    evidence_id: int | None
    audit_id: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "records": [item.as_dict() for item in self.records],
            "pending_items": [dict(item) for item in self.pending_items],
            "evidence_id": self.evidence_id,
            "audit_id": self.audit_id,
        }


@dataclass(frozen=True)
class HistoricalPriceHint:
    status: str
    review_only: bool
    normalized_name: str
    normalized_feature: str
    normalized_unit: str
    direction: str
    region: str
    observed_at: str | None
    project_type: str
    sample_count: int
    comparable_count: int
    incomparable_count: int
    min_price: D | None
    max_price: D | None
    median_price: D | None
    current_price: D | None
    deviation_from_median: D | None
    deviation_rate: D | None
    reasons: tuple[str, ...]
    evidence_ids: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        def money(value: D | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "status": self.status,
            "review_only": self.review_only,
            "normalized_name": self.normalized_name,
            "normalized_feature": self.normalized_feature,
            "normalized_unit": self.normalized_unit,
            "direction": self.direction,
            "region": self.region,
            "observed_at": self.observed_at,
            "project_type": self.project_type,
            "sample_count": self.sample_count,
            "comparable_count": self.comparable_count,
            "incomparable_count": self.incomparable_count,
            "min_price": money(self.min_price),
            "max_price": money(self.max_price),
            "median_price": money(self.median_price),
            "current_price": money(self.current_price),
            "deviation_from_median": money(self.deviation_from_median),
            "deviation_rate": money(self.deviation_rate),
            "reasons": list(self.reasons),
            "evidence_ids": list(self.evidence_ids),
        }


def _closure_from_row(row: sqlite3.Row | dict[str, Any]) -> ProjectClosure:
    metadata = _loads(row["metadata_json"], {})
    return ProjectClosure(
        closure_id=int(row["id"]),
        project_id=int(row["project_id"]),
        final_version_id=int(row["final_version_id"]),
        status=str(row["status"]),
        region=_raw_text(row["region"]),
        project_type=_raw_text(row["project_type"]),
        observed_at=_raw_text(row["observed_at"]) or None,
        closed_by=str(row["closed_by"]),
        closed_at=str(row["closed_at"]),
        reason=str(row["reason"]),
        run_id=str(row["run_id"]),
        run_signature=str(row["run_signature"]),
        evidence_id=int(row["evidence_id"]) if row["evidence_id"] is not None else None,
        audit_id=int(row["audit_id"]) if row["audit_id"] is not None else None,
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def get_project_closure(
    conn: sqlite3.Connection,
    project_id: int | None,
    closure_id: int | None = None,
) -> ProjectClosure | None:
    if closure_id is None and project_id is None:
        raise HistoricalPriceError("读取项目关闭记录必须提供项目或关闭记录 ID")
    if closure_id is None:
        row = conn.execute(
            "SELECT * FROM project_closures WHERE project_id=? ORDER BY id DESC LIMIT 1",
            (int(project_id),),
        ).fetchone()
    elif project_id is None:
        row = conn.execute(
            "SELECT * FROM project_closures WHERE id=?",
            (int(closure_id),),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM project_closures WHERE project_id=? AND id=?",
            (int(project_id), int(closure_id)),
        ).fetchone()
    return _closure_from_row(row) if row else None


def _require_closure_evidence(
    conn: sqlite3.Connection,
    closure: ProjectClosure,
) -> None:
    if closure.status != "closed" or closure.evidence_id is None or closure.audit_id is None:
        raise HistoricalPriceError("项目关闭记录缺少完整 Evidence/Audit，不能沉淀历史单价")
    evidence = conn.execute(
        """SELECT kind, project_id, scope, run_id, run_signature,
                          steps_json, sources_json
             FROM evidence WHERE id=?""",
        (closure.evidence_id,),
    ).fetchone()
    audit = conn.execute(
        """SELECT project_id, action, target, run_id, run_signature, after_json
             FROM audit_log WHERE id=?""",
        (closure.audit_id,),
    ).fetchone()
    if (
        evidence is None
        or evidence["project_id"] != closure.project_id
        or evidence["kind"] != "project_closure"
        or evidence["scope"] not in {"current", "human", "historical"}
        or evidence["run_id"] != closure.run_id
        or evidence["run_signature"] != closure.run_signature
        or not _json_has_ref(evidence["sources_json"], "closure_id", closure.closure_id)
        or not _json_has_ref(evidence["sources_json"], "final_version_id", closure.final_version_id)
        or not _json_has_ref(evidence["steps_json"], "closure_id", closure.closure_id)
        or not _json_has_ref(evidence["steps_json"], "final_version_id", closure.final_version_id)
        or audit is None
        or audit["project_id"] != closure.project_id
        or audit["action"] != "close_project_for_history"
        or audit["target"] != f"project_closure:{closure.closure_id}"
        or audit["run_id"] != closure.run_id
        or audit["run_signature"] != closure.run_signature
        or not _json_has_ref(audit["after_json"], "closure_id", closure.closure_id)
        or not _json_has_ref(audit["after_json"], "final_version_id", closure.final_version_id)
    ):
        raise HistoricalPriceError("项目关闭 Evidence/Audit 与来源项目不一致")


def _require_final_version(
    conn: sqlite3.Connection,
    project_id: int,
    version_id: int,
    current: run_contract.RunContract,
) -> sqlite3.Row:
    row = conn.execute(
        """SELECT * FROM project_versions
           WHERE id=? AND project_id=? AND version_kind='final_approval'""",
        (int(version_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise HistoricalPriceError("历史资产来源必须是当前项目的最终审定版本")
    if row["run_id"] != current.run_id or row["run_signature"] != current.signature:
        raise HistoricalPriceError("最终审定版本不属于当前 Run Contract，不能关闭项目")
    if row["evidence_id"] is None or row["audit_id"] is None:
        raise HistoricalPriceError("最终审定版本缺少 Evidence/Audit，不能关闭项目")
    evidence = conn.execute(
        """SELECT kind, scope, run_id, run_signature, steps_json, sources_json
             FROM evidence WHERE id=? AND project_id=?""",
        (row["evidence_id"], int(project_id)),
    ).fetchone()
    audit = conn.execute(
        """SELECT action, target, run_id, run_signature, after_json
             FROM audit_log WHERE id=? AND project_id=?""",
        (row["audit_id"], int(project_id)),
    ).fetchone()
    if (
        evidence is None
        or evidence["kind"] != "project_version"
        or evidence["scope"] not in {"current", "human"}
        or evidence["run_id"] != current.run_id
        or evidence["run_signature"] != current.signature
        or not _json_has_ref(evidence["sources_json"], "version_id", int(version_id))
        or not _json_has_ref(evidence["steps_json"], "version_id", int(version_id))
        or audit is None
        or audit["action"] != "create_project_version"
        or audit["target"] != f"project_version:{int(version_id)}"
        or audit["run_id"] != current.run_id
        or audit["run_signature"] != current.signature
        or not _json_has_ref(audit["after_json"], "version_id", int(version_id))
    ):
        raise HistoricalPriceError("最终审定版本的 Evidence/Audit 不属于当前运行")
    # Evidence/Audit 的责任链成立仍不足以证明快照内容可信。关闭项目会
    # 允许版本项进入长期历史资产，因此必须复用版本模块的完整性校验，
    # 包括 item_count/hash、期次/方向、显式来源 raw_cells 回读和来源项目
    # 归属；任何旧脚本直接写入的“自洽”伪造版本都在这里 fail-closed。
    from jiadun.core.versions.project import _version_integrity, get_project_version

    version_obj = get_project_version(conn, int(project_id), int(version_id))
    if version_obj is None:
        raise HistoricalPriceError("最终审定版本无法读取，不能关闭项目")
    valid, reasons = _version_integrity(conn, version_obj)
    if not valid:
        reason_text = ", ".join(reasons) or "内容或来源证据未闭合"
        raise HistoricalPriceError(
            f"最终审定版本内容或来源证据未闭合，不能关闭项目：{reason_text}"
        )
    return row


def close_project_for_history(
    conn: sqlite3.Connection,
    project_id: int,
    final_version_id: int,
    *,
    closed_by: str,
    reason: str,
    region: str = "",
    project_type: str = "",
    observed_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ProjectClosure:
    """显式关闭项目，允许其作为历史单价来源。

    “可形成项目结论”仍不是业务审批本身；这里额外要求摘要的 Evidence、
    覆盖、方向和人工待办均已闭合。缺少任一证据时直接拒绝，不创建半成品
    关闭记录。
    """
    actor = _raw_text(closed_by)
    close_reason = _raw_text(reason)
    if not actor:
        raise HistoricalPriceError("项目关闭必须记录操作人")
    if not close_reason:
        raise audit_log.AuditReasonRequiredError("项目关闭必须记录原因")
    existing = get_project_closure(conn, int(project_id))
    if existing is not None:
        raise HistoricalPriceError("项目已经存在不可覆盖的关闭记录")
    current = run_contract.get_current_contract(conn, int(project_id))
    if current is None:
        raise HistoricalPriceError("项目没有当前 Run Contract，不能关闭")
    availability = run_contract.current_results_available(conn, int(project_id))
    if not availability["available"]:
        raise HistoricalPriceError("当前运行不可用，不能关闭项目并沉淀历史资产")
    version = _require_final_version(conn, int(project_id), int(final_version_id), current)

    # 延迟导入避免 reporting.summary → pricing.history 的循环依赖；只读摘要
    # 不写派生标志，但会完整执行项目级 Evidence/fail-closed 闸门。
    from jiadun.core.reporting.summary import build_project_summary

    summary = build_project_summary(conn, int(project_id), read_only=True)
    if summary.statuses.get("project_status_code") != "can_conclude":
        raise HistoricalPriceError(
            "项目尚未满足可形成项目结论的证据条件，不能关闭并沉淀历史单价"
        )

    region = _raw_text(region)
    project_type = _raw_text(project_type)
    observed_at = _raw_text(observed_at) or None
    metadata_payload = {
        **(metadata or {}),
        "gate": "project_status_code=can_conclude",
        "final_version_id": int(final_version_id),
    }
    now = _now()
    with run_contract._transaction(conn, "close_project_for_history"):
        cur = conn.execute(
            """INSERT INTO project_closures(
                   project_id, final_version_id, status, region, project_type,
                   observed_at, closed_by, closed_at, reason, run_id, run_signature,
                   metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(project_id), int(final_version_id), "closed", region, project_type,
                observed_at, actor, now, close_reason, current.run_id, current.signature,
                _json(metadata_payload),
            ),
        )
        closure_id = int(cur.lastrowid)
        evidence_id = evidence_api.add_evidence(
            conn,
            int(project_id),
            "project_closure",
            f"项目关闭：最终审定版本 v{version['version_no']} 可作为历史单价来源",
            steps=[
                {
                    "step": "项目级证据闸门",
                    "project_status_code": summary.statuses.get("project_status_code"),
                    "verification_status": summary.verification.get("status"),
                    "evidence_complete": summary.verification.get("evidence_complete"),
                },
                {
                    "step": "固定历史资产维度",
                    "closure_id": closure_id,
                    "final_version_id": int(final_version_id),
                    "region": region,
                    "project_type": project_type,
                    "observed_at": observed_at,
                },
            ],
            sources=[
                {
                    "closure_id": closure_id,
                    "final_version_id": int(final_version_id),
                    "run_id": current.run_id,
                    "run_signature": current.signature,
                }
            ],
            commit=False,
            run_signature=current.signature,
            run_id=current.run_id,
            scope="human",
        )
        audit_id = audit_log.record_audit(
            conn,
            int(project_id),
            actor,
            "close_project_for_history",
            f"project_closure:{closure_id}",
            None,
            {
                "closure_id": closure_id,
                "final_version_id": int(final_version_id),
                "region": region,
                "project_type": project_type,
                "observed_at": observed_at,
                "evidence_id": evidence_id,
            },
            close_reason,
            commit=False,
            run_id=current.run_id,
            run_signature=current.signature,
        )
        conn.execute(
            "UPDATE project_closures SET evidence_id=?, audit_id=? WHERE id=? AND project_id=?",
            (evidence_id, audit_id, closure_id, int(project_id)),
        )
    result = get_project_closure(conn, int(project_id), closure_id)
    if result is None:
        raise RuntimeError("项目关闭记录已写入但无法重新读取")
    return result


def _row_to_price(row: sqlite3.Row | dict[str, Any]) -> HistoricalPriceRecord:
    def optional_decimal(value: Any) -> D | None:
        if value is None or value == "":
            return None
        try:
            parsed = D(str(value))
        except (TypeError, ValueError, InvalidOperation):
            return None
        return parsed if parsed.is_finite() else None

    metadata = _loads(row["metadata_json"], {})
    if not isinstance(metadata, dict):
        metadata = {}
    unit_price = optional_decimal(row["unit_price"])
    unit_price_error = None if unit_price is not None else (
        None if row["unit_price"] in (None, "") else (
            "not_finite" if str(row["unit_price"]).strip().lower()
            in {"nan", "+nan", "-nan", "infinity", "+infinity", "-infinity", "inf", "+inf", "-inf"}
            else "not_numeric"
        )
    )
    quantity = optional_decimal(row["quantity"])
    amount = optional_decimal(row["amount"])
    invalid_fields = {
        field_name: {
            "status": "invalid",
            "reason": "not_finite"
            if str(row[field_name]).strip().lower()
            in {"nan", "+nan", "-nan", "infinity", "+infinity", "-infinity", "inf", "+inf", "-inf"}
            else "not_numeric",
            "raw": row[field_name],
        }
        for field_name, value in (("quantity", row["quantity"]), ("amount", row["amount"]))
        if value not in (None, "") and optional_decimal(value) is None
    }
    if unit_price_error or invalid_fields:
        # include_revoked=True 必须能够导出损坏旧档案。保留原始数据库值在
        # metadata 中，同时把不可解析单价置为空，不能让 InvalidOperation
        # 穿透到 Word/Excel 导出层。
        metadata = {
            **metadata,
            **(
                {
                    "unit_price_integrity_status": "invalid",
                    "unit_price_integrity_reason": unit_price_error,
                    "unit_price_raw": row["unit_price"],
                }
                if unit_price_error else {}
            ),
            **{
                f"{field_name}_integrity_status": value["status"]
                for field_name, value in invalid_fields.items()
            },
            **{
                f"{field_name}_integrity_reason": value["reason"]
                for field_name, value in invalid_fields.items()
            },
            **{
                f"{field_name}_raw": value["raw"]
                for field_name, value in invalid_fields.items()
            },
        }
    return HistoricalPriceRecord(
        price_id=int(row["id"]),
        source_project_id=int(row["source_project_id"]),
        closure_id=int(row["closure_id"]),
        source_version_id=int(row["source_version_id"]),
        source_version_item_id=int(row["source_version_item_id"]),
        raw_project_name=str(row["raw_project_name"]),
        normalized_project_name=str(row["normalized_project_name"]),
        raw_name=str(row["raw_name"]),
        normalized_name=str(row["normalized_name"]),
        feature=str(row["feature"] or ""),
        normalized_feature=str(row["normalized_feature"] or ""),
        unit=str(row["unit"] or ""),
        normalized_unit=str(row["normalized_unit"] or ""),
        region=str(row["region"] or ""),
        observed_at=_raw_text(row["observed_at"]) or None,
        project_type=str(row["project_type"] or ""),
        direction=str(row["direction"] or "unknown"),
        unit_price=unit_price,
        quantity=quantity,
        amount=amount,
        source_file_id=(int(row["source_file_id"]) if row["source_file_id"] is not None else None),
        source_sheet_id=(int(row["source_sheet_id"]) if row["source_sheet_id"] is not None else None),
        source_row=(int(row["source_row"]) if row["source_row"] is not None else None),
        source_evidence_id=(
            int(row["source_evidence_id"]) if row["source_evidence_id"] is not None else None
        ),
        evidence_id=int(row["evidence_id"]),
        audit_id=int(row["audit_id"]),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        status="invalid" if unit_price_error or invalid_fields else str(row["status"]),
        metadata=metadata,
    )


def _historical_snapshot_integrity(
    conn: sqlite3.Connection,
    row: sqlite3.Row | dict[str, Any],
) -> tuple[bool, tuple[str, ...]]:
    """回读历史资产与不可变版本快照，防止旧库伪造行进入统计。"""
    reasons: list[str] = []
    try:
        source_item_id = int(row["source_version_item_id"])
        source_version_id = int(row["source_version_id"])
        project_id = int(row["source_project_id"])
    except (TypeError, ValueError):
        return False, ("historical_source_identity_invalid",)
    source = conn.execute(
        """SELECT pvi.*, pv.project_id AS version_project_id,
                  pv.id AS version_id, pv.version_kind,
                  pv.run_id AS version_run_id, pv.run_signature AS version_run_signature,
                  p.name AS project_name
             FROM project_version_items pvi
             JOIN project_versions pv
               ON pv.id=pvi.version_id AND pv.project_id=pvi.project_id
             JOIN projects p ON p.id=pvi.project_id
            WHERE pvi.id=? AND pvi.version_id=? AND pvi.project_id=?""",
        (source_item_id, source_version_id, project_id),
    ).fetchone()
    if source is None:
        return False, ("historical_source_version_item_missing",)

    # 历史价格不能只与 pvi 行“自洽”。来源版本必须是当前项目的最终审定
    # 版本，且其 Evidence/Audit、快照哈希、期次/方向和原始来源仍闭合；
    # 关闭记录也必须确实指向这个最终版本。这样旧库中直接 SQL 拼出的
    # final_version + pvi + historical_unit_prices 不会进入 active 统计。
    from jiadun.core.versions.project import _version_integrity, get_project_version

    version_obj = get_project_version(conn, project_id, source_version_id)
    if source["version_kind"] != "final_approval":
        reasons.append("historical_source_version_not_final_approval")
    if version_obj is None:
        reasons.append("historical_source_version_missing")
    else:
        version_valid, version_reasons = _version_integrity(conn, version_obj)
        if not version_valid:
            reasons.extend(f"source_version_{reason}" for reason in version_reasons)

    closure = get_project_closure(conn, project_id=None, closure_id=int(row["closure_id"]))
    if closure is None:
        reasons.append("historical_closure_missing")
    else:
        if (
            closure.project_id != project_id
            or closure.status != "closed"
            or closure.final_version_id != source_version_id
        ):
            reasons.append("historical_closure_scope_mismatch")
        if (
            source["version_run_id"] != closure.run_id
            or source["version_run_signature"] != closure.run_signature
        ):
            reasons.append("historical_closure_run_mismatch")
        try:
            _require_closure_evidence(conn, closure)
        except HistoricalPriceError:
            reasons.append("historical_closure_evidence_invalid")

    asset_evidence = conn.execute(
        """SELECT project_id, kind, scope, run_id, run_signature,
                          steps_json, sources_json
             FROM evidence WHERE id=?""",
        (row["evidence_id"],),
    ).fetchone()
    asset_audit = conn.execute(
        """SELECT project_id, action, target, run_id, run_signature, after_json
             FROM audit_log WHERE id=?""",
        (row["audit_id"],),
    ).fetchone()
    if closure is None or asset_evidence is None or asset_audit is None:
        reasons.append("historical_asset_evidence_or_audit_missing")
    else:
        if (
            asset_evidence["project_id"] != project_id
            or asset_evidence["kind"] != "historical_unit_price"
            or asset_evidence["scope"] != "historical"
            or asset_evidence["run_id"] != closure.run_id
            or asset_evidence["run_signature"] != closure.run_signature
            or not _json_has_ref_deep(asset_evidence["sources_json"], "closure_id", closure.closure_id)
            or not _json_has_ref_deep(asset_evidence["sources_json"], "source_version_id", source_version_id)
        ):
            reasons.append("historical_asset_evidence_mismatch")
        if (
            asset_audit["project_id"] != project_id
            or asset_audit["action"] != "collect_historical_prices"
            or asset_audit["target"] != f"project_closure:{closure.closure_id}"
            or asset_audit["run_id"] != closure.run_id
            or asset_audit["run_signature"] != closure.run_signature
            or not _json_has_ref_deep(asset_audit["after_json"], "closure_id", closure.closure_id)
            or not _json_has_ref_deep(asset_audit["after_json"], "final_version_id", source_version_id)
        ):
            reasons.append("historical_asset_audit_mismatch")

    def _text_equal(left: Any, right: Any) -> bool:
        return _norm_text(left) == _norm_text(right)

    def _decimal_equal(left: Any, right: Any) -> bool:
        left_value, left_reason = _optional_decimal(left)
        right_value, right_reason = _optional_decimal(right)
        if left in (None, "") and right in (None, ""):
            return True
        if left_reason or right_reason:
            return False
        return left_value == right_value

    text_fields = (
        ("raw_project_name", source["project_name"]),
        ("normalized_project_name", _norm_text(source["project_name"])),
        ("raw_name", source["name"]),
        ("normalized_name", _norm_text(source["name"])),
        ("feature", source["feature"]),
        ("normalized_feature", _norm_text(source["feature"])),
        ("unit", source["unit"]),
        ("normalized_unit", _norm_text(source["unit"])),
        ("direction", source["direction"] or "unknown"),
    )
    for field_name, expected in text_fields:
        if not _text_equal(row[field_name], expected):
            reasons.append(f"{field_name}_mismatch")
    for field_name in ("unit_price", "quantity", "amount"):
        if not _decimal_equal(row[field_name], source[field_name]):
            reasons.append(f"{field_name}_mismatch")
    for field_name in (
        "source_file_id", "source_sheet_id", "source_row", "source_evidence_id"
    ):
        left = row[field_name]
        right = source[field_name]
        if left is None and right is None:
            continue
        if left is None or right is None or str(left) != str(right):
            reasons.append(f"{field_name}_mismatch")
    return not reasons, tuple(dict.fromkeys(reasons))


def _ensure_dimension_consistency(
    closure: ProjectClosure,
    region: str | None,
    observed_at: str | None,
    project_type: str | None,
) -> tuple[str, str | None, str]:
    values = [
        ("region", closure.region, _raw_text(region)),
        ("observed_at", closure.observed_at or "", _raw_text(observed_at)),
        ("project_type", closure.project_type, _raw_text(project_type)),
    ]
    resolved: list[str] = []
    for name, fixed, requested in values:
        if fixed and requested and fixed != requested:
            raise HistoricalPriceError(f"{name} 与项目关闭时固定维度不一致")
        resolved.append(requested or fixed)
    return resolved[0], resolved[1] or None, resolved[2]


def collect_historical_prices(
    conn: sqlite3.Connection,
    closure_id: int,
    *,
    created_by: str,
    reason: str,
    item_ids: Iterable[int] | None = None,
    region: str | None = None,
    observed_at: str | None = None,
    project_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> HistoricalPriceCollection:
    """从关闭项目的最终版本复制可用单价，缺失项返回待补资料清单。"""
    actor = _raw_text(created_by)
    collect_reason = _raw_text(reason)
    if not actor:
        raise HistoricalPriceError("沉淀历史单价必须记录操作人")
    if not collect_reason:
        raise audit_log.AuditReasonRequiredError("沉淀历史单价必须记录原因")
    closure = get_project_closure(conn, project_id=None, closure_id=closure_id)
    if closure is None:
        raise HistoricalPriceError("项目关闭记录不存在")
    _require_closure_evidence(conn, closure)
    current = run_contract.get_current_contract(conn, closure.project_id)
    if current is None:
        raise HistoricalPriceError("项目没有当前 Run Contract，不能沉淀历史单价")
    if current.run_id != closure.run_id or current.signature != closure.run_signature:
        raise HistoricalPriceError("项目关闭记录不属于当前 Run Contract，不能沉淀历史单价")
    # 采集入口再次验证最终版本，而不是只相信关闭记录当时的判断；版本
    # 内容在历史快照写入前若已被旧脚本破坏，必须返回待修复而不产生资产。
    _require_final_version(
        conn, closure.project_id, closure.final_version_id, current
    )
    region, observed_at, project_type = _ensure_dimension_consistency(
        closure, region, observed_at, project_type
    )
    source_project = conn.execute(
        "SELECT name FROM projects WHERE id=?", (closure.project_id,)
    ).fetchone()
    if source_project is None:
        raise HistoricalPriceError("历史单价来源项目不存在")
    item_params: list[Any] = [closure.final_version_id, closure.project_id]
    item_sql = (
        "SELECT * FROM project_version_items WHERE version_id=? AND project_id=?"
    )
    requested_ids = None
    if item_ids is not None:
        requested_ids = sorted({int(item_id) for item_id in item_ids})
        if not requested_ids:
            raise HistoricalPriceError("至少选择一条版本清单项")
        placeholders = ",".join("?" for _ in requested_ids)
        item_sql += f" AND id IN ({placeholders})"
        item_params.extend(requested_ids)
    item_sql += " ORDER BY id"
    items = conn.execute(item_sql, tuple(item_params)).fetchall()
    if requested_ids is not None and len(items) != len(requested_ids):
        raise HistoricalPriceError("选择的版本清单项不属于关闭项目的最终版本")
    if not items:
        raise HistoricalPriceError("最终版本没有可沉淀的清单项")
    existing = {
        int(row["source_version_item_id"])
        for row in conn.execute(
            "SELECT source_version_item_id FROM historical_unit_prices WHERE closure_id=?",
            (closure.closure_id,),
        ).fetchall()
    }
    if existing.intersection(int(row["id"]) for row in items):
        raise HistoricalPriceError("所选清单项已沉淀，历史资产不可覆盖或重复写入")

    pending: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for item in items:
        raw_name = _raw_text(item["name"])
        normalized_name = _norm_text(raw_name)
        if not normalized_name:
            pending.append({"source_version_item_id": int(item["id"]), "reason": "名称缺失"})
            continue
        try:
            unit_price = _required_decimal(item["unit_price"], "单价")
        except HistoricalPriceError:
            pending.append({
                "source_version_item_id": int(item["id"]),
                "reason": "单价缺失或无法解析",
            })
            continue
        quantity, quantity_reason = _optional_decimal(item["quantity"])
        amount, amount_reason = _optional_decimal(item["amount"])
        raw_flags = _loads(item["flags_json"], {})
        if not isinstance(raw_flags, dict):
            raw_flags = {"raw_flags": raw_flags}
        candidates.append({
            "item": item,
            "raw_name": raw_name,
            "normalized_name": normalized_name,
            "feature": _raw_text(item["feature"]),
            "normalized_feature": _norm_text(item["feature"]),
            "unit": _raw_text(item["unit"]),
            "normalized_unit": _norm_text(item["unit"]),
            "direction": item["direction"] if item["direction"] in VALID_DIRECTIONS else "unknown",
            "unit_price": unit_price,
            "quantity": quantity,
            "amount": amount,
            "metadata": {
                **(metadata or {}),
                "source_flags": raw_flags,
                "quantity_status": quantity_reason or "available",
                "amount_status": amount_reason or "available",
                "historical_review_only": True,
            },
        })
    if not candidates:
        return HistoricalPriceCollection((), tuple(pending), None, None)

    source_refs = [
        {
            "source_version_item_id": int(candidate["item"]["id"]),
            "source_version_id": closure.final_version_id,
            "source_file_id": candidate["item"]["source_file_id"],
            "source_sheet_id": candidate["item"]["source_sheet_id"],
            "source_row": candidate["item"]["source_row"],
            "source_evidence_id": candidate["item"]["source_evidence_id"],
        }
        for candidate in candidates
    ]
    evidence_reason = "历史单价资产只作复核提示，不直接认定当前单价错误"
    with run_contract._transaction(conn, "collect_historical_prices"):
        evidence_id = evidence_api.add_evidence(
            conn,
            closure.project_id,
            "historical_unit_price",
            f"从已关闭项目沉淀 {len(candidates)} 条历史综合单价；{evidence_reason}",
            steps=[
                {
                    "step": "读取最终审定版本快照",
                    "closure_id": closure.closure_id,
                    "final_version_id": closure.final_version_id,
                    "candidate_count": len(candidates),
                    "pending_count": len(pending),
                },
                {
                    "step": "比较限制",
                    "region": region,
                    "observed_at": observed_at,
                    "project_type": project_type,
                    "review_only": True,
                    "limitation": evidence_reason,
                },
            ],
            sources=[
                {
                    "closure_id": closure.closure_id,
                    "source_project_id": closure.project_id,
                    "source_version_id": closure.final_version_id,
                    "region": region,
                    "observed_at": observed_at,
                    "project_type": project_type,
                    "items": source_refs,
                }
            ],
            commit=False,
            run_signature=closure.run_signature,
            run_id=closure.run_id,
            scope="historical",
            historical_reason=evidence_reason,
        )
        audit_id = audit_log.record_audit(
            conn,
            closure.project_id,
            actor,
            "collect_historical_prices",
            f"project_closure:{closure.closure_id}",
            None,
            {
                "closure_id": closure.closure_id,
                "final_version_id": closure.final_version_id,
                "candidate_count": len(candidates),
                "pending_count": len(pending),
                "evidence_id": evidence_id,
                "review_only": True,
            },
            collect_reason,
            commit=False,
            run_id=closure.run_id,
            run_signature=closure.run_signature,
        )
        conn.executemany(
            """INSERT INTO historical_unit_prices(
                   source_project_id, closure_id, source_version_id,
                   source_version_item_id, raw_project_name, normalized_project_name,
                   raw_name, normalized_name, feature, normalized_feature, unit,
                   normalized_unit, region, observed_at, project_type, direction,
                   unit_price, quantity, amount, source_file_id, source_sheet_id,
                   source_row, source_evidence_id, evidence_id, audit_id, created_by,
                   created_at, status, metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    closure.project_id,
                    closure.closure_id,
                    closure.final_version_id,
                    int(candidate["item"]["id"]),
                    str(source_project["name"]),
                    _norm_text(source_project["name"]),
                    candidate["raw_name"],
                    candidate["normalized_name"],
                    candidate["feature"],
                    candidate["normalized_feature"],
                    candidate["unit"],
                    candidate["normalized_unit"],
                    region,
                    observed_at,
                    project_type,
                    candidate["direction"],
                    candidate["unit_price"],
                    candidate["quantity"],
                    candidate["amount"],
                    candidate["item"]["source_file_id"],
                    candidate["item"]["source_sheet_id"],
                    candidate["item"]["source_row"],
                    candidate["item"]["source_evidence_id"],
                    evidence_id,
                    audit_id,
                    actor,
                    _now(),
                    "active",
                    _json(candidate["metadata"]),
                )
                for candidate in candidates
            ],
        )
    records = tuple(
        _row_to_price(row)
        for row in conn.execute(
            "SELECT * FROM historical_unit_prices WHERE closure_id=? AND evidence_id=? ORDER BY id",
            (closure.closure_id, evidence_id),
        ).fetchall()
    )
    return HistoricalPriceCollection(records, tuple(pending), evidence_id, audit_id)


def list_historical_unit_prices(
    conn: sqlite3.Connection,
    *,
    name: str | None = None,
    feature: str | None = None,
    unit: str | None = None,
    direction: str | None = None,
    region: str | None = None,
    observed_at: str | None = None,
    project_type: str | None = None,
    source_project_id: int | None = None,
    include_revoked: bool = False,
) -> list[HistoricalPriceRecord]:
    """列出历史资产；这是只读查询，不把结果混入当前项目结论。

    默认只返回当前可用资产。管理层/审计导出可显式传入
    ``include_revoked=True``，把已撤销快照一并列出并保留其状态，避免把
    撤销记录从历史档案中物理隐藏。
    """
    clauses = [] if include_revoked else ["status='active'"]
    params: list[Any] = []
    for column, value in (
        ("normalized_name", name),
        ("normalized_feature", feature),
        ("normalized_unit", unit),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(_norm_text(value))
    if direction is not None:
        direction = _raw_text(direction).lower()
        if direction not in VALID_DIRECTIONS:
            raise HistoricalPriceError(f"不支持的结算方向: {direction!r}")
        clauses.append("direction=?")
        params.append(direction)
    for column, value in (
        ("region", region),
        ("observed_at", observed_at),
        ("project_type", project_type),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(_raw_text(value))
    if source_project_id is not None:
        clauses.append("source_project_id=?")
        params.append(int(source_project_id))
    rows = conn.execute(
        "SELECT * FROM historical_unit_prices WHERE " + " AND ".join(clauses)
        + " ORDER BY normalized_name, normalized_feature, normalized_unit, id",
        tuple(params),
    ).fetchall()
    records: list[HistoricalPriceRecord] = []
    for row in rows:
        valid, reasons = _historical_snapshot_integrity(conn, row)
        if not valid and not include_revoked:
            # 旧版本数据库可能在 v40 之前写入过没有来源勾稽的资产；它们
            # 仍留在档案表，但不得进入当前价格提示或统计。
            continue
        if valid:
            record = _row_to_price(row)
            if record.status == "invalid" and not include_revoked:
                # 快照链仍闭合并不代表字段值没有被外部脚本篡改；损坏的
                # 单价只能留在 include_revoked=True 的档案读取面，不能进入
                # 当前价格提示、样本数或中位数。
                continue
            records.append(record)
            continue
        payload = dict(row)
        metadata = _loads(payload.get("metadata_json"), {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.update({"integrity_status": "invalid", "integrity_reasons": list(reasons)})
        payload["metadata_json"] = _json(metadata)
        payload["status"] = "invalid"
        record = _row_to_price(payload)
        if record.status == "invalid" and not include_revoked:
            continue
        records.append(record)
    return records


def _median(values: list[D]) -> D:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / D("2")


def query_historical_price_hint(
    conn: sqlite3.Connection,
    *,
    name: str,
    feature: str = "",
    unit: str = "",
    direction: str,
    current_unit_price: Any = None,
    region: str = "",
    observed_at: str | None = None,
    project_type: str = "",
) -> HistoricalPriceHint:
    """按严格维度查询历史单价统计；结果始终标注 ``review_only``。"""
    direction = _raw_text(direction).lower()
    if direction not in VALID_DIRECTIONS:
        raise HistoricalPriceError(f"不支持的结算方向: {direction!r}")
    normalized_name = _norm_text(name)
    normalized_feature = _norm_text(feature)
    normalized_unit = _norm_text(unit)
    region = _raw_text(region)
    observed_at = _raw_text(observed_at) or None
    project_type = _raw_text(project_type)
    base_rows = list_historical_unit_prices(
        conn,
        name=name,
        feature=feature,
        unit=unit,
        direction=direction,
        include_revoked=False,
    )
    reasons: set[str] = set()
    comparable: list[HistoricalPriceRecord] = []
    for row in base_rows:
        row_region = _raw_text(row.region)
        row_time = _raw_text(row.observed_at) or None
        row_type = _raw_text(row.project_type)
        if not region or not row_region:
            reasons.add("地区缺失，不能直接比较")
        elif region != row_region:
            reasons.add("地区不一致，不能直接比较")
        if not observed_at or not row_time:
            reasons.add("时间缺失，不能直接比较")
        elif observed_at != row_time:
            reasons.add("时间不一致，不能直接比较")
        if not project_type or not row_type:
            reasons.add("项目类型缺失，不能直接比较")
        elif project_type != row_type:
            reasons.add("项目类型不一致，不能直接比较")
        if (
            region and row_region and region == row_region
            and observed_at and row_time and observed_at == row_time
            and project_type and row_type and project_type == row_type
        ):
            comparable.append(row)
    values = [D(str(row.unit_price)) for row in comparable]
    current_price, current_reason = _optional_decimal(current_unit_price)
    current_decimal = D(current_price) if current_price is not None else None
    median = _median(values) if values else None
    deviation = current_decimal - median if current_decimal is not None and median is not None else None
    deviation_rate = (
        deviation / median
        if deviation is not None and median is not None and median != D("0")
        else None
    )
    if current_reason:
        reasons.add("当前单价缺失或无法解析，无法计算偏差")
    if not base_rows:
        status = "not_comparable"
        reasons.add("没有规范化名称、特征、单位和方向均匹配的历史样本")
    elif not comparable:
        status = "not_comparable"
    else:
        status = "available"
    evidence_ids = tuple(sorted({int(row.evidence_id) for row in comparable or base_rows}))
    return HistoricalPriceHint(
        status=status,
        review_only=True,
        normalized_name=normalized_name,
        normalized_feature=normalized_feature,
        normalized_unit=normalized_unit,
        direction=direction,
        region=region,
        observed_at=observed_at,
        project_type=project_type,
        sample_count=len(base_rows),
        comparable_count=len(comparable),
        incomparable_count=len(base_rows) - len(comparable),
        min_price=min(values) if values else None,
        max_price=max(values) if values else None,
        median_price=median,
        current_price=current_decimal,
        deviation_from_median=deviation,
        deviation_rate=deviation_rate,
        reasons=tuple(sorted(reasons)),
        evidence_ids=evidence_ids,
    )


def price_hint_for_line_item(
    conn: sqlite3.Connection,
    line_item: sqlite3.Row | dict[str, Any],
    *,
    region: str = "",
    observed_at: str | None = None,
    project_type: str = "",
) -> HistoricalPriceHint:
    """对当前清单行提供历史价格复核提示，不修改当前清单。"""
    get = line_item.__getitem__
    return query_historical_price_hint(
        conn,
        name=_raw_text(get("name")),
        feature=_raw_text(get("feature")),
        unit=_raw_text(get("unit")),
        direction=_raw_text(get("direction") or "unknown"),
        current_unit_price=get("unit_price"),
        region=region,
        observed_at=observed_at,
        project_type=project_type,
    )
