"""检测覆盖率与技术失败的独立记录测试。"""

from pathlib import Path

from jiadun.core.anomalies import coverage, engine, rules
from jiadun.core.contracts import run_contract
from jiadun.core.models import project as project_model


def _project(tmp_path: Path):
    info = project_model.create_project("覆盖率测试", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    return info, conn


def test_coverage_status_is_fail_closed():
    assert coverage.coverage_from_values(["a", "b"], ["a"]).status == coverage.PARTIAL
    assert coverage.coverage_from_values(["a", "b"], ["a"], {"b": "不适用"}).status == coverage.PARTIAL
    assert coverage.coverage_from_values(["a"], [], failed={"a": "boom"}).status == coverage.FAILED
    assert coverage.coverage_from_values([], []).status == coverage.NOT_STARTED


def test_skipped_rule_is_partial_not_complete():
    assert coverage.coverage_from_values(
        ["a", "b"], ["a"], {"b": "不适用"}
    ).status == coverage.PARTIAL


def test_unexpected_coverage_values_are_failed_not_filtered():
    """producer 层收到额外键时必须保留事实并失败关闭，不能过滤后 complete。"""
    value = coverage.coverage_from_values(
        expected=["a"],
        executed=["a", "unexpected"],
        skipped={"unexpected": "错误阶段"},
        failed={"unexpected": "错误阶段"},
        critical_failed=["unexpected"],
    )
    assert "unexpected" in value.executed
    assert "unexpected" in value.skipped
    assert "unexpected" in value.failed
    assert "unexpected" in value.critical_failed
    assert value.status == coverage.FAILED
    assert coverage.coverage_from_values(expected=[], executed=["unexpected"]).status == coverage.FAILED


def test_critical_failure_cannot_appear_complete():
    value = coverage.DetectionCoverage(
        expected=("rule-a",), executed=("rule-a",), critical_failed=("rule-a",)
    )
    assert value.status == coverage.FAILED


def test_rule_failure_is_not_persisted_as_low_business_anomaly(tmp_path):
    info, conn = _project(tmp_path)

    def broken_rule(_conn, _project_id):
        raise RuntimeError("synthetic detector failure")

    findings = engine.run_anomalies(conn, info.project_id, rules=[broken_rule, rules.rule_negative_qty])
    assert any(item.rule_id.startswith("rule_error_") for item in findings)
    run = coverage.current_detection_run(conn, info.project_id)
    assert run and run["status"] == coverage.FAILED
    assert coverage.coverage_summary(conn, info.project_id)["failed_count"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM anomalies WHERE project_id=? AND rule_id LIKE 'rule_error_%'",
        (info.project_id,),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE project_id=? AND kind='detection_failure'",
        (info.project_id,),
    ).fetchone()[0] == 1
    conn.close()


def test_detection_failure_evidence_is_historical_after_successful_rerun(tmp_path):
    """失败→成功重跑时，旧技术证据不能继续进入当前 Evidence 范围。"""
    info, conn = _project(tmp_path)

    def broken_rule(_conn, _project_id):
        raise RuntimeError("synthetic detector failure")

    engine.run_anomalies(conn, info.project_id, rules=[broken_rule])
    failure = conn.execute(
        "SELECT id, run_id, run_signature, scope FROM evidence "
        "WHERE project_id=? AND kind='detection_failure'",
        (info.project_id,),
    ).fetchone()
    assert failure is not None and failure["scope"] == "current"

    engine.run_anomalies(conn, info.project_id, rules=[])
    historical = conn.execute(
        "SELECT scope, historical_reason FROM evidence WHERE id=?", (failure["id"],)
    ).fetchone()
    assert historical["scope"] == "historical"
    assert historical["historical_reason"]
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence_events WHERE evidence_id=? AND event_type='invalidated'",
        (failure["id"],),
    ).fetchone()[0] == 1
    scope, params = run_contract.current_scope(conn, info.project_id, "e")
    assert conn.execute(
        f"SELECT COUNT(*) FROM evidence e WHERE e.project_id=? AND {scope}",
        (info.project_id, *params),
    ).fetchone()[0] == 0
    conn.close()
