"""合成性能基准的唯一运行目录、运行包和取消语义测试。"""

from pathlib import Path

from scripts import performance_benchmark as benchmark

from jiadun.core.acceptance import GOLDEN_VECTOR_SHA256, canonical_bundle_hash


def test_benchmark_writes_unique_run_bundle_without_overwrite(tmp_path: Path):
    output = tmp_path / "benchmark"
    first = benchmark.run_benchmark([2], output=output, skip_export=True)
    second = benchmark.run_benchmark([2], output=output, skip_export=True)

    assert first["status"] == second["status"] == "completed"
    first_json = Path(first["output_paths"]["json"])
    second_json = Path(second["output_paths"]["json"])
    assert first_json != second_json
    assert first_json.is_file() and second_json.is_file()
    assert first["acceptance_bundle"]["integrity"]["bundle_sha256"] == canonical_bundle_hash(
        first["acceptance_bundle"]
    )
    assert first["acceptance_bundle"]["runtime"]["product_id"] == "jiadun"
    assert first["acceptance_bundle"]["runtime"]["product_name"] == "价盾"
    assert first["acceptance_bundle"]["runtime"]["schema_version"] >= 10
    assert first["acceptance_bundle"]["truth"]["metrics"]["precision"] is None
    golden = first["acceptance_bundle"]["golden_vector"]
    assert golden["expected_sha256"] == GOLDEN_VECTOR_SHA256
    assert golden["matches_expected"] is True
    assert first["results"][0]["run_contract_signature"]


def test_benchmark_cancel_preserves_exact_worksite(tmp_path: Path):
    report = benchmark.run_benchmark(
        [100], output=tmp_path / "cancelled", skip_export=True, cancel_check=lambda: True
    )
    assert report["status"] == "cancelled"
    assert report["results"][0]["status"] == "cancelled"
    assert Path(report["workspace"]).is_dir()
    assert report["acceptance_bundle"]["truth"]["status"] == "not_available"
