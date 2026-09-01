"""项目送审版本链和版本间清单比较。

版本是同一项目内的不可覆盖事实快照。每次创建版本都会复制当前清单的
数量、单价、合价、字段、方向和来源定位；版本之间的比较只使用
``Decimal``，待确认/不可比项目不计入确认后的净金额影响。
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from jiadun.core.contracts import run_contract
from jiadun.core.evidence import audit as audit_log
from jiadun.core.evidence import evidence as evidence_api
from jiadun.core.evidence.finding import stable_fingerprint

D = Decimal

VERSION_KINDS = {
    "initial_submission": "第一次送审",
    "supplement": "补充资料",
    "resubmission": "第二次送审",
    "final_approval": "最终审定",
}

_FIELD_CATEGORIES = {
    "quantity": "quantity_changed",
    "unit_price": "unit_price_changed",
    "amount": "amount_changed",
    "code": "code_changed",
    "name": "name_changed",
    "feature": "feature_changed",
    "unit": "unit_changed",
}
_FIELD_ORDER = ("code", "name", "feature", "unit", "quantity", "unit_price", "amount")
_UNCERTAIN_FLAGS = {
    "needs_review",
    "pending_review",
    "unrecognized",
    "parse_failed",
    # extract_items.py 将无法解析的原文保留在 *_unparsed 标记中；版本
    # 比较必须把这类金额/数量/单价视为待确认，不能仅因 flags 中没有
    # needs_review 就把不可计算的差异标成 confirmed。
    "quantity_unparsed",
    "unit_price_unparsed",
    "amount_unparsed",
    "orphan_numeric_row",
}


class ProjectVersionError(ValueError):
    """项目版本输入、归属或比较范围无效。"""


@dataclass(frozen=True)
class ProjectVersion:
    version_id: int
    project_id: int
    version_no: int
    version_kind: str
    title: str
    description: str
    source_manifest_id: int | None
    snapshot_sha256: str
    item_count: int
    created_by: str
    created_at: str
    reason: str
    run_id: str | None
    run_signature: str | None
    evidence_id: int | None
    audit_id: int | None
    metadata: dict[str, Any]
    is_current_run: bool = False

    @property
    def version_label(self) -> str:
        return VERSION_KINDS.get(self.version_kind, self.version_kind)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.version_id,
            "project_id": self.project_id,
            "version_no": self.version_no,
            "version_kind": self.version_kind,
            "version_label": self.version_label,
            "title": self.title,
            "description": self.description,
            "source_manifest_id": self.source_manifest_id,
            "snapshot_sha256": self.snapshot_sha256,
            "item_count": self.item_count,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "reason": self.reason,
            "run_id": self.run_id,
            "run_signature": self.run_signature,
            "evidence_id": self.evidence_id,
            "audit_id": self.audit_id,
            "metadata": dict(self.metadata),
            "is_current_run": self.is_current_run,
        }


@dataclass(frozen=True)
class VersionDiffItem:
    identity_key: str
    category: str
    categories: tuple[str, ...]
    status: str
    reason: str
    baseline: dict[str, Any]
    current: dict[str, Any]
    amount_impact: Decimal | None
    confirmed_amount_impact: Decimal | None
    evidence_ids: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity_key": self.identity_key,
            "category": self.category,
            "categories": list(self.categories),
            "status": self.status,
            "reason": self.reason,
            "baseline": dict(self.baseline),
            "current": dict(self.current),
            "amount_impact": str(self.amount_impact) if self.amount_impact is not None else None,
            "confirmed_amount_impact": (
                str(self.confirmed_amount_impact)
                if self.confirmed_amount_impact is not None else None
            ),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class VersionComparison:
    project_id: int
    baseline: ProjectVersion
    current: ProjectVersion
    items: tuple[VersionDiffItem, ...]
    category_summary: dict[str, dict[str, Any]]
    confirmed_net_amount_impact: Decimal | None
    status: str
    evidence_ids: tuple[int, ...]
    reason_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "baseline": self.baseline.as_dict(),
            "current": self.current.as_dict(),
            "items": [item.as_dict() for item in self.items],
            "category_summary": {
                key: {
                    **value,
                    "amount_impact": (
                        str(value["amount_impact"])
                        if isinstance(value.get("amount_impact"), D)
                        else value.get("amount_impact")
                    ),
                }
                for key, value in self.category_summary.items()
            },
            "confirmed_net_amount_impact": (
                str(self.confirmed_net_amount_impact)
                if self.confirmed_net_amount_impact is not None else None
            ),
            "status": self.status,
            "evidence_ids": list(self.evidence_ids),
            "reason_codes": list(self.reason_codes),
        }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _loads(value: str | None, fallback: Any) -> Any:
    try:
        result = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _row_to_version(row: sqlite3.Row, *, current_run_id: str | None = None) -> ProjectVersion:
    return ProjectVersion(
        version_id=int(row["id"]),
        project_id=int(row["project_id"]),
        version_no=int(row["version_no"]),
        version_kind=str(row["version_kind"]),
        title=str(row["title"]),
        description=str(row["description"] or ""),
        source_manifest_id=(
            int(row["source_manifest_id"]) if row["source_manifest_id"] is not None else None
        ),
        snapshot_sha256=str(row["snapshot_sha256"]),
        item_count=int(row["item_count"] or 0),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        reason=str(row["reason"]),
        run_id=row["run_id"],
        run_signature=row["run_signature"],
        evidence_id=(int(row["evidence_id"]) if row["evidence_id"] is not None else None),
        audit_id=(int(row["audit_id"]) if row["audit_id"] is not None else None),
        metadata=_loads(row["metadata_json"], {}),
        is_current_run=bool(current_run_id and row["run_id"] == current_run_id),
    )


def get_project_version(
    conn: sqlite3.Connection,
    project_id: int,
    version_id: int,
) -> ProjectVersion | None:
    current = run_contract.get_current_contract(conn, project_id)
    row = conn.execute(
        "SELECT * FROM project_versions WHERE project_id=? AND id=?",
        (int(project_id), int(version_id)),
    ).fetchone()
    return _row_to_version(row, current_run_id=current.run_id if current else None) if row else None


def list_project_versions(
    conn: sqlite3.Connection,
    project_id: int,
) -> list[ProjectVersion]:
    current = run_contract.get_current_contract(conn, project_id)
    return [
        _row_to_version(row, current_run_id=current.run_id if current else None)
        for row in conn.execute(
            "SELECT * FROM project_versions WHERE project_id=? ORDER BY version_no, id",
            (int(project_id),),
        ).fetchall()
    ]


def _line_item_rows(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT li.id, li.period_id, sp.period_no, sp.direction,
                  li.code, li.name, li.feature, li.unit, li.quantity,
                  li.unit_price, li.amount, li.flags_json,
                  sf.id AS source_file_id, rs.id AS source_sheet_id
           FROM line_items li
           JOIN settlement_periods sp ON sp.id=li.period_id
           LEFT JOIN raw_sheets rs ON rs.id=li.sheet_id
           LEFT JOIN parse_batches pb ON pb.id=rs.batch_id
           LEFT JOIN source_files sf ON sf.id=pb.file_id
           WHERE sp.project_id=? ORDER BY li.id""",
        (int(project_id),),
    ).fetchall()
    counters: defaultdict[str, int] = defaultdict(int)
    result: list[dict[str, Any]] = []
    for row in rows:
        flags = _loads(row["flags_json"], {})
        if not isinstance(flags, dict):
            flags = {"raw_flags": flags}
        code = str(row["code"] or "").strip().casefold()
        name = "".join(str(row["name"] or "").strip().split()).casefold()
        feature = "".join(str(row["feature"] or "").strip().split()).casefold()
        unit = "".join(str(row["unit"] or "").strip().split()).casefold()
        identity_key = (
            f"code:{code}" if code else f"name:{name}|feature:{feature}|unit:{unit}"
        )
        counters[identity_key] += 1
        source_evidence_id = flags.get("source_evidence_id")
        try:
            source_evidence_id = int(source_evidence_id) if source_evidence_id is not None else None
        except (TypeError, ValueError):
            source_evidence_id = None
        result.append({
            "id": int(row["id"]),
            "period_id": int(row["period_id"]),
            "period_no": int(row["period_no"]),
            "direction": row["direction"] or "unknown",
            "code": row["code"],
            "name": row["name"],
            "feature": row["feature"],
            "unit": row["unit"],
            "quantity": row["quantity"],
            "unit_price": row["unit_price"],
            "amount": row["amount"],
            "flags": flags,
            "identity_key": identity_key,
            "occurrence": counters[identity_key],
            "source_file_id": row["source_file_id"],
            "source_sheet_id": row["source_sheet_id"],
            "source_row": flags.get("row") or flags.get("source_row"),
            "source_evidence_id": source_evidence_id,
        })
    return result


