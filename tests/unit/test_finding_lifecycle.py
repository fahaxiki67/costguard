"""P1-06 Finding 闭环：状态、证据、审计和 fingerprint 重现。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jiadun.core.anomalies import engine as anomaly_engine
from jiadun.core.contracts import run_contract
from jiadun.core.db import migrations
from jiadun.core.evidence.finding import Finding
from jiadun.core.evidence.finding_lifecycle import (
    lifecycle_label,
    status_history,
    update_finding_status,
)


@pytest.fixture()
def db(tmp_path: Path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        project_id = conn.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at) VALUES (?,?,?,?)",
            ("Finding 闭环", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
        period_id = conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction) VALUES (?,?,?,?)",
            (project_id, 1, "第1期", "downward"),
        ).lastrowid
    yield conn, int(project_id), int(period_id)
    conn.close()


def _anomaly(conn, project_id: int, run: run_contract.RunContract, *, fingerprint: str = "fp-1") -> int:
    with conn:
        return int(conn.execute(
            """INSERT INTO anomalies(
                   project_id, rule_id, severity, subject_type, subject_id,
                   message, status, created_at, run_signature, run_id,
                   finding_id, fingerprint)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (project_id, "probe", "medium", "project", project_id, "待处理问题",
             "open", "2026", run.signature, run.run_id, f"finding:{fingerprint}", fingerprint),
        ).lastrowid)


def test_status_change_is_atomic_and_traceable(db):
    conn, project_id, _ = db
    active = run_contract.ensure_run_contract(conn, project_id)
    anomaly_id = _anomaly(conn, project_id, active)
    event = update_finding_status(
        conn, project_id, anomaly_id, "confirmed_issue", actor="李四", reason="已回查原始清单并确认差异",
    )
    assert event.before_status == "new"
    assert event.after_status == "confirmed_issue"
    row = conn.execute(
        "SELECT status, lifecycle_status, lifecycle_updated_by, resolved_note FROM anomalies WHERE id=?",
        (anomaly_id,),
    ).fetchone()
    assert tuple(row) == ("open", "confirmed_issue", "李四", "已回查原始清单并确认差异")
    ev = conn.execute(
        "SELECT kind, scope, run_id, finding_id FROM evidence WHERE id=?", (event.evidence_id,)
    ).fetchone()
    assert tuple(ev) == ("finding_status_change", "human", active.run_id, "finding:fp-1")
    audit = conn.execute(
        "SELECT actor, action, target, reason FROM audit_log WHERE id=?", (event.audit_id,)
    ).fetchone()
    assert tuple(audit) == ("李四", "update_finding_status", f"anomaly:{anomaly_id}", "已回查原始清单并确认差异")
    history = status_history(conn, project_id, anomaly_id)
    assert len(history) == 1 and history[0].evidence_id == event.evidence_id
    assert lifecycle_label("confirmed_issue") == "已确认问题"


