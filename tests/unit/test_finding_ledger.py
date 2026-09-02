"""经营合规问题台账单测（P6，v46 schema）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jiadun.core import analysis, ledger
from jiadun.core import demo as demo_core
from jiadun.core.anomalies import engine as anomaly_engine
from jiadun.core.models import project as pm


@pytest.fixture()
def project_with_anomaly(tmp_path: Path):
    info = demo_core.provision_demo_project(tmp_path / "ws")
    info, conn = pm.open_project(Path(info.workspace_path))
    anomaly_engine.run_anomalies(conn, info.project_id)
    row = conn.execute(
        "SELECT id, project_id FROM anomalies ORDER BY id LIMIT 1"
    ).fetchone()
    yield conn, int(row["project_id"]), int(row["id"])
    conn.close()


def test_v46_migration_creates_ledger_table(tmp_path: Path):
    info = demo_core.provision_demo_project(tmp_path / "ws2")
    conn = sqlite3.connect(Path(info.workspace_path) / "project.db")
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    triggers = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}
    conn.close()
    assert "finding_ledger" in tables
    assert "finding_ledger_status_guard" in triggers


def test_ledger_upsert_and_status_flow(project_with_anomaly):
    conn, pid, aid = project_with_anomaly
    assert ledger.get_finding_ledger(conn, pid, aid) is None
    entry = ledger.update_finding_ledger(
        conn, pid, aid, actor="审计员甲", reason="人工核定金额影响",
        amount_impact="1234.56", responsible_unit="项目二部",
        responsible_matter="对下结算超计量", handling_opinion="补签证后确认",
        ledger_status="confirmed")
    assert entry.amount_impact == "1234.56"
    assert entry.ledger_status == "confirmed"
    assert entry.updated_by == "审计员甲"
    # 二次更新：未传字段保持原值，状态推进为 resolved
    entry2 = ledger.update_finding_ledger(
        conn, pid, aid, actor="审计员乙", reason="复核后解决",
        ledger_status="resolved")
    assert entry2.amount_impact == "1234.56"
    assert entry2.ledger_status == "resolved"
    assert entry2.updated_by == "审计员乙"
    rows = ledger.list_finding_ledger(conn, pid)
    assert len(rows) == 1 and rows[0].fingerprint
    # 分析重建异常（id 变化）后，台账仍挂同一 fingerprint
    analysis.run_analysis(conn, pid)
    new_row = conn.execute(
        "SELECT id FROM anomalies WHERE fingerprint=?",
        (rows[0].fingerprint,)).fetchone()
    assert new_row is not None
    again = ledger.get_finding_ledger(conn, pid, int(new_row["id"]))
    assert again is not None and again.amount_impact == "1234.56"


def test_ledger_rejects_bad_input(project_with_anomaly):
    conn, pid, aid = project_with_anomaly
    with pytest.raises(ValueError, match="金额影响"):
        ledger.update_finding_ledger(
            conn, pid, aid, actor="甲", reason="r",
            amount_impact="12.3.4.5")
    with pytest.raises(ValueError, match="操作者"):
        ledger.update_finding_ledger(conn, pid, aid, actor="  ", reason="r")
    with pytest.raises(ValueError, match="原因"):
        ledger.update_finding_ledger(conn, pid, aid, actor="甲", reason=" ")
    with pytest.raises(ValueError, match="无效台账状态"):
        ledger.update_finding_ledger(
            conn, pid, aid, actor="甲", reason="r", ledger_status="随便")
    with pytest.raises(ValueError, match="不存在"):
        ledger.update_finding_ledger(conn, pid, 999999, actor="甲", reason="r")


def test_ledger_amount_accepts_negative_decimal(project_with_anomaly):
    conn, pid, aid = project_with_anomaly
    entry = ledger.update_finding_ledger(
        conn, pid, aid, actor="甲", reason="调差为负",
        amount_impact=("-9876.54"))
    assert entry.amount_impact == "-9876.54"
