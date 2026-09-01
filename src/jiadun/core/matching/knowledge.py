"""本地别名与匹配知识库。

知识库只保存人工确认后的候选映射，不修改原始清单，也不把一条项目内
确认自动推广到所有项目。每次新建或撤销都追加一个不可变版本，并把适用
项目、专业、方向、操作人、原因和 Evidence/Audit 关系保留下来。当前
``global`` 作用域的存储边界是“同一 project.db 内可见的所有项目行”；
项目工作空间彼此独立时不会自动共享这份知识，也不会自动广播使其他库
的运行契约失效。跨工作空间的企业级知识中心仍是后续任务，UI 和报告
不得把当前实现宣传成跨项目全局。匹配引擎只读取当前作用域中最新的
``active`` 版本；被撤销的最新版本会阻断旧表 ``item_aliases`` 的兼容回退，
避免撤销后旧别名继续生效。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jiadun.core.contracts import run_contract
from jiadun.core.evidence import audit as audit_log
from jiadun.core.evidence import evidence as evidence_api

ACTIVE = "active"
REVOKED = "revoked"
PROJECT_SCOPE = "project"
GLOBAL_SCOPE = "global"
VALID_SCOPES = {PROJECT_SCOPE, GLOBAL_SCOPE}
VALID_DIRECTIONS = {"upward", "downward", "unknown"}


class AliasKnowledgeError(ValueError):
    """别名知识库输入或作用域冲突。"""


@dataclass(frozen=True)
class AliasKnowledgeEntry:
    alias_id: int
    owner_project_id: int
    scope: str
    applicable_project_id: int | None
    profession: str
    direction: str
    original_name: str
    normalized_name: str
    canonical_key: str
    canonical_name: str
    mapping_basis: str
    version: int
    status: str
    created_by: str
    confirmed_at: str
    revoked_by: str | None
    revoked_at: str | None
    revoke_reason: str | None
    supersedes_id: int | None
    evidence_id: int | None
    audit_id: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any]) -> AliasKnowledgeEntry:
        get = row.__getitem__
        return cls(
            alias_id=int(get("id")),
            owner_project_id=int(get("project_id")),
            scope=str(get("scope")),
            applicable_project_id=(
                int(get("applicable_project_id"))
                if get("applicable_project_id") is not None else None
            ),
            profession=str(get("profession") or ""),
            direction=str(get("direction") or "unknown"),
            original_name=str(get("original_name")),
            normalized_name=str(get("normalized_name")),
            canonical_key=str(get("canonical_key")),
            canonical_name=str(get("canonical_name") or ""),
            mapping_basis=str(get("mapping_basis")),
            version=int(get("version")),
            status=str(get("status")),
            created_by=str(get("created_by")),
            confirmed_at=str(get("confirmed_at")),
            revoked_by=get("revoked_by"),
            revoked_at=get("revoked_at"),
            revoke_reason=get("revoke_reason"),
            supersedes_id=(int(get("supersedes_id")) if get("supersedes_id") is not None else None),
            evidence_id=(int(get("evidence_id")) if get("evidence_id") is not None else None),
            audit_id=(int(get("audit_id")) if get("audit_id") is not None else None),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.alias_id,
            "owner_project_id": self.owner_project_id,
            "scope": self.scope,
            "applicable_project_id": self.applicable_project_id,
            "profession": self.profession,
            "direction": self.direction,
            "original_name": self.original_name,
            "normalized_name": self.normalized_name,
            "canonical_key": self.canonical_key,
            "canonical_name": self.canonical_name,
            "mapping_basis": self.mapping_basis,
            "version": self.version,
            "status": self.status,
            "created_by": self.created_by,
            "confirmed_at": self.confirmed_at,
            "revoked_by": self.revoked_by,
            "revoked_at": self.revoked_at,
            "revoke_reason": self.revoke_reason,
            "supersedes_id": self.supersedes_id,
            "evidence_id": self.evidence_id,
            "audit_id": self.audit_id,
        }


def normalize_alias_name(value: str | None) -> str:
    """沿用匹配引擎的确定性名称归一化，避免知识库另造口径。"""
    from jiadun.core.matching.matching import normalize_name

    return normalize_name(value)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _validate_common(
    *,
    original_name: str,
    canonical_key: str,
    canonical_name: str,
    mapping_basis: str,
    actor: str,
    reason: str,
    scope: str,
    direction: str,
) -> tuple[str, str, str, str, str, str, str]:
    values = [
        str(original_name or "").strip(),
        str(canonical_key or "").strip(),
        str(canonical_name or "").strip(),
        str(mapping_basis or "").strip(),
        str(actor or "").strip(),
        str(reason or "").strip(),
        str(scope or "").strip().lower(),
        str(direction or "unknown").strip().lower(),
    ]
    if not values[0] or not values[1] or not values[2]:
        raise AliasKnowledgeError("别名知识必须填写原始名称、规范键和规范名称")
    if not values[3]:
        raise AliasKnowledgeError("别名映射依据不能为空")
    if not values[4]:
        raise AliasKnowledgeError("别名知识必须记录操作人")
    if not values[5]:
        raise audit_log.AuditReasonRequiredError("别名知识变更必须记录原因")
    if values[6] not in VALID_SCOPES:
        raise AliasKnowledgeError(f"不支持的别名作用域: {scope!r}")
    if values[7] not in VALID_DIRECTIONS:
        raise AliasKnowledgeError(f"不支持的结算方向: {direction!r}")
    return tuple(values)  # type: ignore[return-value]


def _latest_rows(
    conn: sqlite3.Connection,
    *,
    project_id: int | None = None,
    direction: str | None = None,
    profession: str | None = None,
    include_all_professions: bool = False,
) -> list[sqlite3.Row]:
    """读取作用域内每个别名身份的最新版本（active 或 revoked）。"""
    clauses = [
        "((scope='global' AND applicable_project_id IS NULL) "
        "OR (scope='project' AND applicable_project_id=? "
        "AND project_id=applicable_project_id))",
    ]
    params: list[Any] = [int(project_id)] if project_id is not None else [None]
    if project_id is None:
        # 只有项目查询才能判断项目作用域；知识库快照由调用方传入项目。
        raise ValueError("读取别名知识必须指定项目")
    if direction is not None:
        clauses.append("direction=?")
        params.append(str(direction))
    if profession is None and not include_all_professions:
        clauses.append("profession='' ")
    elif profession is not None:
        normalized_profession = str(profession).strip()
        clauses.append("(profession='' OR profession=?)")
        params.append(normalized_profession)
    rows = conn.execute(
        "SELECT * FROM alias_knowledge WHERE " + " AND ".join(clauses) +
        " ORDER BY scope DESC, version DESC, id DESC",
        params,
    ).fetchall()
    latest: dict[tuple[str, int | None, str, str, str], sqlite3.Row] = {}
    for row in rows:
        key = (
            str(row["scope"]),
            int(row["applicable_project_id"]) if row["applicable_project_id"] is not None else None,
            str(row["direction"]),
            str(row["profession"] or ""),
            str(row["normalized_name"]),
        )
        if key not in latest:
            latest[key] = row
    return list(latest.values())


def current_entries(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    direction: str | None = None,
    profession: str | None = None,
    include_all_professions: bool = False,
) -> list[AliasKnowledgeEntry]:
    """返回当前项目/专业/方向作用域内的最新版本，含 revoked 记录。"""
    return [
        AliasKnowledgeEntry.from_row(row)
        for row in _latest_rows(
            conn, project_id=project_id, direction=direction, profession=profession,
            include_all_professions=include_all_professions,
        )
    ]


def current_alias_snapshot(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    """供 Run Contract 使用的 active 别名快照；不把撤销版本当作当前映射。"""
    return [
        entry.as_dict()
        for entry in current_entries(conn, project_id, include_all_professions=True)
        if entry.status == ACTIVE
    ]


def lookup_aliases(
    conn: sqlite3.Connection,
    project_id: int,
    direction: str,
    *,
    profession: str | None = None,
) -> tuple[dict[str, str], set[str]]:
    """返回 ``normalized_name -> canonical_key`` 与被撤销的阻断集合。"""
    entries = current_entries(conn, project_id, direction=direction, profession=profession)
    # 旧版 ``item_aliases`` 没有专业字段，不能在调用方未提供专业或提供
    # 不匹配专业时绕过知识库的适用范围。先读取同一方向下所有最新版本，
    # 为受限专业名称建立兼容回退阻断集合；若存在一般（空专业）有效映射，
    # 则仍允许使用该明确的通用知识。
    all_entries = current_entries(
        conn,
        project_id,
        direction=direction,
        include_all_professions=True,
    )
    general_active = {
        entry.normalized_name
        for entry in all_entries
        if not entry.profession and entry.status == ACTIVE
    }
    applicable_restricted = {
        entry.normalized_name
        for entry in entries
        if entry.profession and entry.profession == (profession or "")
    }
    restricted_blocked = {
        entry.normalized_name
        for entry in all_entries
        if entry.profession
        and entry.normalized_name not in general_active
        and entry.normalized_name not in applicable_restricted
    }
    # 项目级优先于全局；同一层级按版本/ID 最新优先。若多个全局拥有者
    # 对同一名称给出冲突规范键，宁可不自动匹配，也不猜测业务等价关系。
    selected: dict[str, AliasKnowledgeEntry] = {}
    conflicts: set[str] = set()
    for entry in sorted(
        entries,
        key=lambda item: (
            0 if item.scope == PROJECT_SCOPE else 1,
            -item.version,
            -item.alias_id,
        ),
    ):
        prior = selected.get(entry.normalized_name)
        if prior is not None and (
            prior.scope == entry.scope and prior.canonical_key != entry.canonical_key
        ):
            conflicts.add(entry.normalized_name)
            continue
        if prior is None:
            selected[entry.normalized_name] = entry
    active = {
        name: entry.canonical_key
        for name, entry in selected.items()
        if entry.status == ACTIVE and name not in conflicts
    }
    blocked = {
        name for name, entry in selected.items()
        if entry.status == REVOKED or name in conflicts
    }
    blocked.update(restricted_blocked)
    return active, blocked


def _identity_rows(
    conn: sqlite3.Connection,
    *,
    scope: str,
    applicable_project_id: int | None,
    direction: str,
    profession: str,
    normalized_name: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM alias_knowledge
           WHERE scope=? AND applicable_project_id IS ? AND direction=?
             AND profession=? AND normalized_name=?
           ORDER BY version DESC, id DESC""",
        (scope, applicable_project_id, direction, profession, normalized_name),
    ).fetchall()


