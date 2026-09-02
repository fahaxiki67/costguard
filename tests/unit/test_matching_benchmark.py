"""匹配 benchmark 的闭世界评估测试。

benchmark 只评价显式人工标注的稳定项目项身份，不把名称相似度或算法自身
的输出当作 ground truth。缺少标签必须保持 PENDING，结构不能对齐时保持
INCOMPARABLE，避免用“自动匹配率”掩盖错误合并。
"""

import json

from scripts import matching_benchmark as benchmark


def _truth():
    return {
        "item_universe": ["a", "b", "c", "d", "e"],
        "matching_groups": [["a", "b"], ["c", "d"]],
        "unmatched_items": ["e"],
        "incomparable_items": [],
        "pending_items": [],
    }


def test_exact_benchmark_reports_decimal_metrics_and_level_counts():
    report = benchmark.evaluate_matching(
        [
            {"items": ["a", "b"], "level": "confirmed", "status": "confirmed"},
            {"items": ["c", "d"], "level": "probable", "status": "pending"},
            {"items": ["e"], "level": "pending_data", "status": "pending"},
        ],
        _truth(),
    )

    assert report["status"] == "PASS"
    assert report["metrics"] == {
        "true_positive": 2,
        "false_positive": 0,
        "false_negative": 0,
        "true_negative": 8,
        "precision": "1.0000",
        "recall": "1.0000",
        "f1": "1.0000",
        "false_positive_rate": "0.0000",
    }
    assert report["counts"] == {
        "candidate_group_count": 3,
        "confirmed_group_count": 1,
        "probable_group_count": 1,
        "suspected_group_count": 0,
        "incomparable_group_count": 0,
        "pending_data_group_count": 1,
        "automatic_confirmation_count": 1,
        "manual_review_group_count": 2,
    }
    assert report["false_positive_pairs"] == []
    assert report["false_negative_pairs"] == []


def test_false_positive_and_false_negative_are_reported_separately():
    report = benchmark.evaluate_matching(
        [
            {"items": ["a", "c"], "level": "probable", "status": "pending"},
            {"items": ["b"], "level": "pending_data", "status": "pending"},
            {"items": ["d"], "level": "pending_data", "status": "pending"},
            {"items": ["e"], "level": "pending_data", "status": "pending"},
        ],
        _truth(),
    )

    assert report["status"] == "FAIL"
    assert report["metrics"]["true_positive"] == 0
    assert report["metrics"]["false_positive"] == 1
    assert report["metrics"]["false_negative"] == 2
    assert report["false_positive_pairs"] == [["a", "c"]]
    assert report["false_negative_pairs"] == [["a", "b"], ["c", "d"]]


def test_missing_truth_is_pending_and_never_zero_filled():
    report = benchmark.evaluate_matching(
        [{"items": ["a", "b"], "level": "confirmed", "status": "confirmed"}],
        None,
    )

    assert report["status"] == "PENDING"
    assert report["metrics"] == {
        "true_positive": None,
        "false_positive": None,
        "false_negative": None,
        "true_negative": None,
        "precision": None,
        "recall": None,
        "f1": None,
        "false_positive_rate": None,
    }
    assert report["errors"] == ["缺少人工标注的匹配真值"]


def test_unknown_prediction_item_is_incomparable_not_false_positive():
    report = benchmark.evaluate_matching(
        [{"items": ["a", "outside"], "level": "confirmed", "status": "confirmed"}],
        _truth(),
    )

    assert report["status"] == "INCOMPARABLE"
    assert report["metrics"]["false_positive"] is None
    assert any("outside" in error for error in report["errors"])


def test_truth_partition_must_cover_every_item_once():
    truth = _truth()
    truth["unmatched_items"] = []
    report = benchmark.evaluate_matching([], truth)

    assert report["status"] == "INCOMPARABLE"
    assert any("未被真值分区覆盖" in error for error in report["errors"])


def test_suspected_candidates_are_counted_but_not_scored_as_automatic_links():
    report = benchmark.evaluate_matching(
        [
            {"items": ["a", "b"], "level": "suspected", "status": "pending"},
            {"items": ["c"], "level": "pending_data", "status": "pending"},
            {"items": ["d"], "level": "pending_data", "status": "pending"},
            {"items": ["e"], "level": "pending_data", "status": "pending"},
        ],
        _truth(),
    )

    assert report["status"] == "FAIL"
    assert report["counts"]["suspected_group_count"] == 1
    assert report["counts"]["manual_review_group_count"] == 4
    assert report["metrics"]["true_positive"] == 0
    assert report["metrics"]["false_negative"] == 2


def test_case_suite_preserves_pending_and_fail_statuses_without_green_aggregate():
    report = benchmark.evaluate_cases([
        {"case_id": "pass", "predicted_groups": [], "truth": {
            "item_universe": ["a"],
            "matching_groups": [],
            "unmatched_items": ["a"],
            "incomparable_items": [],
            "pending_items": [],
        }},
        {"case_id": "pending", "predicted_groups": [], "truth": None},
    ])

    assert report["status"] == "passed"  # legacy execution status only
    assert report["overall_comparison_status"] == "PENDING"
    assert report["comparison_status_counts"] == {"PASS": 1, "PENDING": 1}


def test_cli_writes_machine_and_human_reports(tmp_path):
    source = tmp_path / "benchmark.json"
    output = tmp_path / "result.json"
    markdown = tmp_path / "result.md"
    source.write_text(json.dumps({
        "case_id": "pending-case",
        "predicted_groups": [],
        "truth": None,
    }, ensure_ascii=False), encoding="utf-8")

    assert benchmark.main([
        "--input", str(source), "--output", str(output), "--markdown", str(markdown)
    ]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["overall_comparison_status"] == "PENDING"
    assert "PENDING" in markdown.read_text(encoding="utf-8")


def test_stable_item_identity_does_not_use_transient_database_id():
    identity = benchmark.stable_item_identity(
        file_sha256="A" * 64, sheet_name="第1期|对上", row=12
    )

    assert identity == (
        'sha256:' + 'a' * 64 + '|sheet:"第1期|对上"|row:12'
    )


def test_prediction_adapter_requires_every_transient_id_to_have_source_identity():
    groups = [{"item_ids": [1, 2], "level": "probable", "status": "pending"}]
    identities = {1: "item-a", 2: "item-b"}

    assert benchmark.prediction_groups_from_mapping(groups, identities) == [{
        "items": ["item-a", "item-b"],
        "level": "probable",
        "status": "pending",
    }]

    try:
        benchmark.prediction_groups_from_mapping(groups, {1: "item-a"})
    except benchmark.MatchingBenchmarkError as exc:
        assert "item_id=2" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("缺少稳定身份映射时必须失败")