def _snapshot_items(
    conn: sqlite3.Connection,
    project_id: int,
    version_id: int,
    rows: list[dict[str, Any]],
) -> None:
    conn.executemany(
        """INSERT INTO project_version_items(
               version_id, project_id, identity_key, occurrence, period_id, period_no,
               direction, line_item_id, code, name, feature, unit, quantity, unit_price,
               amount, flags_json, source_file_id, source_sheet_id, source_row,
               source_evidence_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                version_id, project_id, item["identity_key"], item["occurrence"],
                item["period_id"], item["period_no"], item["direction"], item["id"],
                item["code"], item["name"], item["feature"], item["unit"],
                item["quantity"], item["unit_price"], item["amount"],
                _json(item["flags"]), item["source_file_id"], item["source_sheet_id"],
                item["source_row"], item["source_evidence_id"], _now(),
            )
            for item in rows
        ],
    )


def create_project_version(
    conn: sqlite3.Connection,
    project_id: int,
    version_kind: str,
    title: str,
    *,
    created_by: str,
    reason: str,
    description: str = "",
    source_manifest_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> ProjectVersion:
    """追加一个项目版本，并将清单快照、Evidence、Audit 原子落库。"""
    kind = str(version_kind or "").strip().lower()
    if kind not in VERSION_KINDS:
        raise ProjectVersionError(f"不支持的项目版本类型: {version_kind!r}")
    title = str(title or "").strip()
    created_by = str(created_by or "").strip()
    reason = str(reason or "").strip()
    if not title:
        raise ProjectVersionError("项目版本标题不能为空")
    if not created_by:
        raise ProjectVersionError("项目版本必须记录创建人")
    if not reason:
        raise audit_log.AuditReasonRequiredError("创建项目版本必须记录原因")
    if source_manifest_id is not None:
        manifest = conn.execute(
            "SELECT id FROM import_manifests WHERE id=? AND project_id=?",
            (int(source_manifest_id), int(project_id)),
        ).fetchone()
        if manifest is None:
            raise ProjectVersionError("source_manifest_id 不属于当前项目")
    else:
        latest_manifest = conn.execute(
            """SELECT id FROM import_manifests WHERE project_id=?
               ORDER BY declared_at DESC, id DESC LIMIT 1""",
            (int(project_id),),
        ).fetchone()
        source_manifest_id = int(latest_manifest["id"]) if latest_manifest else None
    rows = _line_item_rows(conn, project_id)
    snapshot_payload = [
        {
            key: item[key]
            for key in (
                "identity_key", "occurrence", "period_id", "period_no", "direction",
                "code", "name", "feature", "unit", "quantity", "unit_price", "amount",
                "flags", "source_file_id", "source_sheet_id", "source_row", "source_evidence_id",
            )
        }
        for item in rows
    ]
    snapshot_sha256 = stable_fingerprint(snapshot_payload)
    version_row = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS n FROM project_versions WHERE project_id=?",
        (int(project_id),),
    ).fetchone()
    version_no = int(version_row["n"] or 0) + 1
    # 即使项目尚未有运行记录，也先固化一个基线契约。这样新建版本时
    # Evidence/Audit/版本三者从第一条 INSERT 起就有同一 run 身份；随后
    # 版本自身进入契约组件会产生新的最终运行，再由下方统一重绑定。
    old_contract = run_contract.ensure_run_contract(conn, project_id)
    old_run_id = old_contract.run_id if old_contract else None
    old_signature = old_contract.signature if old_contract else None
    now = _now()
    metadata_payload = {
        **(metadata or {}),
        "version_label": VERSION_KINDS[kind],
        "snapshot_item_count": len(rows),
    }
    with run_contract._transaction(conn, "create_project_version"):
        cur = conn.execute(
            """INSERT INTO project_versions(
                   project_id, version_no, version_kind, title, description,
                   source_manifest_id, snapshot_sha256, item_count, created_by,
                   created_at, reason, run_id, run_signature, metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(project_id), version_no, kind, title, str(description or "").strip(),
                source_manifest_id, snapshot_sha256, len(rows), created_by, now, reason,
                old_run_id, old_signature, _json(metadata_payload),
            ),
        )
        version_id = int(cur.lastrowid)
        _snapshot_items(conn, int(project_id), version_id, rows)
        evidence_id = evidence_api.add_evidence(
            conn,
            int(project_id),
            "project_version",
            f"项目版本 v{version_no}「{title}」：{VERSION_KINDS[kind]}",
            steps=[
                {
                    "step": "冻结当前清单为项目版本快照",
                    "version_id": version_id,
                    "version_no": version_no,
                    "version_kind": kind,
                    "item_count": len(rows),
                    "snapshot_sha256": snapshot_sha256,
                    "reason": reason,
                }
            ],
            sources=[{
                "version_id": version_id,
                "source_manifest_id": source_manifest_id,
                "line_item_count": len(rows),
            }],
            commit=False,
            run_signature=old_signature,
            run_id=old_run_id,
            scope="human",
        )
        audit_id = audit_log.record_audit(
            conn,
            int(project_id),
            created_by,
            "create_project_version",
            f"project_version:{version_id}",
            None,
            {
                "version_id": version_id,
                "version_no": version_no,
                "version_kind": kind,
                "title": title,
                "item_count": len(rows),
                "snapshot_sha256": snapshot_sha256,
                "evidence_id": evidence_id,
            },
            reason,
            commit=False,
            run_signature=old_signature,
            run_id=old_run_id,
        )
        conn.execute(
            "UPDATE project_versions SET evidence_id=?, audit_id=? WHERE id=? AND project_id=?",
            (evidence_id, audit_id, version_id, int(project_id)),
        )
        final_contract = run_contract.ensure_run_contract(conn, int(project_id))
        # v35 的数据库闸门要求版本的运行身份与其 Evidence/Audit 同步。
        # 先把两条责任记录绑定到最终契约，再更新版本行，避免中间出现
        # “版本已是新 run、证据仍是旧 run”的半绑定窗口。
        conn.execute(
            """UPDATE evidence SET run_signature=?, run_id=?
               WHERE id=? AND project_id=?""",
            (final_contract.signature, final_contract.run_id, evidence_id, int(project_id)),
        )
        conn.execute(
            """UPDATE audit_log SET run_signature=?, run_id=?
               WHERE id=? AND project_id=?""",
            (final_contract.signature, final_contract.run_id, audit_id, int(project_id)),
        )
        conn.execute(
            """UPDATE project_versions SET run_signature=?, run_id=?
               WHERE id=? AND project_id=?""",
            (final_contract.signature, final_contract.run_id, version_id, int(project_id)),
        )
    result = get_project_version(conn, int(project_id), version_id)
    if result is None:
        raise RuntimeError("项目版本已写入但无法重新读取")
    return result


