"""P0-04 黄金回归执行器的安全边界和只读比较测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
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
    assert report["comparison_status_counts"] == {"PASS": 1, "PENDING": 1}
    assert report["overall_comparison_status"] == "PENDING"
    assert report["results"][0]["comparison_status"] == "PASS"
    assert REGISTRY.read_bytes() == before


def test_golden_suite_runs_anonymized_registry_and_reports_real_coverage():
    report = golden_regression.run_golden_regression_suite()
    assert report["status"] == "passed", report
    assert report["registry_count"] == 2
    assert report["real_case_count"] == 0
    assert report["not_available_case_count"] == 2
    assert report["comparison_status_counts"] == {"PASS": 1, "PENDING": 2}
    assert report["overall_comparison_status"] == "PENDING"
    assert any(
        item["registry"].endswith("anonymized_golden_cases/cases.json")
        for item in report["reports"]
    )


def test_unavailable_golden_case_is_explicitly_pending():
    report = golden_regression.run_golden_regression(
        REPO_ROOT / "tests" / "anonymized_golden_cases" / "cases.json"
    )

    result = report["results"][0]
    assert report["status"] == "passed"
    assert result["status"] == "not_available"  # legacy reader compatibility
    assert result["comparison_status"] == "PENDING"
    assert report["comparison_status_counts"] == {"PENDING": 1}
    assert report["overall_comparison_status"] == "PENDING"


def test_cli_require_complete_blocks_pending_summary():
    """CLI 的显式完整门槛不能把旧 status=passed 当成 PASS。"""
    assert golden_regression.main(["--registry", str(REGISTRY), "--require-complete"]) == 3


def test_golden_suite_preserves_malformed_status_as_incomparable(monkeypatch):
    """suite 聚合不能把非法计数吞掉后显示为全 PASS。"""
    fake = {
        "status": "passed",
        "registry": "fake.json",
        "case_count": 1,
        "available_case_count": 1,
        "real_case_count": 1,
        "not_available_case_count": 0,
        "mismatch_case_count": 0,
        "comparison_status_counts": {"PASS": 1, "FAIL": "oops"},
    }
    monkeypatch.setattr(golden_regression, "run_golden_regression", lambda *_args, **_kwargs: fake)

    report = golden_regression.run_golden_regression_suite(
        registries=(REGISTRY, REPO_ROOT / "tests" / "anonymized_golden_cases" / "cases.json")
    )

    assert report["status"] == "failed"
    assert report["overall_comparison_status"] == "INCOMPARABLE"
    assert report["comparison_status_counts"]["INCOMPARABLE"] >= 2
    assert report["comparison_status_errors"]


def test_golden_suite_converts_malformed_registry_to_incomparable(monkeypatch, tmp_path):
    """登记表损坏时发布闸门应结构化失败，而不是裸抛异常。"""

    def fail_registry(*_args, **_kwargs):
        raise golden_regression.GoldenRegistryError("registry malformed")

    broken = tmp_path / "broken.json"
    broken.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(golden_regression, "run_golden_regression", fail_registry)
    report = golden_regression.run_golden_regression_suite(
        registries=(broken,)
    )

    assert report["status"] == "failed"
    assert report["overall_comparison_status"] == "INCOMPARABLE"
    assert report["comparison_status_counts"] == {"INCOMPARABLE": 1}
    assert "registry malformed" in report["comparison_status_errors"][0]


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
    assert report["results"][0]["comparison_status"] == "FAIL"
    assert report["overall_comparison_status"] == "FAIL"
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
                "registry_kind": "synthetic_demo",
                "cases": [
                    {
                        "case_id": "private_case",
                        "case_version": "1",
                        "case_kind": "synthetic_demo",
                        "availability": "available",
                        "inputs": [
                            {
                                "path": "examples/demo/../local_private_data/secret.xlsx",
                                "type": "xlsx",
                                "direction": "upward",
                                "sha256": "0" * 64,
                            }
                        ],
                        "expected": {"file_count": 1},
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


def test_registered_matching_truth_is_evaluated_from_stable_source_identity(tmp_path):
    """黄金案例显式登记 matching_truth 后，执行器使用源身份而非临时行 ID。"""
    from scripts import matching_benchmark

    from jiadun.core.db import migrations

    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    try:
        with conn:
            project_id = conn.execute(
                "INSERT INTO projects(name, schema_version, workspace_path, created_at) "
                "VALUES (?,?,?,?)",
                ("benchmark", 1, str(tmp_path), "2026"),
            ).lastrowid
            file_id = conn.execute(
                "INSERT INTO source_files(project_id, original_path, stored_path, original_name, "
                "sha256, size_bytes, file_type, imported_at) VALUES (?,?,?,?,?,?,?,?)",
                (project_id, "input.xlsx", "input.xlsx", "input.xlsx", "a" * 64, 1, "xlsx", "2026"),
            ).lastrowid
            batch_id = conn.execute(
                "INSERT INTO parse_batches(file_id, parser, parsed_at, status) VALUES (?,?,?,?)",
                (file_id, "test", "2026", "ok"),
            ).lastrowid
            sheet_id = conn.execute(
                "INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name, n_rows, n_cols) "
                "VALUES (?,?,?,?,?)",
                (batch_id, 0, "第1期", 3, 2),
            ).lastrowid
            period_id = conn.execute(
                "INSERT INTO settlement_periods(project_id, period_no, title, source_file_id, direction) "
                "VALUES (?,?,?,?,?)",
                (project_id, 1, "第1期", file_id, "upward"),
            ).lastrowid
            conn.executemany(
                "INSERT INTO line_items(period_id, sheet_id, name, flags_json) VALUES (?,?,?,?)",
                [(period_id, sheet_id, "项目A", '{"row":2}'),
                 (period_id, sheet_id, "项目B", '{"row":3}')],
            )
        identities = [
            matching_benchmark.stable_item_identity(
                file_sha256="a" * 64, sheet_name="第1期", row=row
            )
            for row in (2, 3)
        ]
        report = golden_regression._case_matching_benchmark(
            conn,
            project_id,
            [{"item_ids": [1, 2], "level": "confirmed", "status": "pending"}],
            {
                "matching_truth": {
                    "item_universe": identities,
                    "matching_groups": [identities],
                    "unmatched_items": [],
                    "incomparable_items": [],
                    "pending_items": [],
                }
            },
        )
        assert report is not None
        assert report["status"] == "PASS"
        assert report["metrics"]["precision"] == "1.0000"
    finally:
        conn.close()


def test_compare_metrics_rejects_empty_expected_and_extra_actual_fields():
    """黄金比较采用闭世界口径，不能因 expected 为空或 actual 多字段而静默通过。"""
    empty = golden_regression.compare_metrics({"file_count": 1}, {})
    assert empty and any(diff["kind"] == "contract_or_scope" for diff in empty)

    extra = golden_regression.compare_metrics(
        {"file_count": 1, "crosscheck": {"sufficient_period_count": 0}},
        {"file_count": 1},
    )
    assert any(diff["path"] == "crosscheck.sufficient_period_count" for diff in extra)


def test_available_golden_input_requires_sha256_identity(tmp_path):
    """可执行黄金案例必须锁定输入文件内容，缺少 SHA-256 不能进入回归。"""
    data = {
        "schema_version": 1,
        "registry_kind": "synthetic_demo",
        "cases": [
            {
                "case_id": "missing_hash",
                "case_version": "1",
                "case_kind": "synthetic_demo",
                "availability": "available",
                "inputs": [
                    {"path": "examples/demo/演示-对上结算-第1至3期.xlsx", "type": "xlsx", "direction": "upward"}
                ],
                "expected": {"file_count": 1},
            }
        ],
    }
    registry = tmp_path / "cases.json"
    registry.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(golden_regression.GoldenRegistryError, match="sha256"):
        golden_regression.load_registry(registry)


def test_available_golden_input_rejects_malformed_sha256(tmp_path):
    """SHA-256 必须是严格 64 位十六进制字符串。"""
    data = {
        "schema_version": 1,
        "registry_kind": "synthetic_demo",
        "cases": [
            {
                "case_id": "bad_hash",
                "case_version": "1",
                "case_kind": "synthetic_demo",
                "availability": "available",
                "inputs": [
                    {
                        "path": "examples/demo/演示-对上结算-第1至3期.xlsx",
                        "type": "xlsx",
                        "direction": "upward",
                        "sha256": "not-a-hash",
                    }
                ],
                "expected": {"file_count": 1},
            }
        ],
    }
    registry = tmp_path / "cases.json"
    registry.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(golden_regression.GoldenRegistryError, match="sha256"):
        golden_regression.load_registry(registry)


def test_synthetic_demo_path_cannot_be_counted_as_sanitized_real(tmp_path):
    """仅改 case_kind 不能把 examples/demo 演示资产冒充真实黄金案例。"""
    data = {
        "schema_version": 1,
        "registry_kind": "anonymized_real_project",
        "cases": [
            {
                "case_id": "spoofed_real",
                "case_version": "1",
                "case_kind": "sanitized_real",
                "availability": "available",
                "provenance": {
                    "authorized": True,
                    "anonymized": True,
                    "source_type": "user_authorized_anonymized",
                    "verified_by": "tester",
                    "verified_at": "2026-09-02T00:00:00+08:00",
                    "verification_note": "synthetic spoof test",
                },
                "inputs": [
                    {
                        "path": "examples/demo/演示-对上结算-第1至3期.xlsx",
                        "type": "xlsx",
                        "direction": "upward",
                        "sha256": "0" * 64,
                    }
                ],
                "expected": {"file_count": 1},
            }
        ],
    }
    registry = tmp_path / "cases.json"
    registry.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(golden_regression.GoldenRegistryError, match="assets"):
        golden_regression.load_registry(registry)


def test_golden_asset_path_traversal_is_rejected(tmp_path):
    """真实资产前缀不能通过 ``..`` 穿越到公开演示目录。"""
    data = {
        "schema_version": 1,
        "registry_kind": "anonymized_real_project",
        "cases": [
            {
                "case_id": "traversal",
                "case_version": "1",
                "case_kind": "sanitized_real",
                "availability": "available",
                "provenance": {
                    "authorized": True,
                    "anonymized": True,
                    "source_type": "user_authorized_anonymized",
                    "verified_by": "tester",
                    "verified_at": "2026-09-02T00:00:00+08:00",
                    "verification_note": "traversal test",
                },
                "inputs": [
                    {
                        "path": (
                            "tests/anonymized_golden_cases/assets/../../../examples/demo/"
                            "演示-对上结算-第1至3期.xlsx"
                        ),
                        "type": "xlsx",
                        "direction": "upward",
                        "sha256": "0" * 64,
                    }
                ],
                "expected": {"file_count": 1},
            }
        ],
    }
    registry = tmp_path / "traversal.json"
    registry.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(golden_regression.GoldenRegistryError, match="解析后必须位于"):
        golden_regression.load_registry(registry)


def test_golden_asset_symlink_outside_exact_directory_is_rejected(tmp_path):
    """脱敏资产软链接不能把 assets 外部文件伪装成真实输入。"""
    assets = REPO_ROOT / "tests" / "anonymized_golden_cases" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    link = assets / "_test_external_link.xlsx"
    target = REPO_ROOT / "examples" / "demo" / "演示-对上结算-第1至3期.xlsx"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"当前平台不允许创建符号链接：{exc}")
    try:
        data = {
            "schema_version": 1,
            "registry_kind": "anonymized_real_project",
            "cases": [
                {
                    "case_id": "symlink",
                    "case_version": "1",
                    "case_kind": "sanitized_real",
                    "availability": "available",
                    "provenance": {
                        "authorized": True,
                        "anonymized": True,
                        "source_type": "user_authorized_anonymized",
                        "verified_by": "tester",
                        "verified_at": "2026-09-02T00:00:00+08:00",
                        "verification_note": "symlink test",
                    },
                    "inputs": [
                        {
                            "path": "tests/anonymized_golden_cases/assets/_test_external_link.xlsx",
                            "type": "xlsx",
                            "direction": "upward",
                            "sha256": "0" * 64,
                        }
                    ],
                    "expected": {"file_count": 1},
                }
            ],
        }
        registry = tmp_path / "symlink.json"
        registry.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(golden_regression.GoldenRegistryError, match="解析后必须位于"):
            golden_regression.load_registry(registry)
    finally:
        link.unlink(missing_ok=True)


def test_unavailable_golden_case_requires_reason(tmp_path):
    data = {
        "schema_version": 1,
        "registry_kind": "anonymized_real_project",
        "cases": [
            {"case_id": "missing_reason", "case_version": "1", "availability": "not_available"}
        ],
    }
    registry = tmp_path / "cases.json"
    registry.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(golden_regression.GoldenRegistryError, match="reason"):
        golden_regression.load_registry(registry)
