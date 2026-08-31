"""统一摘要模型测试。"""

from pathlib import Path

from costguard.core.contracts import run_contract
from costguard.core.models import project as project_model
from costguard.core.reporting import build_project_summary, build_report_model


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
        run_contract.clear_fail_closed_state(conn, info.project_id)
        conn.close()
