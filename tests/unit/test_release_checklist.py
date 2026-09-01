"""发布清单的门禁状态、证据边界和输出安全测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts import release_checklist

REPO_ROOT = Path(__file__).parents[2]


def test_checklist_blocks_release_without_real_golden_case_by_default(tmp_path):
    report = release_checklist.build_checklist(REPO_ROOT, output_dir=tmp_path)

    assert report["overall_status"] == "failed"
    assert report["production_release_ready"] is False
    by_id = {item["id"]: item for item in report["items"]}
    assert by_id["golden_regression"]["status"] == "failed"
    assert by_id["golden_regression"]["gate_status"] == "blocked"
    assert "real_case_count=0" in by_id["golden_regression"]["detail"]
    assert by_id["performance_1w_5w_20w"]["status"] == "not_run"
    assert by_id["office_four_environment"]["status"] == "conditional"
    assert by_id["package_signature"]["status"] in {"conditional", "not_available"}
    assert by_id["unresolved_p0_p1"]["status"] == "conditional"


def test_checklist_allows_missing_real_case_only_in_explicit_development_mode(tmp_path):
    report = release_checklist.build_checklist(
        REPO_ROOT,
        output_dir=tmp_path,
        allow_no_real=True,
    )

    assert report["overall_status"] == "conditional"
    assert report["production_release_ready"] is False
    by_id = {item["id"]: item for item in report["items"]}
    assert by_id["golden_regression"]["status"] == "conditional"
    assert by_id["golden_regression"]["gate_status"] == "development_override"
    assert "allow_no_real" in by_id["golden_regression"]["detail"]


def test_checklist_skip_golden_is_a_blocked_release_gate(tmp_path):
    report = release_checklist.build_checklist(
        REPO_ROOT,
        output_dir=tmp_path,
        run_golden=False,
    )

    assert report["overall_status"] == "failed"
    assert report["production_release_ready"] is False
    item = next(item for item in report["items"] if item["id"] == "golden_regression")
    assert item["status"] == "failed"
    assert item["gate_status"] == "blocked"
    assert "未运行黄金回归" in item["detail"]


def test_write_checklist_emits_json_and_markdown_without_private_path(tmp_path):
    report = release_checklist.build_checklist(REPO_ROOT, output_dir=tmp_path)

    json_path, markdown_path = release_checklist.write_checklist(report, tmp_path / "out")

    assert json_path.is_file()
    assert markdown_path.is_file()
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == 1
    assert "Jiadun" in markdown_path.read_text(encoding="utf-8")


def test_checklist_rejects_private_output(tmp_path):
    private_output = tmp_path / "local_private_data" / "release"

    with pytest.raises(ValueError, match="local_private_data"):
        release_checklist.build_checklist(REPO_ROOT, output_dir=private_output)


def test_checklist_can_read_cancelled_performance_report_without_rerun(tmp_path):
    report_path = tmp_path / "performance.json"
    report_path.write_text(
        json.dumps(
            {
                "status": "cancelled",
                "config": {"sizes": [10000, 50000, 200000]},
                "results": [{"status": "completed"}, {"status": "cancelled"}],
            }
        ),
        encoding="utf-8",
    )

    report = release_checklist.build_checklist(
        REPO_ROOT,
        performance_report=report_path,
        output_dir=tmp_path / "out",
    )

    item = next(item for item in report["items"] if item["id"] == "performance_1w_5w_20w")
    assert item["status"] == "conditional"
    assert "cancelled" in item["detail"]


def test_running_performance_cancellation_is_conditional(tmp_path, monkeypatch):
    from scripts import performance_benchmark

    monkeypatch.setattr(
        performance_benchmark,
        "run_benchmark",
        lambda *_args, **_kwargs: {
            "status": "cancelled",
            "config": {"sizes": [10000, 50000, 200000]},
            "results": [{"status": "completed"}, {"status": "cancelled"}],
            "output_paths": {"json": str(tmp_path / "performance.json")},
        },
    )

    item = release_checklist._performance_item(
        REPO_ROOT,
        output_dir=tmp_path,
        run_performance=True,
        performance_report=None,
        keep_workspace=False,
    )

    assert item["status"] == "conditional"
    assert "cancelled" in item["detail"]