def _write_event(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    alias_id: int,
    event_type: str,
    before_status: str | None,
    after_status: str,
    reason: str,
    actor: str,
    active_contract: run_contract.RunContract,
    evidence_id: int,
    audit_id: int,
    metadata: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO alias_knowledge_events(
               project_id, alias_id, event_type, before_status, after_status,
               reason, actor, occurred_at, run_id, run_signature,
               evidence_id, audit_id, metadata_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            int(project_id), int(alias_id), event_type, before_status, after_status,
            str(reason).strip(), str(actor).strip(), _now(), active_contract.run_id,
            active_contract.signature, int(evidence_id), int(audit_id),
            json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True, default=str),
        ),
    )
    return int(cur.lastrowid)


def add_alias(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    original_name: str,
    canonical_key: str,
    canonical_name: str,
    mapping_basis: str,
    actor: str,
    reason: str,
    scope: str = PROJECT_SCOPE,
    applicable_project_id: int | None = None,
    profession: str = "",
    direction: str = "unknown",
) -> AliasKnowledgeEntry:
    """追加一个 active 别名版本；冲突映射必须先撤销旧版本。"""
    (
        original_name, canonical_key, canonical_name, mapping_basis, actor, reason,
        scope, direction,
    ) = _validate_common(
        original_name=original_name,
        canonical_key=canonical_key,
        canonical_name=canonical_name,
        mapping_basis=mapping_basis,
        actor=actor,
        reason=reason,
        scope=scope,
        direction=direction,
    )
    profession = str(profession or "").strip()
    if scope == GLOBAL_SCOPE:
        applicable_project_id = None
    else:
        requested_project_id = int(applicable_project_id or project_id)
        if requested_project_id != int(project_id):
            raise AliasKnowledgeError(
                "项目级别别名只能写入调用方项目；跨项目知识必须使用显式 global 作用域"
            )
        applicable_project_id = requested_project_id
    normalized_name = normalize_alias_name(original_name)
    if not normalized_name:
        raise AliasKnowledgeError("归一化后的别名不能为空")
    rows = _identity_rows(
        conn,
        scope=scope,
        applicable_project_id=applicable_project_id,
        direction=direction,
        profession=profession,
        normalized_name=normalized_name,
    )
    latest = rows[0] if rows else None
    if latest and latest["status"] == ACTIVE:
        if latest["canonical_key"] != canonical_key:
            raise AliasKnowledgeError("同一作用域的别名已有不同规范键，请先撤销旧版本")
        return AliasKnowledgeEntry.from_row(latest)
    version = int(latest["version"] if latest else 0) + 1
    old_contract = run_contract.get_current_contract(conn, project_id)
    now = _now()
    with run_contract._transaction(conn, "add_alias_knowledge"):
        audit_id = audit_log.record_audit(
            conn,
            project_id,
            actor,
            "add_alias_knowledge",
            f"alias:{scope}:{applicable_project_id}:{direction}:{normalized_name}",
            {"status": latest["status"] if latest else None,
             "canonical_key": latest["canonical_key"] if latest else None},
            {"status": ACTIVE, "canonical_key": canonical_key,
             "canonical_name": canonical_name, "profession": profession},
            reason,
            commit=False,
            run_id=old_contract.run_id if old_contract else None,
            run_signature=old_contract.signature if old_contract else None,
        )
        cur = conn.execute(
            """INSERT INTO alias_knowledge(
                   project_id, scope, applicable_project_id, profession, direction,
                   original_name, normalized_name, canonical_key, canonical_name,
                   mapping_basis, version, status, created_by, confirmed_at,
                   supersedes_id, audit_id, metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(project_id), scope, applicable_project_id, profession, direction,
                original_name, normalized_name, canonical_key, canonical_name,
                mapping_basis, version, ACTIVE, actor, now,
                int(latest["id"]) if latest else None, audit_id,
                json.dumps({"reason": reason.strip()}, ensure_ascii=False),
            ),
        )
        alias_id = int(cur.lastrowid)
        active_contract = run_contract.ensure_run_contract(conn, project_id)
        if active_contract is not None:
            # Audit 内容参与合同快照，但运行绑定字段不参与指纹；刷新后
            # 将同一条责任记录绑定到新别名真正生效的当前运行。
            conn.execute(
                "UPDATE audit_log SET run_id=?, run_signature=? WHERE id=?",
                (active_contract.run_id, active_contract.signature, audit_id),
            )
        evidence_id = evidence_api.add_evidence(
            conn,
            project_id,
            "alias_knowledge",
            f"别名知识已{'全局' if scope == GLOBAL_SCOPE else '项目级'}生效：{original_name} → {canonical_name}",
            steps=[{
                "step": "人工确认别名知识",
                "alias_id": alias_id,
                "version": version,
                "scope": scope,
                "applicable_project_id": applicable_project_id,
                "profession": profession,
                "direction": direction,
                "original_name": original_name,
                "normalized_name": normalized_name,
                "canonical_key": canonical_key,
                "canonical_name": canonical_name,
                "mapping_basis": mapping_basis,
                "actor": actor,
                "reason": reason.strip(),
            }],
            sources=[{"project_id": project_id, "audit_id": audit_id}],
            commit=False,
            run_signature=active_contract.signature,
            run_id=active_contract.run_id,
            scope="human",
        )
        _write_event(
            conn,
            project_id=project_id,
            alias_id=alias_id,
            event_type="created",
            before_status=latest["status"] if latest else None,
            after_status=ACTIVE,
            reason=reason,
            actor=actor,
            active_contract=active_contract,
            evidence_id=evidence_id,
            audit_id=audit_id,
            metadata={"version": version},
        )
        # alias_knowledge 采用不可变行；Evidence 通过同一事务写入的
        # alias_knowledge_events.evidence_id 关联，不能事后 UPDATE 旧快照。
    row = conn.execute("SELECT * FROM alias_knowledge WHERE id=?", (alias_id,)).fetchone()
    return AliasKnowledgeEntry.from_row(row)


def revoke_alias(
    conn: sqlite3.Connection,
    project_id: int,
    alias_id: int,
    *,
    actor: str,
    reason: str,
) -> AliasKnowledgeEntry:
    """追加 revoked 版本，不覆盖原 active 版本。"""
    if not str(actor).strip():
        raise AliasKnowledgeError("撤销别名必须记录操作人")
    if not str(reason).strip():
        raise audit_log.AuditReasonRequiredError("撤销别名必须记录原因")
    row = conn.execute(
        "SELECT * FROM alias_knowledge WHERE id=? AND project_id=?",
        (int(alias_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise AliasKnowledgeError(f"别名知识 {alias_id} 不属于当前项目")
    latest_rows = _identity_rows(
        conn,
        scope=row["scope"],
        applicable_project_id=row["applicable_project_id"],
        direction=row["direction"],
        profession=row["profession"],
        normalized_name=row["normalized_name"],
    )
    latest = latest_rows[0] if latest_rows else row
    if latest["status"] == REVOKED:
        raise AliasKnowledgeError("该别名已经撤销，不能重复撤销")
    old_contract = run_contract.get_current_contract(conn, project_id)
    now = _now()
    version = int(latest["version"]) + 1
    with run_contract._transaction(conn, "revoke_alias_knowledge"):
        audit_id = audit_log.record_audit(
            conn,
            project_id,
            str(actor).strip(),
            "revoke_alias_knowledge",
            f"alias:{int(alias_id)}",
            {"status": ACTIVE, "version": int(latest["version"])},
            {"status": REVOKED, "version": version, "reason": str(reason).strip()},
            reason,
            commit=False,
            run_id=old_contract.run_id if old_contract else None,
            run_signature=old_contract.signature if old_contract else None,
        )
        cur = conn.execute(
            """INSERT INTO alias_knowledge(
                   project_id, scope, applicable_project_id, profession, direction,
                   original_name, normalized_name, canonical_key, canonical_name,
                   mapping_basis, version, status, created_by, confirmed_at,
                   revoked_by, revoked_at, revoke_reason, supersedes_id, audit_id,
                   metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(project_id), latest["scope"], latest["applicable_project_id"],
                latest["profession"], latest["direction"], latest["original_name"],
                latest["normalized_name"], latest["canonical_key"], latest["canonical_name"],
                latest["mapping_basis"], version, REVOKED, str(actor).strip(), now,
                str(actor).strip(), now, str(reason).strip(), int(latest["id"]), audit_id,
                json.dumps({"reason": str(reason).strip()}, ensure_ascii=False),
            ),
        )
        new_id = int(cur.lastrowid)
        active_contract = run_contract.ensure_run_contract(conn, project_id)
        if active_contract is not None:
            conn.execute(
                "UPDATE audit_log SET run_id=?, run_signature=? WHERE id=?",
                (active_contract.run_id, active_contract.signature, audit_id),
            )
        evidence_id = evidence_api.add_evidence(
            conn,
            project_id,
            "alias_knowledge_revoke",
            f"别名知识已撤销：{latest['original_name']} → {latest['canonical_name']}",
            steps=[{
                "step": "人工撤销别名知识",
                "alias_id": new_id,
                "supersedes_id": int(latest["id"]),
                "version": version,
                "reason": str(reason).strip(),
                "actor": str(actor).strip(),
            }],
            sources=[{"project_id": project_id, "audit_id": audit_id}],
            commit=False,
            run_signature=active_contract.signature,
            run_id=active_contract.run_id,
            scope="human",
        )
        _write_event(
            conn,
            project_id=project_id,
            alias_id=new_id,
            event_type="revoked",
            before_status=ACTIVE,
            after_status=REVOKED,
            reason=reason,
            actor=str(actor).strip(),
            active_contract=active_contract,
            evidence_id=evidence_id,
            audit_id=audit_id,
            metadata={"version": version, "supersedes_id": int(latest["id"])},
        )
        # 撤销版本同样保持不可变；事件行保存本次 Evidence 关联。
    new_row = conn.execute("SELECT * FROM alias_knowledge WHERE id=?", (new_id,)).fetchone()
    return AliasKnowledgeEntry.from_row(new_row)


def history_for(conn: sqlite3.Connection, project_id: int, normalized_name: str) -> list[AliasKnowledgeEntry]:
    """读取某别名在当前项目作为所有者时的完整追加历史。"""
    rows = conn.execute(
        """SELECT * FROM alias_knowledge
           WHERE project_id=? AND normalized_name=? ORDER BY id""",
        (int(project_id), normalize_alias_name(normalized_name)),
    ).fetchall()
    return [AliasKnowledgeEntry.from_row(row) for row in rows]
