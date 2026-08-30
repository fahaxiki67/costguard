"""检测覆盖率与技术失败的独立记录测试。"""

from pathlib import Path

from costguard.core.anomalies import coverage, engine, rules
from costguard.core.models import project as project_model


def _project(tmp_path: Path):
    info = project_model.create_project("覆盖率测试", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    return info, conn


def test_coverage_status_is_fail_closed():
    assert coverage.coverage_from_values(["a", "b"], ["a"]).status == coverage.PARTIAL
    assert coverage.coverage_from_values(["a", "b"], ["a"], {"b": "不适用"}).status == coverage.COMPLETE
    assert coverage.coverage_from_values(["a"], [], failed={"a": "boom"}).status == coverage.FAILED
    assert coverage.coverage_from_values([], []).status == coverage.NOT_STARTED


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
