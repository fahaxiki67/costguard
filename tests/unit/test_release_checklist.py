"""发布清单的门禁状态、证据边界和输出安全测试。"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import openpyxl
import pytest
from scripts import release_checklist

from jiadun.core.acceptance import build_acceptance_bundle, canonical_bundle_hash

REPO_ROOT = Path(__file__).parents[2]


def _complete_performance_payload(tmp_path: Path, *, create_artifacts: bool = True,
                                  duplicate_stage: bool = False) -> tuple[dict, Path]:
    """构造带现场导出文件的完整三规模报告，用于发布闸门反例/正例。"""
    output_root = tmp_path / "benchmark"
    run_id = "run-test"
    report_path = output_root / "runs" / run_id / "performance_benchmark.json"
    results = []
    stage_names = list(release_checklist.PERFORMANCE_STAGE_NAMES)

    def write_xlsx(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        workbook = openpyxl.Workbook()
        workbook.active["A1"] = "Jiadun test workbook"
        workbook.save(path)
        workbook.close()

    for size in release_checklist.PERFORMANCE_SIZES:
        rows_per_direction = {"upward": size // 2, "downward": size - size // 2}
        input_files = []
        input_paths = []
        for direction, rows in rows_per_direction.items():
            input_path = (
                output_root
                / "work"
                / run_id
                / f"size-{size}"
                / "inputs"
                / f"input-{direction}-{size}.xlsx"
            )
            write_xlsx(input_path)
            input_paths.append(input_path)
            input_files.append(
                {
                    "direction": direction,
                    "rows": rows,
                    "subtotal": "1.00",
                    "file": input_path.name,
                    "bytes": input_path.stat().st_size,
                    "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                }
            )
        stages = [
            {
                "name": name,
                "status": "completed",
                "elapsed_seconds": 0.001,
                "details": {},
            }
            for name in stage_names
        ]
        artifact = output_root / "work" / run_id / f"size-{size}" / "exports" / f"export-{size}.xlsx"
        if create_artifacts:
            write_xlsx(artifact)
        data = artifact.read_bytes() if artifact.is_file() else b"x"
        generation_stage = next(stage for stage in stages if stage["name"] == "合成数据生成")
        generation_stage["details"] = {
            "total_detail_rows": size,
            "rows_per_direction": rows_per_direction,
            "files": input_files,
        }
        import_stage = next(stage for stage in stages if stage["name"] == "Excel 合成导入")
        import_stage["details"] = {
            "project_id": 1,
            "reports": [
                {"direction": direction, "status": "ok"}
                for direction in rows_per_direction
            ],
            "imported_detail_rows": size,
        }
        next(stage for stage in stages if stage["name"] == "清单分页打开")["details"] = {
            "total_rows": size,
            "page_size": 500,
            "returned_rows": min(500, size),
        }
        next(stage for stage in stages if stage["name"] == "清单搜索")["details"] = {
            "search": "CG000000001",
            "total_rows": 2,
            "returned_rows": 2,
        }
        next(stage for stage in stages if stage["name"] == "异常检测")["details"] = {
            "finding_count": 0,
            "by_severity": {},
        }
        next(stage for stage in stages if stage["name"] == "匹配计算")["details"] = {
            "group_count": 0,
        }
        next(stage for stage in stages if stage["name"] == "对上/对下双向校核")["details"] = {
            "checks": [
                {
                    "period_no": 1,
                    "direction": direction,
                    "verification_level": "sufficient",
                    "status": "match",
                    "detail_rows": rows,
                    "path_a_total": "1.00",
                    "path_b_total": "1.00",
                    "control_status": "match",
                    "control_diff": "0.00",
                    "range_unproven_sheets": 0,
                }
                for direction, rows in rows_per_direction.items()
            ],
        }
        export_stage = next(stage for stage in stages if stage["name"] == "Excel 审核底稿导出")
        export_stage["details"] = {
            "file": artifact.name,
            "path": str(artifact),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if duplicate_stage:
            stages.append({"name": "匹配计算", "status": "completed", "details": {}})
        results.append(
            {
                "size": size,
                "status": "completed",
                "total_detail_rows": size,
                "rows_per_direction": rows_per_direction,
                "stages": stages,
                "workspace_retained": True,
                "input": {
                    "total_detail_rows": size,
                    "rows_per_direction": rows_per_direction,
                    "files": input_files,
                },
                "run_contract_signature": "synthetic-test-signature",
            }
        )
    payload = {
        "schema_version": 1,
        "benchmark": "Jiadun synthetic performance benchmark",
        "benchmark_version": release_checklist._version(REPO_ROOT),
        "generated_at": "2026-09-02T00:00:00+00:00",
        "status": "completed",
        "config": {"sizes": list(release_checklist.PERFORMANCE_SIZES), "skip_export": False},
        "environment": {"system": "test", "machine": "test"},
        "results": results,
        "output_paths": {
            "json": str(report_path),
            "markdown": str(report_path.with_suffix(".md")),
        },
        "workspace": str(output_root / "work" / run_id),
    }
    payload["acceptance_bundle"] = build_acceptance_bundle(
        run_id=run_id,
        repo_root=REPO_ROOT,
        input_paths=[
            output_root
            / "work"
            / run_id
            / f"size-{result['size']}"
            / "inputs"
            / file_info["file"]
            for result in results
            for file_info in result["input"]["files"]
        ],
        output_paths=[
            Path(stage["details"]["path"])
            for result in results
            for stage in result["stages"]
            if stage["name"] == "Excel 审核底稿导出"
        ],
        stages=[
            {"size": result["size"], **stage}
            for result in results
            for stage in result["stages"]
        ],
        run_contract_signature={
            str(result["size"]): result["run_contract_signature"] for result in results
        },
        config=payload["config"],
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload, report_path


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


def test_checklist_blocks_non_pass_canonical_golden_statuses(monkeypatch, tmp_path):
    """发布清单不能绕过黄金回归的 PENDING/INCOMPARABLE 状态。"""
    from scripts import golden_regression

    fake = {
        "status": "passed",
        "available_case_count": 2,
        "real_case_count": 1,
        "not_available_case_count": 0,
        "mismatch_case_count": 0,
        "comparison_status_counts": {"PASS": 1, "PENDING": 1, "INCOMPARABLE": 1},
    }
    monkeypatch.setattr(
        golden_regression,
        "run_golden_regression_suite",
        lambda **_kwargs: fake,
    )

    report = release_checklist.build_checklist(REPO_ROOT, output_dir=tmp_path)

    assert report["overall_status"] == "failed"
    assert report["production_release_ready"] is False
    item = next(item for item in report["items"] if item["id"] == "golden_regression")
    assert item["status"] == "failed"
    assert item["gate_status"] == "failed"
    assert "INCOMPARABLE=1" in item["detail"]


@pytest.mark.parametrize(
    "counts",
    [{"PASS": 1, "FAIL": "oops"}, {"PASS": 1, "PENDING": -1}],
)
def test_checklist_rejects_malformed_canonical_counts(monkeypatch, tmp_path, counts):
    """发布清单不能把非法 canonical 计数清洗为绿色通过。"""
    from scripts import golden_regression

    fake = {
        "status": "passed",
        "available_case_count": 1,
        "real_case_count": 1,
        "not_available_case_count": 0,
        "mismatch_case_count": 0,
        "comparison_status_counts": counts,
    }
    monkeypatch.setattr(golden_regression, "run_golden_regression_suite", lambda **_kwargs: fake)

    report = release_checklist.build_checklist(REPO_ROOT, output_dir=tmp_path)

    item = next(item for item in report["items"] if item["id"] == "golden_regression")
    assert item["status"] == "failed"
    assert item["gate_status"] == "failed"
    assert "canonical_errors" in item["detail"]
    assert report["production_release_ready"] is False


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


def test_cancelled_performance_report_requires_structured_termination_evidence():
    """取消现场即使不放行，也必须说明规模、阶段和原因，不能用空壳报告掩盖。"""
    payload = {
        "schema_version": 1,
        "status": "cancelled",
        "config": {"sizes": [10000, 50000, 200000], "skip_export": True},
        "results": [
            {
                "size": 10000,
                "status": "cancelled",
                "stages": [
                    {"name": "合成数据生成", "status": "cancelled", "details": {}}
                ],
            }
        ],
    }

    errors = release_checklist._validate_performance_report(payload)

    assert any("termination" in error or "终止" in error for error in errors)


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


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "completed",
            "config": {"sizes": [10000, 50000, 200000], "skip_export": False},
            "results": [],
        },
        {
            "status": "completed",
            "config": {"sizes": [10000, 50000, 200000], "skip_export": False},
            "results": [{"size": 10000, "status": "completed", "stages": []}],
        },
        {"status": "completed", "config": None, "results": []},
        {
            "status": "completed",
            "config": {"sizes": [10000, 50000, 200000], "skip_export": True},
            "results": [
                {"size": size, "status": "completed", "stages": []}
                for size in (10000, 50000, 200000)
            ],
        },
    ],
)
def test_completed_performance_report_without_complete_evidence_never_passes(
    tmp_path, payload
):
    """性能报告缺结果、配置损坏或跳过导出时不能因 all([]) 变成绿色。"""
    report_path = tmp_path / "performance.json"
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    report = release_checklist.build_checklist(
        REPO_ROOT,
        performance_report=report_path,
        output_dir=tmp_path / "out",
    )

    item = next(item for item in report["items"] if item["id"] == "performance_1w_5w_20w")
    assert item["status"] in {"failed", "conditional"}
    assert item["status"] != "passed"


def test_completed_performance_report_requires_unique_sizes_and_all_stages(tmp_path):
    """即便声明三种规模，尺寸重复或阶段不全也不能冒充完整性能证据。"""
    report_path = tmp_path / "performance.json"
    report_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "config": {"sizes": [10000, 50000, 200000], "skip_export": False},
                "results": [
                    {"size": 10000, "status": "completed", "stages": []},
                    {"size": 10000, "status": "completed", "stages": []},
                    {"size": 200000, "status": "completed", "stages": []},
                ],
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
    assert item["status"] == "failed"


def test_completed_performance_report_requires_existing_hashed_export(tmp_path):
    """完整字段不能替代本次性能现场的真实导出文件和 SHA-256。"""
    _payload, report_path = _complete_performance_payload(tmp_path, create_artifacts=False)

    report = release_checklist.build_checklist(
        REPO_ROOT,
        performance_report=report_path,
        output_dir=tmp_path / "out",
    )

    item = next(item for item in report["items"] if item["id"] == "performance_1w_5w_20w")
    assert item["status"] == "failed"
    assert "不存在" in item["detail"]


def test_completed_performance_report_rejects_self_consistent_but_empty_evidence(tmp_path):
    """不能只凭自洽的文件名、字节数和 SHA 把空壳报告写成绿色。"""
    payload, report_path = _complete_performance_payload(tmp_path, create_artifacts=True)
    first = payload["results"][0]
    first.pop("total_detail_rows", None)
    first.pop("rows_per_direction", None)
    for stage in first["stages"]:
        stage.pop("elapsed_seconds", None)
    payload.pop("acceptance_bundle", None)
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    report = release_checklist.build_checklist(
        REPO_ROOT,
        performance_report=report_path,
        output_dir=tmp_path / "out",
    )

    item = next(item for item in report["items"] if item["id"] == "performance_1w_5w_20w")
    assert item["status"] == "failed"
    assert "total_detail_rows" in item["detail"]
    assert "acceptance_bundle" in item["detail"]


def test_completed_performance_report_rejects_deleted_input_file(tmp_path):
    """输入文件必须在发布复核时仍存在，报告自报的 SHA 不能替代现场文件。"""
    _payload, report_path = _complete_performance_payload(tmp_path, create_artifacts=True)
    input_path = (
        tmp_path
        / "benchmark"
        / "work"
        / "run-test"
        / "size-10000"
        / "inputs"
        / "input-upward-10000.xlsx"
    )
    input_path.unlink()

    report = release_checklist.build_checklist(
        REPO_ROOT,
        performance_report=report_path,
        output_dir=tmp_path / "out",
    )

    item = next(item for item in report["items"] if item["id"] == "performance_1w_5w_20w")
    assert item["status"] == "failed"
    assert "输入文件不存在" in item["detail"]


def test_completed_performance_report_rejects_stale_benchmark_version(tmp_path):
    """旧版本性能现场不能复用为当前版本的性能证据。"""
    payload, report_path = _complete_performance_payload(tmp_path, create_artifacts=True)
    payload["benchmark_version"] = "0.1.16"
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    report = release_checklist.build_checklist(
        REPO_ROOT,
        performance_report=report_path,
        output_dir=tmp_path / "out",
    )

    item = next(item for item in report["items"] if item["id"] == "performance_1w_5w_20w")
    assert item["status"] == "failed"
    assert "benchmark_version" in item["detail"]


def test_completed_performance_report_rejects_zip_that_is_not_openable_xlsx(tmp_path):
    """ZIP 外壳即使自洽，也不能替代可由 openpyxl 打开的 XLSX。"""
    payload, report_path = _complete_performance_payload(tmp_path, create_artifacts=True)
    export_stage = next(
        stage
        for stage in payload["results"][0]["stages"]
        if stage["name"] == "Excel 审核底稿导出"
    )
    artifact = Path(export_stage["details"]["path"])
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    data = artifact.read_bytes()
    export_stage["details"]["bytes"] = len(data)
    export_stage["details"]["sha256"] = hashlib.sha256(data).hexdigest()
    bundle_output = next(
        entry
        for entry in payload["acceptance_bundle"]["outputs"]
        if entry["path"] == artifact.name
    )
    bundle_output["size_bytes"] = len(data)
    bundle_output["sha256"] = export_stage["details"]["sha256"]
    payload["acceptance_bundle"]["integrity"]["bundle_sha256"] = canonical_bundle_hash(
        payload["acceptance_bundle"]
    )
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    report = release_checklist.build_checklist(
        REPO_ROOT,
        performance_report=report_path,
        output_dir=tmp_path / "out",
    )

    item = next(item for item in report["items"] if item["id"] == "performance_1w_5w_20w")
    assert item["status"] == "failed"
    assert "openpyxl" in item["detail"] or "无法作为 XLSX" in item["detail"]


def test_completed_performance_report_with_real_exports_can_pass_performance_gate(tmp_path):
    """真实现场文件存在且哈希一致时，性能条目才允许通过。"""
    _payload, report_path = _complete_performance_payload(tmp_path, create_artifacts=True)

    report = release_checklist.build_checklist(
        REPO_ROOT,
        performance_report=report_path,
        output_dir=tmp_path / "out",
    )

    item = next(item for item in report["items"] if item["id"] == "performance_1w_5w_20w")
    assert item["status"] == "passed"


def test_completed_performance_report_rejects_duplicate_stage_names(tmp_path):
    """重复阶段不得用后一个 completed 覆盖前一个失败/异常证据。"""
    _payload, report_path = _complete_performance_payload(tmp_path, duplicate_stage=True)

    report = release_checklist.build_checklist(
        REPO_ROOT,
        performance_report=report_path,
        output_dir=tmp_path / "out",
    )

    item = next(item for item in report["items"] if item["id"] == "performance_1w_5w_20w")
    assert item["status"] == "failed"
    assert "阶段名称不得重复" in item["detail"]


@pytest.mark.parametrize("mutation", ["extra_non_object", "non_export_details_list"])
def test_completed_performance_report_rejects_malformed_stage_shape(tmp_path, mutation):
    """外部性能 JSON 的阶段结构必须闭合，不能只校验必需阶段名称。"""
    payload, report_path = _complete_performance_payload(tmp_path, create_artifacts=True)
    if mutation == "extra_non_object":
        payload["results"][0]["stages"].append("malformed-extra-stage")
    else:
        next(
            stage
            for stage in payload["results"][0]["stages"]
            if stage["name"] == "匹配计算"
        )["details"] = []
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    report = release_checklist.build_checklist(
        REPO_ROOT,
        performance_report=report_path,
        output_dir=tmp_path / "out",
    )

    item = next(item for item in report["items"] if item["id"] == "performance_1w_5w_20w")
    assert item["status"] == "failed"
    assert "stages" in item["detail"] or "details" in item["detail"]


def test_internal_performance_run_uses_strict_validator(monkeypatch, tmp_path):
    """--run-performance 内部返回空/损坏报告时也必须阻断。"""
    from scripts import performance_benchmark

    monkeypatch.setattr(
        performance_benchmark,
        "run_benchmark",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "config": {"sizes": list(release_checklist.PERFORMANCE_SIZES), "skip_export": False},
            "results": [],
        },
    )

    item = release_checklist._performance_item(
        REPO_ROOT,
        output_dir=tmp_path,
        run_performance=True,
        performance_report=None,
        keep_workspace=False,
    )

    assert item["status"] == "failed"
    assert "规模结果" in item["detail"]


@pytest.mark.parametrize("report", [None, [], "invalid"])
def test_performance_validator_returns_structured_error_for_non_object(report):
    """性能校验器自身也必须是 total function，不能抛 AttributeError。"""
    errors = release_checklist._validate_performance_report(report)
    assert errors
    assert any("对象" in error for error in errors)