def _version_items(conn: sqlite3.Connection, project_id: int, version_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM project_version_items
           WHERE project_id=? AND version_id=? ORDER BY id""",
        (int(project_id), int(version_id)),
    ).fetchall()
    result = []
    for row in rows:
        flags = _loads(row["flags_json"], {})
        result.append({
            "id": int(row["id"]),
            "version_id": int(row["version_id"]),
            "identity_key": row["identity_key"],
            "occurrence": int(row["occurrence"]),
            "period_id": row["period_id"],
            "period_no": row["period_no"],
            "direction": row["direction"] or "unknown",
            "line_item_id": row["line_item_id"],
            "code": row["code"],
            "name": row["name"],
            "feature": row["feature"],
            "unit": row["unit"],
            "quantity": row["quantity"],
            "unit_price": row["unit_price"],
            "amount": row["amount"],
            "flags": flags if isinstance(flags, dict) else {"raw_flags": flags},
            "source_file_id": row["source_file_id"],
            "source_sheet_id": row["source_sheet_id"],
            "source_row": row["source_row"],
            "source_evidence_id": row["source_evidence_id"],
        })
    return result


def _norm(value: Any) -> str:
    return "".join(str(value or "").strip().split()).casefold()


def _dec(value: Any) -> D | None:
    if value is None or value == "":
        return None
    try:
        return D(str(value))
    except Exception:
        return None


def _field_equal(left: Any, right: Any, field: str) -> bool:
    if field in {"quantity", "unit_price", "amount"}:
        l_dec, r_dec = _dec(left), _dec(right)
        return l_dec is not None and r_dec is not None and l_dec == r_dec or l_dec is None and r_dec is None
    return _norm(left) == _norm(right)


def _uncertain(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    flags = row.get("flags") or {}
    if any(bool(flags.get(key)) for key in _UNCERTAIN_FLAGS):
        return True
    # 兼容人工/旧脚本直接写入坏数值但未同步 flags 的场景。空值可以是
    # 合法缺失，只有非空且无法走 Decimal 入口时才判为不确定。
    return any(
        row.get(field) not in (None, "") and _dec(row.get(field)) is None
        for field in ("quantity", "unit_price", "amount")
    )


def _amount_unavailable(row: dict[str, Any] | None) -> bool:
    """金额缺失/损坏时禁止把版本比较整体标成 available。"""
    return bool(row) and (row.get("amount") in (None, "") or _dec(row.get("amount")) is None)


def _json_has_ref(value: str | None, key: str, expected: int) -> bool:
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


def _match_key(row: dict[str, Any], mode: str) -> tuple[str, ...] | None:
    # 期间和方向是清单变更的业务边界。同名/同编码项目跨期出现时，只能
    # 分别归入删除+新增，不能把不同期间的金额变化强行解释成同一项调整。
    period = row.get("period_id") if row.get("period_id") is not None else row.get("period_no")
    period_scope = str(period) if period is not None else "unknown"
    direction_scope = _norm(row.get("direction") or "unknown")
    if mode == "code":
        key = _norm(row.get("code"))
        return (period_scope, direction_scope, key) if key else None
    if mode == "name_feature_unit":
        key = tuple(_norm(row.get(name)) for name in ("name", "feature", "unit"))
        return (period_scope, direction_scope, *key) if key[0] else None
    if mode == "name_feature":
        key = tuple(_norm(row.get(name)) for name in ("name", "feature"))
        return (period_scope, direction_scope, *key) if key[0] else None
    raise ValueError(mode)


def _pair_rows(
    baseline: list[dict[str, Any]],
    current: list[dict[str, Any]],
) -> list[tuple[dict[str, Any] | None, dict[str, Any] | None, bool]]:
    left = set(range(len(baseline)))
    right = set(range(len(current)))
    pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None, bool]] = []
    ambiguous_logical_keys: set[tuple[str, str]] = set()
    for mode in ("code", "name_feature_unit", "name_feature"):
        left_groups: defaultdict[tuple[str, ...], list[int]] = defaultdict(list)
        right_groups: defaultdict[tuple[str, ...], list[int]] = defaultdict(list)
        for index in sorted(left):
            key = _match_key(baseline[index], mode)
            if key is not None:
                left_groups[key].append(index)
        for index in sorted(right):
            key = _match_key(current[index], mode)
            if key is not None:
                right_groups[key].append(index)
        for key in sorted(set(left_groups) & set(right_groups)):
            li, ri = left_groups[key], right_groups[key]
            ambiguous = len(li) != 1 or len(ri) != 1
            for left_index, right_index in zip(li, ri, strict=False):
                pairs.append((baseline[left_index], current[right_index], ambiguous))
                left.discard(left_index)
                right.discard(right_index)
            if ambiguous:
                # 重复键的一侧多、一侧少时，zip 后剩余行不能直接变成
                # confirmed deleted/added；无法知道究竟是哪一条被调整，整组
                # 都必须保持 pending，确认净影响不得吸收这类金额。
                for left_index in li[len(ri):]:
                    pairs.append((baseline[left_index], None, True))
                    left.discard(left_index)
                for right_index in ri[len(li):]:
                    pairs.append((None, current[right_index], True))
                    right.discard(right_index)
    # 方向是版本比较的业务边界。若同一清单逻辑键和期次在两个版本中从
    # “对上”变为“对下”（或反向），不能把它拆成 confirmed 删除+新增，
    # 否则两侧金额会被错误吸收进确认净影响。剩余行先按忽略方向的逻辑键
    # 和期次分组，唯一一对方向变化标记 incomparable；重复组全部 pending。
    baseline_scope: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
    current_scope: defaultdict[tuple[str, str], list[int]] = defaultdict(list)

    def _period_scope(row: dict[str, Any]) -> str:
        if row.get("period_id") is not None:
            return f"id:{row['period_id']}"
        if row.get("period_no") is not None:
            return f"no:{row['period_no']}"
        return "unknown"

    def _scope_key(row: dict[str, Any]) -> tuple[str, str]:
        return (_norm(row.get("identity_key")), _period_scope(row))

    for index in sorted(left):
        baseline_scope[_scope_key(baseline[index])].append(index)
    for index in sorted(right):
        current_scope[_scope_key(current[index])].append(index)
    for key in sorted(set(baseline_scope) & set(current_scope)):
        left_group = baseline_scope[key]
        right_group = current_scope[key]
        if not any(
            _norm(baseline[left_index].get("direction") or "unknown")
            != _norm(current[right_index].get("direction") or "unknown")
            for left_index in left_group
            for right_index in right_group
        ):
            continue
        ambiguous = len(left_group) != 1 or len(right_group) != 1
        for left_index, right_index in zip(left_group, right_group, strict=False):
            pairs.append((baseline[left_index], current[right_index], ambiguous))
            left.discard(left_index)
            right.discard(right_index)
        if ambiguous:
            for left_index in left_group[len(right_group):]:
                pairs.append((baseline[left_index], None, True))
                left.discard(left_index)
            for right_index in right_group[len(left_group):]:
                pairs.append((None, current[right_index], True))
                right.discard(right_index)
    # 即便编码/名称相同，跨期项目也不能直接算成数量或金额调整。把可疑
    # 的同逻辑项先配成一对，由 _diff_item 标记 incomparable；若一侧有
    # 多条，剩余项仍以 ambiguous=True 保持 pending，不能进入确认净额。
    for left_index in sorted(tuple(left)):
        if left_index not in left:
            continue
        for right_index in sorted(tuple(right)):
            if right_index not in right:
                continue
            left_row, right_row = baseline[left_index], current[right_index]
            if (
                _norm(left_row.get("identity_key"))
                and _norm(left_row.get("identity_key")) == _norm(right_row.get("identity_key"))
                and _norm(left_row.get("direction") or "unknown")
                == _norm(right_row.get("direction") or "unknown")
                and (
                    left_row.get("period_id") != right_row.get("period_id")
                    or left_row.get("period_no") != right_row.get("period_no")
                )
            ):
                pairs.append((left_row, right_row, False))
                ambiguous_logical_keys.add(
                    (
                        _norm(left_row.get("identity_key")),
                        _norm(left_row.get("direction") or "unknown"),
                    )
                )
                left.discard(left_index)
                right.discard(right_index)
                break
    for index in sorted(left):
        logical_key = (
            _norm(baseline[index].get("identity_key")),
            _norm(baseline[index].get("direction") or "unknown"),
        )
        pairs.append((baseline[index], None, logical_key in ambiguous_logical_keys))
    for index in sorted(right):
        logical_key = (
            _norm(current[index].get("identity_key")),
            _norm(current[index].get("direction") or "unknown"),
        )
        pairs.append((None, current[index], logical_key in ambiguous_logical_keys))
    return pairs


def _source_evidence_ids(*rows: dict[str, Any] | None) -> tuple[int, ...]:
    return tuple(sorted({
        int(row["source_evidence_id"])
        for row in rows
        if row and row.get("source_evidence_id") is not None
    }))


def _display_row(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        key: row.get(key)
        for key in (
            "line_item_id", "period_id", "period_no", "direction", "code", "name",
            "feature", "unit", "quantity", "unit_price", "amount", "source_file_id",
            "source_sheet_id", "source_row", "source_evidence_id",
        )
    }


def _diff_item(
    baseline: dict[str, Any] | None,
    current: dict[str, Any] | None,
    ambiguous: bool,
) -> VersionDiffItem:
    row = current or baseline or {}
    identity = str(row.get("identity_key") or "unidentified")
    if baseline is None:
        categories = ("added",)
        reason = "当前版本新增清单项"
    elif current is None:
        categories = ("deleted",)
        reason = "当前版本删除清单项"
    else:
        categories = tuple(
            _FIELD_CATEGORIES[field]
            for field in _FIELD_ORDER
            if not _field_equal(baseline.get(field), current.get(field), field)
        )
        reason = "；".join(categories) if categories else "字段未变化"
        if baseline.get("direction") != current.get("direction"):
            categories = (*categories, "incomparable")
            reason = f"方向变化：{baseline.get('direction')} → {current.get('direction')}"
    if ambiguous:
        status = "pending"
        category = "pending"
        reason = "匹配键出现多行，未强行确认一对一对应关系"
    elif baseline and current and (
        baseline.get("direction") != current.get("direction")
        or baseline.get("period_id") != current.get("period_id")
        or baseline.get("period_no") != current.get("period_no")
    ):
        status = "incomparable"
        category = "incomparable"
    elif _uncertain(baseline) or _uncertain(current) or _amount_unavailable(baseline) or _amount_unavailable(current):
        status = "pending"
        category = "pending"
        reason = f"{reason}；存在待人工确认、金额缺失或解析不确定标记"
    else:
        status = "confirmed"
        category = categories[0] if categories else "unchanged"
    baseline_amount = _dec(baseline.get("amount")) if baseline else None
    current_amount = _dec(current.get("amount")) if current else None
    impact = None
    if ambiguous:
        # 重复键归属不确定，不能把候选增删金额展示成可确认影响。
        impact = None
    elif baseline is None:
        impact = current_amount
    elif current is None:
        impact = -baseline_amount if baseline_amount is not None else None
    elif baseline_amount is not None and current_amount is not None:
        impact = current_amount - baseline_amount
    confirmed_impact = impact if status == "confirmed" else None
    return VersionDiffItem(
        identity_key=identity,
        category=category,
        categories=tuple(categories),
        status=status,
        reason=reason,
        baseline=_display_row(baseline),
        current=_display_row(current),
        amount_impact=impact,
        confirmed_amount_impact=confirmed_impact,
        evidence_ids=_source_evidence_ids(baseline, current),
    )


def _version_integrity(
    conn: sqlite3.Connection,
    version: ProjectVersion,
) -> tuple[bool, tuple[str, ...]]:
    """验证版本快照的运行、Evidence/Audit 和内容哈希是否闭合。

    版本 API 会按“先写事实、再绑定最终运行”的顺序落库；比较入口不能
    只相信 ``project_versions`` 自己的字段。旧脚本若留下未绑定版本、跨
    运行 Evidence/Audit、缺行或篡改后的快照，比较仍可作为排查线索返回，
    但不得报告 available 或生成确认净金额。
    """
    reasons: list[str] = []
    if not version.run_id or not version.run_signature:
        reasons.append("version_run_unbound")
    else:
        run = conn.execute(
            """SELECT 1 FROM run_contracts
                WHERE project_id=? AND run_id=? AND signature=? LIMIT 1""",
            (version.project_id, version.run_id, version.run_signature),
        ).fetchone()
        if run is None:
            reasons.append("version_run_missing")
    evidence = None
    if version.evidence_id is None:
        reasons.append("version_evidence_missing")
    else:
        evidence = conn.execute(
            """SELECT project_id, kind, scope, run_id, run_signature,
                              steps_json, sources_json
                 FROM evidence WHERE id=?""",
            (version.evidence_id,),
        ).fetchone()
        if (
            evidence is None
            or evidence["project_id"] != version.project_id
            or evidence["kind"] != "project_version"
            or evidence["scope"] not in {"current", "human", "historical"}
            or (
                version.version_kind == "final_approval"
                and evidence["scope"] not in {"current", "human"}
            )
            or evidence["run_id"] != version.run_id
            or evidence["run_signature"] != version.run_signature
            or not _json_has_ref(evidence["steps_json"], "version_id", version.version_id)
            or not _json_has_ref(evidence["sources_json"], "version_id", version.version_id)
        ):
            reasons.append("version_evidence_mismatch")
        if (
            evidence is not None
            and version.version_kind == "final_approval"
            and evidence["scope"] not in {"current", "human"}
        ):
            reasons.append("final_version_evidence_not_current")
    if version.audit_id is None:
        reasons.append("version_audit_missing")
    else:
        audit = conn.execute(
            """SELECT project_id, action, target, run_id, run_signature, after_json
                 FROM audit_log WHERE id=?""",
            (version.audit_id,),
        ).fetchone()
        if (
            audit is None
            or audit["project_id"] != version.project_id
            or audit["action"] != "create_project_version"
            or audit["target"] != f"project_version:{version.version_id}"
            or audit["run_id"] != version.run_id
            or audit["run_signature"] != version.run_signature
            or not _json_has_ref(audit["after_json"], "version_id", version.version_id)
        ):
            reasons.append("version_audit_mismatch")
    items = _version_items(conn, version.project_id, version.version_id)
    if len(items) != version.item_count:
        reasons.append("version_item_count_mismatch")
    # 版本快照是长期资产，不能只凭 item_count/hash 证明其业务范围。旧脚本
    # 可能写入没有期次、方向或任何来源定位的“金额行”；这类行即使 hash
    # 自洽，也不能进入确认后的版本差异。line_item_id 是快照时的历史引用，
    # 原清单后来删除并不影响快照本身，因此这里只要求它或其它来源定位字段
    # 曾被保留下来，不回查会随项目重算而变化的当前 line_items。
    scope_reasons: set[str] = set()
    for item in items:
        if item.get("period_id") is None or item.get("period_no") is None:
            scope_reasons.add("version_item_period_missing")
        if str(item.get("direction") or "unknown") not in {"upward", "downward"}:
            scope_reasons.add("version_item_direction_missing")
        has_historical_line_item = item.get("line_item_id") not in (None, "")
        has_explicit_source = all(
            item.get(field) not in (None, "")
            for field in (
                "source_file_id", "source_sheet_id", "source_row", "source_evidence_id",
            )
        )
        if not (has_historical_line_item or has_explicit_source):
            scope_reasons.add("version_item_source_missing")
        if item.get("source_evidence_id") not in (None, ""):
            source_evidence = conn.execute(
                """SELECT project_id, kind, sources_json
                     FROM evidence WHERE id=?""",
                (item.get("source_evidence_id"),),
            ).fetchone()
            if (
                source_evidence is None
                or int(source_evidence["project_id"]) != int(version.project_id)
                or source_evidence["kind"] not in {"line_item_source", "sheet_coverage_proof"}
            ):
                scope_reasons.add("version_item_source_evidence_mismatch")
            else:
                try:
                    source_entries = json.loads(source_evidence["sources_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    source_entries = []
                source_entries = source_entries if isinstance(source_entries, list) else [source_entries]
                def _same_location(
                    entry: Any, snapshot_item: dict[str, Any] = item
                ) -> bool:
                    if not isinstance(entry, dict):
                        return False
                    for field in ("file_id", "sheet_id"):
                        expected = snapshot_item.get(f"source_{field}")
                        if expected not in (None, "") and str(entry.get(field)) != str(expected):
                            return False
                    expected_row = snapshot_item.get("source_row")
                    if expected_row in (None, ""):
                        return True
                    if entry.get("row") is not None:
                        return str(entry["row"]) == str(expected_row)
                    # 覆盖证明来源可能只保存一个行范围；允许该范围明确涵盖
                    # 快照行，但不接受只有文件/Sheet、没有任何行定位的证据。
                    try:
                        return (
                            entry.get("row_start") is not None
                            and entry.get("row_end") is not None
                            and int(entry["row_start"]) <= int(expected_row) <= int(entry["row_end"])
                        )
                    except (TypeError, ValueError):
                        return False

                if not any(_same_location(entry) for entry in source_entries):
                    scope_reasons.add("version_item_source_evidence_location_missing")
                # 没有 line_item_id 时，source_file/sheet/row 只能证明“指向
                # 某处”，还不能证明快照中的金额、数量、单价和文本来自该处。
                # 导入器生成的 line_item_source Evidence 会按字段保存 raw/value；
                # 逐字段 Decimal/规范文本回读，防止直接 SQL 用真实定位拼接一
                # 个伪造的 999 快照。覆盖证明类 Evidence 没有逐字段值时，
                # 保守降级为不可用于当前版本完整性。
                if not has_historical_line_item:
                    if source_evidence["kind"] != "line_item_source":
                        scope_reasons.add("version_item_source_evidence_values_missing")
                    else:
                        field_entries: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
                        for entry in source_entries:
                            if not _same_location(entry) or not isinstance(entry, dict):
                                continue
                            field_name = str(entry.get("field") or "")
                            if field_name in {"code", "name", "feature", "unit", "quantity", "unit_price", "amount"}:
                                field_entries[field_name].append(entry)
                                # Evidence 的金额/文本不是独立事实。原始网格
                                # 是证据根（raw_cells 迁移 v39 后不可更新/删除）；
                                # 逐字段回读，避免直接 UPDATE evidence.sources_json
                                # 把同一定位改成伪造值后再生成“自洽”版本。
                                raw_value = entry.get("raw_value", entry.get("raw"))
                                row_value = entry.get("row")
                                col_value = entry.get("col") or entry.get("amount_col")
                                if raw_value is None or row_value is None or col_value is None:
                                    scope_reasons.add("version_item_source_evidence_cell_missing")
                                    continue
                                try:
                                    cell = conn.execute(
                                        """SELECT raw_value, cached_value, is_formula
                                             FROM raw_cells
                                            WHERE sheet_id=? AND row=? AND col=?""",
                                        (int(item["source_sheet_id"]), int(row_value), int(col_value)),
                                    ).fetchone()
                                except (TypeError, ValueError):
                                    cell = None
                                if cell is None:
                                    scope_reasons.add("version_item_source_evidence_cell_missing")
                                else:
                                    actual_value = (
                                        cell["cached_value"]
                                        if cell["is_formula"] and cell["cached_value"] is not None
                                        else cell["raw_value"]
                                    )
                                    if not _field_equal(raw_value, actual_value, field_name):
                                        scope_reasons.add("version_item_source_evidence_raw_mismatch")
                        for field_name in ("code", "name", "feature", "unit", "quantity", "unit_price", "amount"):
                            expected_value = item.get(field_name)
                            if expected_value in (None, ""):
                                continue
                            candidates = field_entries.get(field_name, [])
                            if not candidates or not any(
                                _field_equal(
                                    expected_value,
                                    candidate.get("value", candidate.get("raw_value")),
                                    field_name,
                                )
                                for candidate in candidates
                            ):
                                scope_reasons.add("version_item_source_evidence_value_mismatch")
                                break
        # 只要版本项显式带有文件/Sheet 来源，哪怕同时保留了 line_item_id，
        # 也要回读该清单行的实际来源，不能用同项目另一期的原始单元格替换
        # 它。这个检查同时覆盖旧库中 v41 迁移前已经写入的快照。
        if has_historical_line_item and (
            item.get("source_file_id") not in (None, "")
            or item.get("source_sheet_id") not in (None, "")
        ):
            try:
                line_source = conn.execute(
                    """SELECT li.sheet_id AS line_sheet_id,
                              pb.file_id AS line_file_id,
                              li.flags_json
                         FROM line_items li
                         LEFT JOIN raw_sheets rs ON rs.id=li.sheet_id
                         LEFT JOIN parse_batches pb ON pb.id=rs.batch_id
                        WHERE li.id=?""",
                    (int(item["line_item_id"]),),
                ).fetchone()
            except (TypeError, ValueError):
                line_source = None
            if line_source is None:
                scope_reasons.add("version_item_line_item_source_missing")
            else:
                if str(line_source["line_sheet_id"]) != str(item.get("source_sheet_id")):
                    scope_reasons.add("version_item_line_item_source_mismatch")
                if str(line_source["line_file_id"]) != str(item.get("source_file_id")):
                    scope_reasons.add("version_item_line_item_source_mismatch")
                line_flags = _loads(line_source["flags_json"], {})
                if not isinstance(line_flags, dict):
                    line_flags = {}
                line_row = line_flags.get("row") or line_flags.get("source_row")
                if item.get("source_row") not in (None, "") and str(line_row) != str(item.get("source_row")):
                    scope_reasons.add("version_item_line_item_source_row_mismatch")
        # 显式来源快照没有 line_item_id 时，文件/Sheet/行列定位还必须与
        # 快照期次、方向和项目建立数据库关系。只检查 Evidence 的文字定位
        # 会允许把另一期的原始金额挂到当前期，之后再由版本差异和历史资产
        # 传播为跨期结论。
        if not has_historical_line_item and item.get("source_sheet_id") not in (None, ""):
            try:
                source_scope = conn.execute(
                    """SELECT rs.period_id AS sheet_period_id,
                              sp.project_id AS sheet_project_id,
                              sp.period_no AS sheet_period_no,
                              sp.direction AS sheet_direction,
                              pb.file_id AS sheet_file_id
                         FROM raw_sheets rs
                         JOIN parse_batches pb ON pb.id=rs.batch_id
                         LEFT JOIN settlement_periods sp ON sp.id=rs.period_id
                        WHERE rs.id=?""",
                    (int(item["source_sheet_id"]),),
                ).fetchone()
            except (TypeError, ValueError):
                source_scope = None
            if source_scope is None:
                scope_reasons.add("version_item_source_sheet_missing")
            else:
                if (
                    source_scope["sheet_project_id"] is None
                    or int(source_scope["sheet_project_id"]) != int(version.project_id)
                    or source_scope["sheet_period_id"] is None
                    or str(source_scope["sheet_period_id"]) != str(item.get("period_id"))
                    or str(source_scope["sheet_period_no"]) != str(item.get("period_no"))
                    or str(source_scope["sheet_direction"] or "unknown")
                    != str(item.get("direction") or "unknown")
                ):
                    scope_reasons.add("version_item_source_sheet_period_mismatch")
                if (
                    source_scope["sheet_file_id"] is None
                    or str(source_scope["sheet_file_id"]) != str(item.get("source_file_id"))
                ):
                    scope_reasons.add("version_item_source_sheet_file_mismatch")
        period = conn.execute(
            """SELECT project_id, period_no, direction
                 FROM settlement_periods WHERE id=?""",
            (item.get("period_id"),),
        ).fetchone() if item.get("period_id") is not None else None
        if period is None or int(period["project_id"]) != int(version.project_id):
            scope_reasons.add("version_item_period_mismatch")
        elif (
            item.get("period_no") is None
            or int(period["period_no"]) != int(item["period_no"])
            or str(period["direction"] or "unknown") != str(item.get("direction") or "unknown")
        ):
            scope_reasons.add("version_item_period_mismatch")
    reasons.extend(sorted(scope_reasons))
    snapshot_payload = [
        {
            key: item[key]
            for key in (
                "identity_key", "occurrence", "period_id", "period_no", "direction",
                "code", "name", "feature", "unit", "quantity", "unit_price", "amount",
                "flags", "source_file_id", "source_sheet_id", "source_row", "source_evidence_id",
            )
        }
        for item in items
    ]
    if stable_fingerprint(snapshot_payload) != version.snapshot_sha256:
        reasons.append("version_snapshot_hash_mismatch")
    return not reasons, tuple(dict.fromkeys(reasons))


def compare_project_versions(
    conn: sqlite3.Connection,
    project_id: int,
    baseline_version_id: int,
    current_version_id: int,
) -> VersionComparison:
    """比较两个版本；不覆盖版本、不写业务清单，也不自动确认差异。"""
    baseline = get_project_version(conn, project_id, baseline_version_id)
    current = get_project_version(conn, project_id, current_version_id)
    if baseline is None or current is None:
        raise ProjectVersionError("比较版本不存在或不属于当前项目")
    if baseline.version_id == current.version_id:
        raise ProjectVersionError("基准版本与当前版本不能相同")
    baseline_integrity, baseline_reasons = _version_integrity(conn, baseline)
    current_integrity, current_reasons = _version_integrity(conn, current)
    integrity_reasons = tuple(dict.fromkeys((*baseline_reasons, *current_reasons)))
    pairs = _pair_rows(
        _version_items(conn, project_id, baseline.version_id),
        _version_items(conn, project_id, current.version_id),
    )
    items = tuple(_diff_item(left, right, ambiguous) for left, right, ambiguous in pairs)
    if not (baseline_integrity and current_integrity):
        # 保留字段级差异供排查，但撤销所有“确认后净影响”，避免未绑定
        # 版本被外部 SQL 伪造成可用于正式金额结论的比较结果。
        items = tuple(
            replace(
                item,
                status=("incomparable" if item.status == "incomparable" else "pending"),
                confirmed_amount_impact=None,
                reason=f"{item.reason}；版本证据链未闭合，暂不形成确认净影响",
            )
            for item in items
        )
    summary: dict[str, dict[str, Any]] = {}
    for item in items:
        bucket = summary.setdefault(item.category, {"count": 0, "amount_impact": D(0), "amount_available": False})
        bucket["count"] += 1
        if item.amount_impact is not None and item.status == "confirmed":
            bucket["amount_impact"] += item.amount_impact
            bucket["amount_available"] = True
    confirmed_values = [item.confirmed_amount_impact for item in items if item.confirmed_amount_impact is not None]
    confirmed_net = (
        sum(confirmed_values, D(0))
        if confirmed_values and baseline_integrity and current_integrity else None
    )
    evidence_ids = tuple(sorted({
        *([baseline.evidence_id] if baseline.evidence_id is not None else []),
        *([current.evidence_id] if current.evidence_id is not None else []),
        *(evidence_id for item in items for evidence_id in item.evidence_ids),
    }))
    if not (baseline_integrity and current_integrity):
        for bucket in summary.values():
            bucket["amount_impact"] = None
            bucket["amount_available"] = False
    status = "available" if (
        items
        and baseline_integrity
        and current_integrity
        and not any(item.status in {"pending", "incomparable"} for item in items)
    ) else ("conditional" if items else "not_comparable")
    return VersionComparison(
        project_id=int(project_id),
        baseline=baseline,
        current=current,
        items=items,
        category_summary=summary,
        confirmed_net_amount_impact=confirmed_net,
        status=status,
        evidence_ids=evidence_ids,
        reason_codes=integrity_reasons,
    )
