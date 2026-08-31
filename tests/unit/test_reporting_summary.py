"""统一摘要模型测试。"""

import json
from pathlib import Path

import pytest

from costguard.core.anomalies import coverage
from costguard.core.contracts import run_contract
from costguard.core.models import project as project_model
from costguard.core.reporting import build_project_summary, build_report_model
from costguard.core.reporting.summary import _statuses


def test_report_model_has_one_shared_summary_and_no_approval_upgrade(tmp_path: Path):
    info = project_model.create_project("摘要模型测试", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    summary = build_project_summary(conn, info.project_id)
    model = build_report_model(conn, info.project_id)
    assert summary.as_dict() == model.management_summary.as_dict()
    assert model.project_summary is model.management_summary.project_summary
    assert summary.data_cutoff is None
    assert summary.data_cutoff_status == "not_available"
    assert summary.statuses["business_confirmation"] == "not_requested"
    assert summary.statuses["approval"] == "not_requested"
    assert summary.detection_coverage["status"] == "not_started"
    assert summary.aggregate_coverage["status"] == "not_started"
    assert summary.pending["manifest_status"] == "not_available"
    conn.close()


def test_summary_prioritizes_run_unavailable_over_historical_success(tmp_path: Path):
    """摘要必须明确当前结果不可用，不能把旧签名成功显示为当前成功。"""
    info = project_model.create_project("摘要不可用边界", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction) "
            "VALUES (?, 1, '第1期', 'downward')",
            (info.project_id,),
        )
        run_contract.ensure_run_contract(conn, info.project_id)
        run_contract.set_fail_closed_state(
            conn,
            info.project_id,
            reason="synthetic database is not writable",
        )

        summary = build_project_summary(conn, info.project_id)

        assert summary.run_availability["available"] is False
        assert summary.run_availability["status"] == run_contract.FAIL_CLOSED_STATUS
        assert summary.verification["status"] == run_contract.FAIL_CLOSED_STATUS
        assert summary.statuses["automatic_analysis"] == "failed"
        assert summary.aggregate_coverage["fail_closed"] is True
        assert summary.aggregate_coverage["status"] != "complete"
    finally:
        conn.close()


def test_summary_never_marks_findings_or_unchecked_periods_complete():
    complete_coverage = {"status": coverage.COMPLETE}
    pending = {
        "sheets": 0,
        "matches": 0,
        "anomalies": 0,
        "manifest_status": "not_available",
    }
    risk = {}

    findings = _statuses(
        {"status": "findings", "periods_unchecked": 0},
        risk,
        pending,
        complete_coverage,
        complete_coverage,
        source_files=1,
        period_count=1,
    )
    assert findings["automatic_analysis"] == "partial"

    unchecked = _statuses(
        {"status": "sufficient", "periods_unchecked": 1},
        risk,
        pending,
        complete_coverage,
        complete_coverage,
        source_files=1,
        period_count=2,
    )
    assert unchecked["automatic_analysis"] == "partial"