def test_closing_requires_reason_and_events_are_immutable(db):
    conn, project_id, _ = db
    active = run_contract.ensure_run_contract(conn, project_id)
    anomaly_id = _anomaly(conn, project_id, active)
    with pytest.raises(Exception, match="原因"):
        update_finding_status(conn, project_id, anomaly_id, "closed", actor="用户", reason="")
    update_finding_status(conn, project_id, anomaly_id, "closed", actor="用户", reason="问题已整改并完成复核")
    assert conn.execute(
        "SELECT lifecycle_status, status FROM anomalies WHERE id=?", (anomaly_id,)
    ).fetchone()[:] == ("closed", "resolved")
    event_id = conn.execute("SELECT id FROM finding_status_events ORDER BY id DESC LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="finding status event immutable"):
        conn.execute("UPDATE finding_status_events SET reason='篡改' WHERE id=?", (event_id,))
    with pytest.raises(sqlite3.IntegrityError, match="finding status event immutable"):
        conn.execute("DELETE FROM finding_status_events WHERE id=?", (event_id,))


def test_database_rejects_untracked_finding_snapshot_and_status_mutation(db):
    """旧脚本不能绕过生命周期原因/事件直接篡改当前 Finding。"""
    conn, project_id, _ = db
    active = run_contract.ensure_run_contract(conn, project_id)
    anomaly_id = _anomaly(conn, project_id, active)
    with pytest.raises(sqlite3.IntegrityError, match="finding lifecycle requires"):
        conn.execute("UPDATE anomalies SET status='resolved' WHERE id=?", (anomaly_id,))
    with pytest.raises(sqlite3.IntegrityError, match="anomaly snapshot immutable"):
        conn.execute(
            "UPDATE anomalies SET repeat_history_json='[{\"tampered\":true}]' WHERE id=?",
            (anomaly_id,),
        )
    # 旧脚本若同时伪造旧 status、生命周期、操作人、时间和原因，仍不得
    # 绕过 Evidence/Audit/不可变事件闭环。
    with pytest.raises(sqlite3.IntegrityError, match="finding lifecycle requires evidence"):
        conn.execute(
            """UPDATE anomalies
               SET status='resolved', lifecycle_status='closed',
                   resolved_note='伪造关闭', lifecycle_updated_at='2026-09-01T00:00:00',
                   lifecycle_updated_by='伪造脚本'
               WHERE id=?""",
            (anomaly_id,),
        )
    # 即使旧脚本先拼出同项目但无关的 Evidence/Audit/事件，也不能冒充
    # Finding 状态闭环；数据库触发器还要核对 finding_id、运行身份、类型和目标。
    from jiadun.core.evidence import audit as audit_log
    from jiadun.core.evidence import evidence as evidence_api

    unrelated_evidence = evidence_api.add_evidence(
        conn,
        project_id,
        "source_probe",
        "无关来源证据",
        run_signature=active.signature,
        run_id=active.run_id,
        scope="current",
    )
    unrelated_audit = audit_log.record_audit(
        conn,
        project_id,
        "伪造脚本",
        "unrelated_action",
        "source:probe",
        None,
        None,
        "无关记录",
        run_id=active.run_id,
        run_signature=active.signature,
    )
    conn.execute(
        """INSERT INTO finding_status_events(
               project_id, anomaly_id, finding_id, fingerprint,
               before_status, after_status, reason, actor, occurred_at,
               run_signature, run_id, evidence_id, audit_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            project_id, anomaly_id, "finding:fp-1", "fp-1", "new", "closed",
            "伪造关闭", "伪造脚本", "2026-09-01T00:00:00",
            active.signature, active.run_id, unrelated_evidence, unrelated_audit,
        ),
    )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="finding lifecycle requires evidence, audit and linked",
    ):
        conn.execute(
            """UPDATE anomalies
               SET status='resolved', lifecycle_status='closed',
                   resolved_note='伪造关闭', lifecycle_updated_at='2026-09-01T00:00:00',
                   lifecycle_updated_by='伪造脚本'
               WHERE id=?""",
            (anomaly_id,),
        )


def test_same_fingerprint_keeps_history_but_new_finding_is_not_auto_closed(db):
    conn, project_id, _ = db

    def rule_probe(_conn, _project_id):
        return [Finding("fingerprint_probe", "medium", "project", project_id, "重复指纹问题")]

    first = anomaly_engine.run_anomalies(conn, project_id, rules=[rule_probe])
    assert len(first) == 1
    first_row = conn.execute(
        "SELECT id, lifecycle_status, repeat_history_json FROM anomalies WHERE project_id=?",
        (project_id,),
    ).fetchone()
    assert first_row["lifecycle_status"] == "new"
    update_finding_status(conn, project_id, first_row["id"], "closed", actor="用户", reason="第一次已关闭")

    second = anomaly_engine.run_anomalies(conn, project_id, rules=[rule_probe])
    assert len(second) == 1
    current = conn.execute(
        "SELECT id, lifecycle_status, status, repeat_history_json FROM anomalies "
        "WHERE project_id=? AND status='open' ORDER BY id DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    assert current["lifecycle_status"] == "new"
    assert current["status"] == "open"
    history = json.loads(current["repeat_history_json"])
    assert history and any(item["lifecycle_status"] == "closed" for item in history)
    assert current["id"] != first_row["id"]


def test_disappeared_finding_is_historical_and_reappears_with_history(db):
    """规则重跑暂未命中时不得删除 Finding；再次命中仍需从新发现开始。"""
    conn, project_id, _ = db

    def rule_probe(_conn, _project_id):
        return [Finding("disappearing_probe", "medium", "project", project_id, "间歇性问题")]

    anomaly_engine.run_anomalies(conn, project_id, rules=[rule_probe])
    first = conn.execute(
        "SELECT id, evidence_id, run_id, run_signature FROM anomalies WHERE project_id=?",
        (project_id,),
    ).fetchone()
    assert first is not None

    assert anomaly_engine.run_anomalies(conn, project_id, rules=[]) == []
    historical = conn.execute(
        "SELECT status, lifecycle_status, resolved_note FROM anomalies WHERE id=?",
        (first["id"],),
    ).fetchone()
    assert tuple(historical)[:2] == ("stale", "historical")
    assert historical["resolved_note"]
    assert conn.execute(
        "SELECT scope FROM evidence WHERE id=?", (first["evidence_id"],)
    ).fetchone()["scope"] == "historical"
    assert conn.execute(
        "SELECT COUNT(*) FROM finding_status_events WHERE anomaly_id=? AND after_status='historical'",
        (first["id"],),
    ).fetchone()[0] == 1
    scope, params = run_contract.current_scope(conn, project_id, "a")
    assert conn.execute(
        f"SELECT COUNT(*) FROM anomalies a WHERE a.project_id=? AND {scope}",
        (project_id, *params),
    ).fetchone()[0] == 0

    third = anomaly_engine.run_anomalies(conn, project_id, rules=[rule_probe])
    assert len(third) == 1
    current = conn.execute(
        "SELECT id, lifecycle_status, repeat_history_json FROM anomalies "
        "WHERE project_id=? AND lifecycle_status='new'",
        (project_id,),
    ).fetchone()
    assert current is not None and current["id"] != first["id"]
    history = json.loads(current["repeat_history_json"])
    assert any(item["anomaly_id"] == first["id"] for item in history)
