"""统一摘要模型测试。"""

from pathlib import Path

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