def test_partial_coverage_cannot_clear_pending_fail_closed_state(tmp_path: Path):
    info = project_model.create_project("部分覆盖清除门控", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        contract = run_contract.ensure_run_contract(conn, info.project_id)
        run_contract.set_fail_closed_state(conn, info.project_id, reason="partial run")
        run_id = coverage.record_detection_run(
            conn,
            info.project_id,
            coverage.coverage_from_values(["a", "b"], ["a"]),
            run_signature=contract.signature,
            run_kind=coverage.AGGREGATE_VALIDATION,
        )
        run_contract.defer_fail_closed_state_clear(
            conn,
            info.project_id,
            run_signature=contract.signature,
            coverage_run_id=run_id,
            coverage_run_kind=coverage.AGGREGATE_VALIDATION,
        )
        assert not run_contract.current_results_available(conn, info.project_id)["available"]
    finally:
        conn.close()


def test_declarative_complete_coverage_cannot_clear_without_business_results(tmp_path: Path):
    """项目有期次时，形状正确的 coverage 不能替代真实 A/B/C 结果。"""
    info = project_model.create_project("声明式覆盖不能清除", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        with conn:
            conn.executemany(
                """INSERT INTO settlement_periods(project_id, period_no, title, direction)
                   VALUES (?, ?, ?, ?)""",
                [
                    (info.project_id, 1, "第1期", "upward"),
                    (info.project_id, 2, "第2期", "downward"),
                ],
            )
        contract = run_contract.ensure_run_contract(conn, info.project_id)
        run_contract.set_fail_closed_state(conn, info.project_id, reason="proof binding")
        expected = list(
            run_contract._aggregate_expected_coverage_keys(conn, info.project_id)
        )
        with pytest.raises(ValueError, match="只能由 run_crosscheck"):
            coverage.record_detection_run(
                conn,
                info.project_id,
                coverage.coverage_from_values(expected, expected),
                run_signature=contract.signature,
                run_kind=coverage.AGGREGATE_VALIDATION,
            )
        with conn:
            cursor = conn.execute(
                """INSERT INTO detection_runs(
                       project_id, run_signature, run_kind, started_at, completed_at,
                       status, expected_json, executed_json, skipped_json, failed_json,
                       critical_failed_json, error_summary, metadata_json)
                   VALUES (?, ?, ?, '2026', '2026', 'complete', ?, ?, '{}', '{}', '[]', NULL, '{}')""",
                (
                    info.project_id,
                    contract.signature,
                    coverage.AGGREGATE_VALIDATION,
                    json.dumps(expected),
                    json.dumps(expected),
                ),
            )
        run_id = int(cursor.lastrowid)

        with pytest.raises(RuntimeError, match="完整成功运行证明"):
            run_contract.clear_fail_closed_state(
                conn,
                info.project_id,
                run_signature=contract.signature,
                coverage_run_id=run_id,
                coverage_run_kind=coverage.AGGREGATE_VALIDATION,
            )
        assert not run_contract.current_results_available(conn, info.project_id)["available"]
    finally:
        conn.close()


def test_fail_closed_recovery_kind_is_fixed(tmp_path: Path):
    info = project_model.create_project("固定恢复依据", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        with pytest.raises(ValueError, match="只能由 aggregate_validation"):
            run_contract.set_fail_closed_state(
                conn,
                info.project_id,
                reason="invalid recovery kind",
                recovery_run_kind=coverage.ANOMALY_DETECTION,
            )
        assert run_contract.get_fail_closed_state(conn, info.project_id) is None
    finally:
        conn.close()


def test_complete_coverage_clears_pending_state_after_outer_commit(tmp_path: Path):
    """完整成功覆盖应在外层提交后清除 pending，不得因字段读取崩溃。"""
    info = project_model.create_project("完整覆盖清除门控", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        contract = run_contract.ensure_run_contract(conn, info.project_id)
        run_contract.set_fail_closed_state(
            conn, info.project_id, run_signature=contract.signature, reason="pending success"
        )
        run_id = coverage.record_detection_run(
            conn,
            info.project_id,
            coverage.coverage_from_values(["a", "b"], ["a", "b"]),
            run_signature=contract.signature,
            run_kind=coverage.AGGREGATE_VALIDATION,
        )
        run_contract.defer_fail_closed_state_clear(
            conn,
            info.project_id,
            run_signature=contract.signature,
            coverage_run_id=run_id,
            coverage_run_kind=coverage.AGGREGATE_VALIDATION,
        )

        availability = run_contract.current_results_available(conn, info.project_id)

        assert availability["available"] is True
        assert run_contract.get_fail_closed_state(conn, info.project_id) is None
        assert not run_contract.fail_closed_state_path(conn, info.project_id).exists()
    finally:
        conn.close()


def test_clear_requires_strict_current_latest_complete_proof(tmp_path: Path):
    """无证明、伪造额外执行项和旧 complete 行均不能解除边界。"""
    info = project_model.create_project("严格覆盖证明", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        contract = run_contract.ensure_run_contract(conn, info.project_id)
        run_contract.set_fail_closed_state(
            conn, info.project_id, run_signature=contract.signature, reason="proof probe"
        )
        first_id = coverage.record_detection_run(
            conn,
            info.project_id,
            coverage.coverage_from_values(["a"], ["a"]),
            run_signature=contract.signature,
            run_kind=coverage.AGGREGATE_VALIDATION,
        )
        conn.execute(
            "UPDATE detection_runs SET executed_json=? WHERE id=?",
            ('["a", "forged"]', first_id),
        )
        with pytest.raises(RuntimeError, match="完整成功运行证明"):
            run_contract.clear_fail_closed_state(conn, info.project_id)
        with pytest.raises(RuntimeError, match="完整成功运行证明"):
            run_contract.clear_fail_closed_state(
                conn,
                info.project_id,
                run_signature=contract.signature,
                coverage_run_id=first_id,
                coverage_run_kind=coverage.AGGREGATE_VALIDATION,
            )

        second_id = coverage.record_detection_run(
            conn,
            info.project_id,
            coverage.coverage_from_values(["a"], []),
            run_signature=contract.signature,
            run_kind=coverage.AGGREGATE_VALIDATION,
        )
        assert second_id > first_id
        with pytest.raises(RuntimeError, match="完整成功运行证明"):
            run_contract.clear_fail_closed_state(
                conn,
                info.project_id,
                run_signature=contract.signature,
                coverage_run_id=first_id,
                coverage_run_kind=coverage.AGGREGATE_VALIDATION,
            )
        assert not run_contract.current_results_available(conn, info.project_id)["available"]
    finally:
        conn.close()


def test_corrupt_fail_closed_sidecar_requires_success_proof_to_clear(tmp_path: Path):
    info = project_model.create_project("损坏状态清除门控", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        contract = run_contract.ensure_run_contract(conn, info.project_id)
        run_contract.set_fail_closed_state(conn, info.project_id, reason="corrupt probe")
        state_path = run_contract.fail_closed_state_path(conn, info.project_id)
        state_path.write_text("{broken", encoding="utf-8")
        assert run_contract.get_fail_closed_state(conn, info.project_id)["corrupt"] is True
        with pytest.raises(RuntimeError, match="完整成功运行证明"):
            run_contract.clear_fail_closed_state(conn, info.project_id)
        assert state_path.exists()
        run_id = coverage.record_detection_run(
            conn,
            info.project_id,
            coverage.coverage_from_values(["a"], ["a"]),
            run_signature=contract.signature,
            run_kind=coverage.AGGREGATE_VALIDATION,
        )
        run_contract.clear_fail_closed_state(
            conn,
            info.project_id,
            run_signature=contract.signature,
            coverage_run_id=run_id,
            coverage_run_kind=coverage.AGGREGATE_VALIDATION,
        )
        assert not state_path.exists()
    finally:
        conn.close()
