"""P0-04 黄金回归执行器的安全边界和只读比较测试。"""
from __future__ import annotations

import json
from pathlib import Path

from scripts import golden_regression

REPO_ROOT = Path(__file__).parents[2]
REGISTRY = REPO_ROOT / "tests" / "golden" / "cases.json"


def test_synthetic_registry_matches_without_updating_registry():
    """合成案例可以锁定当前基线，但执行器不得写回黄金规格。"""
    before = REGISTRY.read_bytes()

    report = golden_regression.run_golden_regression(REGISTRY)

    assert report["status"] == "passed", report
    assert report["available_case_count"] == 1
    assert report["real_case_count"] == 0
    assert report["mismatch_case_count"] == 0
    assert REGISTRY.read_bytes() == before


def test_golden_suite_runs_anonymized_registry_and_reports_real_coverage():
    report = golden_regression.run_golden_regression_suite()
    assert report["status"] == "passed", report
    assert report["registry_count"] == 2
    assert report["real_case_count"] == 0
    assert any(
        item["registry"].endswith("anonymized_golden_cases/cases.json")
        for item in report["reports"]
    )


def test_golden_suite_isolates_each_registry_workspace(tmp_path):
    """并行登记表的同名案例不能共享输出目录或相互覆盖。"""
    report = golden_regression.run_golden_regression_suite(
        output=tmp_path, keep_workspace=True
    )
    workspaces = [Path(item["workspace"]) for item in report["reports"]]
    assert workspaces[0] != workspaces[1]
    assert workspaces[0].name == "registry-1"
    assert workspaces[1].name == "registry-2"


def test_mismatch_reports_field_path_and_preserves_registry(tmp_path):
    """黄金值变化必须报告差异，不能被执行器自动更新掩盖。"""
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    data["cases"] = [data["cases"][0]]
    data["cases"][0]["expected"]["file_count"] = 999
    registry = tmp_path / "cases.json"
    registry.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    before = registry.read_bytes()

    report = golden_regression.run_golden_regression(registry)

    assert report["status"] == "failed"
    assert report["mismatch_case_count"] == 1
    diffs = report["results"][0]["diffs"]
    assert any(
        diff["path"] == "file_count" and diff["expected"] == 999 and diff["actual"] == 4
        for diff in diffs
    )
    assert registry.read_bytes() == before


def test_private_input_path_is_rejected(tmp_path):
    """黄金库不能越过边界读取 local_private_data。"""
    registry = tmp_path / "private.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cases": [
                    {
                        "case_id": "private_case",
                        "case_kind": "sanitized_real",
                        "availability": "available",
                        "inputs": [
                            {
                                "path": "local_private_data/secret.xlsx",
                                "type": "xlsx",
                                "direction": "upward",
                            }
                        ],
                        "expected": {},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = golden_regression.run_golden_regression(registry)

    assert report["status"] == "failed"
    assert "local_private_data" in report["results"][0]["error"]


def test_require_real_returns_distinct_gate_code():
    """没有真实黄金案例时，发布门槛返回专用状态码而非误报通过。"""
    assert golden_regression.main(["--registry", str(REGISTRY), "--require-real"]) == 2


def test_compare_metrics_rejects_empty_expected_and_extra_actual_fields():
    """黄金比较采用闭世界口径，不能因 expected 为空或 actual 多字段而静默通过。"""
    empty = golden_regression.compare_metrics({"file_count": 1}, {})
    assert empty and any(diff["kind"] == "contract_or_scope" for diff in empty)

    extra = golden_regression.compare_metrics(
        {"file_count": 1, "crosscheck": {"sufficient_period_count": 0}},
        {"file_count": 1},
    )
    assert any(diff["path"] == "crosscheck.sufficient_period_count" for diff in extra)
