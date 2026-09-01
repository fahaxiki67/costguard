"""P1-04 别名知识库：作用域、版本、撤销和匹配读取边界。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jiadun.core.contracts import run_contract
from jiadun.core.db import migrations
from jiadun.core.matching import knowledge, matching
from jiadun.core.models import project as project_model


def _project(tmp_path: Path, name: str):
    info = project_model.create_project(name, tmp_path / name)
    info, conn = project_model.open_project(Path(info.workspace_path))
    return info, conn


def test_project_and_global_scope_are_explicit_and_enter_run_contract(tmp_path):
    info, conn = _project(tmp_path, "别名作用域")
    try:
        first = run_contract.ensure_run_contract(conn, info.project_id)
        entry = knowledge.add_alias(
            conn,
            info.project_id,
            original_name="润管砂浆",
            canonical_key="code:MORTAR-01",
            canonical_name="泵送砂浆",
            mapping_basis="合同清单与项目确认单逐项核对",
            actor="复核人",
            reason="同标号、同单位且特征一致",
            profession="土建",
            direction="upward",
        )
        current = run_contract.get_current_contract(conn, info.project_id)
        assert current is not None and current.run_id != first.run_id
        assert current.components["alias_library_version"]
        assert any(item["original_name"] == "润管砂浆" for item in current.components["aliases"])
        assert entry.scope == "project"
        assert entry.applicable_project_id == info.project_id
        assert entry.evidence_id is None  # 行不可变；事件持有 Evidence 关联
        event = conn.execute(
            "SELECT after_status, run_id, run_signature, evidence_id, audit_id "
            "FROM alias_knowledge_events WHERE alias_id=?",
            (entry.alias_id,),
        ).fetchone()
        assert tuple(event[:3]) == ("active", current.run_id, current.signature)
        assert event["evidence_id"] is not None and event["audit_id"] is not None
        audit = conn.execute(
            "SELECT run_id, run_signature FROM audit_log WHERE id=?",
            (event["audit_id"],),
        ).fetchone()
        assert tuple(audit) == (current.run_id, current.signature)
        assert run_contract.ensure_run_contract(conn, info.project_id).run_id == current.run_id
        assert knowledge.current_alias_snapshot(conn, info.project_id)[0]["profession"] == "土建"

        # 未提供专业上下文时，不得自动套用仅适用于土建的别名；提供明确专业
        # 后才允许进入匹配读取面。
        aliases_without_profession, _ = knowledge.lookup_aliases(
            conn, info.project_id, "upward"
        )
        aliases_with_profession, _ = knowledge.lookup_aliases(
            conn, info.project_id, "upward", profession="土建"
        )
        normalized = knowledge.normalize_alias_name("润管砂浆")
        assert normalized not in aliases_without_profession
        assert aliases_with_profession[normalized] == "code:MORTAR-01"
        assert matching.load_aliases(conn, info.project_id, "upward") == {}
        assert matching.load_aliases(conn, info.project_id, "upward", "土建")[normalized] == "code:MORTAR-01"
    finally:
        conn.close()


def test_global_alias_applies_to_other_project_but_project_alias_does_not(tmp_path):
    db_path = tmp_path / "shared.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn1 = migrations.connect(db_path)
    with conn1:
        project1 = conn1.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at) "
            "VALUES ('项目一', ?, ?, '2026')",
            (migrations.LATEST_SCHEMA_VERSION, str(tmp_path / "项目一")),
        ).lastrowid
        project2 = conn1.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at) "
            "VALUES ('项目二', ?, ?, '2026')",
            (migrations.LATEST_SCHEMA_VERSION, str(tmp_path / "项目二")),
        ).lastrowid
    try:
        knowledge.add_alias(
            conn1, project1,
            original_name="项目专用砂浆", canonical_key="code:P1",
            canonical_name="项目一规范砂浆", mapping_basis="项目一签认单",
            actor="甲", reason="仅适用于项目一", direction="downward",
        )
        knowledge.add_alias(
            conn1, project1,
            original_name="润管砂浆", canonical_key="code:G1",
            canonical_name="全局规范砂浆", mapping_basis="企业统一别名表",
            actor="甲", reason="全局知识库确认", scope="global", direction="downward",
        )
        project_aliases, _ = knowledge.lookup_aliases(conn1, project1, "downward")
        other_aliases, _ = knowledge.lookup_aliases(conn1, project2, "downward")
        assert project_aliases[knowledge.normalize_alias_name("项目专用砂浆")] == "code:P1"
        assert knowledge.normalize_alias_name("项目专用砂浆") not in other_aliases
        assert other_aliases[knowledge.normalize_alias_name("润管砂浆")] == "code:G1"
    finally:
        conn1.close()


def test_revoke_is_append_only_blocks_legacy_alias_and_can_be_reintroduced(tmp_path):
    info, conn = _project(tmp_path, "别名撤销")
    try:
        entry = knowledge.add_alias(
            conn, info.project_id,
            original_name="泵送砂浆", canonical_key="code:M1",
            canonical_name="规范泵送砂浆", mapping_basis="人工复核",
            actor="复核人", reason="首版确认", direction="downward",
        )
        # 旧库兼容别名与新知识库相同；撤销后旧表不能绕过撤销状态继续生效。
        conn.execute(
            """INSERT INTO item_aliases(project_id, direction, canonical_key,
                       alias_text, mapping_basis, confirmed_by, confirmed_at)
                   VALUES (?, 'downward', 'code:M1', '泵送砂浆', '旧兼容记录', '旧用户', '2025')""",
            (info.project_id,),
        )
        normalized = knowledge.normalize_alias_name("泵送砂浆")
        assert matching.load_aliases(conn, info.project_id, "downward")[normalized] == "code:M1"
        revoked = knowledge.revoke_alias(
            conn, info.project_id, entry.alias_id, actor="复核人", reason="发现适用特征不一致"
        )
        assert revoked.version == entry.version + 1
        assert revoked.status == knowledge.REVOKED
        revoked_event = conn.execute(
            "SELECT run_id, run_signature, audit_id FROM alias_knowledge_events WHERE alias_id=?",
            (revoked.alias_id,),
        ).fetchone()
        revoked_audit = conn.execute(
            "SELECT run_id, run_signature FROM audit_log WHERE id=?",
            (revoked_event["audit_id"],),
        ).fetchone()
        assert tuple(revoked_audit) == (revoked_event["run_id"], revoked_event["run_signature"])
        assert run_contract.ensure_run_contract(conn, info.project_id).run_id == revoked_event["run_id"]
        active, blocked = knowledge.lookup_aliases(conn, info.project_id, "downward")
        assert normalized not in active and normalized in blocked
        assert normalized not in matching.load_aliases(conn, info.project_id, "downward")
        history = knowledge.history_for(conn, info.project_id, "泵送砂浆")
        assert [item.status for item in history] == [knowledge.ACTIVE, knowledge.REVOKED]
        with pytest.raises(sqlite3.IntegrityError, match="alias knowledge immutable"):
            conn.execute("UPDATE alias_knowledge SET status='active' WHERE id=?", (revoked.alias_id,))
        with pytest.raises(sqlite3.IntegrityError, match="alias knowledge immutable"):
            conn.execute("DELETE FROM alias_knowledge WHERE id=?", (entry.alias_id,))

        reintroduced = knowledge.add_alias(
            conn, info.project_id,
            original_name="泵送砂浆", canonical_key="code:M1",
            canonical_name="规范泵送砂浆", mapping_basis="重新取得项目签认",
            actor="复核人", reason="补充特征后重新确认", direction="downward",
        )
        assert reintroduced.version == revoked.version + 1
        assert matching.load_aliases(conn, info.project_id, "downward")[normalized] == "code:M1"
    finally:
        conn.close()


def test_profession_scoped_knowledge_blocks_legacy_fallback_without_matching_context(tmp_path):
    """专业受限知识不能被无专业的旧兼容别名绕过。"""
    info, conn = _project(tmp_path, "别名专业边界")
    try:
        with conn:
            conn.execute(
                """INSERT INTO item_aliases(
                       project_id, direction, canonical_key, alias_text,
                       mapping_basis, confirmed_by, confirmed_at)
                   VALUES (?, 'upward', 'code:OLD', '润管砂浆', '旧兼容记录', '旧用户', '2025')""",
                (info.project_id,),
            )
        knowledge.add_alias(
            conn,
            info.project_id,
            original_name="润管砂浆",
            canonical_key="code:MORTAR",
            canonical_name="规范砂浆",
            mapping_basis="土建签认单",
            actor="复核人",
            reason="仅适用于土建专业",
            profession="土建",
            direction="upward",
        )
        normalized = knowledge.normalize_alias_name("润管砂浆")
        assert matching.load_aliases(conn, info.project_id, "upward") == {}
        assert matching.load_aliases(conn, info.project_id, "upward", "安装") == {}
        assert matching.load_aliases(conn, info.project_id, "upward", "土建")[normalized] == "code:MORTAR"
    finally:
        conn.close()


def test_project_scope_cannot_write_another_project(tmp_path):
    """项目级知识的 Evidence/Audit 所属项目必须与适用项目一致。"""
    db_path = tmp_path / "shared-project-scope.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        project1 = conn.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at) "
            "VALUES ('项目一', ?, ?, '2026')",
            (migrations.LATEST_SCHEMA_VERSION, str(tmp_path / "项目一")),
        ).lastrowid
        project2 = conn.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at) "
            "VALUES ('项目二', ?, ?, '2026')",
            (migrations.LATEST_SCHEMA_VERSION, str(tmp_path / "项目二")),
        ).lastrowid
    try:
        with pytest.raises(knowledge.AliasKnowledgeError, match="只能写入调用方项目"):
            knowledge.add_alias(
                conn,
                project1,
                original_name="跨项目砂浆",
                canonical_key="code:CROSS",
                canonical_name="跨项目规范砂浆",
                mapping_basis="待授权的跨项目代录",
                actor="代录人",
                reason="验证项目作用域边界",
                applicable_project_id=project2,
                direction="downward",
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM alias_knowledge WHERE project_id=?", (project1,)
        ).fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="project alias owner mismatch"):
            conn.execute(
                """INSERT INTO alias_knowledge(
                       project_id, scope, applicable_project_id, profession, direction,
                       original_name, normalized_name, canonical_key, canonical_name,
                       mapping_basis, version, status, created_by, confirmed_at)
                   VALUES (?, 'project', ?, '', 'downward', '直写越权', '直写越权',
                           'code:FORGED', '伪造规范名', '旧脚本直写', 1, 'active', '脚本', '2026')""",
                (project1, project2),
            )
    finally:
        conn.close()


def test_conflicting_active_alias_requires_revoke_first(tmp_path):
    info, conn = _project(tmp_path, "别名冲突")
    try:
        knowledge.add_alias(
            conn, info.project_id,
            original_name="同名砂浆", canonical_key="code:A",
            canonical_name="规范 A", mapping_basis="第一次核验",
            actor="用户", reason="建立基线", direction="upward",
        )
        with pytest.raises(knowledge.AliasKnowledgeError, match="先撤销"):
            knowledge.add_alias(
                conn, info.project_id,
                original_name="同名砂浆", canonical_key="code:B",
                canonical_name="规范 B", mapping_basis="冲突核验",
                actor="用户", reason="发现另一种解释", direction="upward",
            )
    finally:
        conn.close()
