"""P1-05 规则目录：元数据完整、项目级启停和 Run Contract 绑定。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jiadun.core.anomalies import catalog, engine
from jiadun.core.contracts import run_contract
from jiadun.core.db import migrations


@pytest.fixture()
def db(tmp_path: Path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        project_id = conn.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at) VALUES (?,?,?,?)",
            ("规则目录测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
        period_id = conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction) VALUES (?,?,?,?)",
            (project_id, 1, "第1期", "downward"),
        ).lastrowid
        conn.execute(
            """INSERT INTO line_items(period_id, code, name, unit, quantity, unit_price, amount)
               VALUES (?,?,?,?,?,?,?)""",
            (period_id, "K1", "大额项", "项", "1", "100000", "100000"),
        )
    yield conn, int(project_id)
    conn.close()


def test_catalog_has_auditable_metadata_and_safe_rule_is_not_disableable():
    definitions = catalog.catalog_entries()
    assert len(definitions) >= 20
    for definition in definitions:
        payload = definition.as_dict()
        assert all(payload[key] for key in (
            "rule_id", "name_zh", "scenario", "severity", "trigger_condition",
            "evidence_requirements", "impact_algorithm", "limitations", "suggested_review",
            "version",
        ))
    assert catalog.RULE_CATALOG["rule_needs_review_headers"].allow_disable is False


def test_disable_rule_changes_run_contract_and_execution_scope(db):
    conn, project_id = db
    first = run_contract.ensure_run_contract(conn, project_id)
    change = catalog.set_rule_enabled(
        conn, project_id, "rule_round_amounts", False,
        actor="管理员", reason="本项目大额整数由合同控制表另行复核",
    )
    current = run_contract.get_current_contract(conn, project_id)
    assert current is not None
    assert current.run_id == change["run_id"]
    assert current.run_id != first.run_id
    assert "rule_round_amounts" in current.components["rules"]["disabled_rule_ids"]
    assert "rule_round_amounts" not in current.components["selected_rule_ids"]
    assert len(catalog.enabled_rule_functions(conn, project_id)) == len(catalog.RULE_CATALOG) - 1
    findings = engine.run_anomalies(conn, project_id)
    assert not any(item.rule_id == "large_round_amount" for item in findings)
    row = conn.execute(
        "SELECT id, enabled, config_version, actor, audit_id, evidence_id FROM rule_configurations "
        "WHERE project_id=? AND rule_id=? ORDER BY config_version DESC LIMIT 1",
        (project_id, "rule_round_amounts"),
    ).fetchone()
    assert tuple(row[1:4]) == (0, 1, "管理员")
    assert row["audit_id"] is not None
    # 配置 Evidence 与新 Run Contract 关联，但配置快照本身不可改写。
    evidence = conn.execute(
        "SELECT kind, scope, run_id FROM evidence WHERE kind='rule_configuration' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert tuple(evidence) == ("rule_configuration", "human", current.run_id)
    with pytest.raises(sqlite3.IntegrityError, match="rule configuration immutable"):
        conn.execute("UPDATE rule_configurations SET enabled=1 WHERE id=?", (row["id"],))


def test_reenable_creates_next_configuration_version_and_safe_rule_cannot_disable(db):
    conn, project_id = db
    run_contract.ensure_run_contract(conn, project_id)
    catalog.set_rule_enabled(
        conn, project_id, "rule_round_amounts", False,
        actor="管理员", reason="暂时关闭以验证配置隔离",
    )
    enabled = catalog.set_rule_enabled(
        conn, project_id, "rule_round_amounts", True,
        actor="管理员", reason="补充控制证据后恢复规则",
    )
    assert enabled["config_version"] == 2
    assert "rule_round_amounts" in run_contract.get_current_contract(
        conn, project_id
    ).components["selected_rule_ids"]
    with pytest.raises(ValueError, match="不允许关闭"):
        catalog.set_rule_enabled(
            conn, project_id, "rule_needs_review_headers", False,
            actor="管理员", reason="不应允许关闭安全闸门",
        )
