"""复合键完整性分类测试。"""

import math

import pytest

from costguard.core.matching import (
    KeyNormalizationRules,
    classify_composite_keys,
    normalize_composite_key,
)


def _row(
    row_id: int,
    code: object,
    unit: object,
    *,
    file_id: int,
    sheet: str,
    source_row: int,
) -> dict:
    return {
        "id": row_id,
        "code": code,
        "unit": unit,
        "amount": str(row_id),
        "source_file_id": file_id,
        "source_sheet": sheet,
        "source_row": source_row,
        "source_cell": f"A{source_row}:B{source_row}",
    }


def test_trim_empty_and_nan_are_versioned_missing_without_mutating_source_rows():
    rules = KeyNormalizationRules(
        version="composite-key-v2",
        missing_tokens=("待补资料", "N/A"),
        strip_chars=" \t\r\n\u200b\ufeff",
    )
    source = _row(1, "  A01  ", " m2 ", file_id=7, sheet="左表", source_row=9)
    assert normalize_composite_key(source, ("code", "unit"), rules=rules) == ("A01", "m2")
    assert source["code"] == "  A01  "
    assert normalize_composite_key({"code": "待补资料", "unit": "m2"}, ("code", "unit"), rules=rules) is None
    assert normalize_composite_key({"code": " \u200b ", "unit": "m2"}, ("code", "unit"), rules=rules) is None
    assert normalize_composite_key({"code": math.nan, "unit": "m2"}, ("code", "unit"), rules=rules) is None
    assert normalize_composite_key({"code": 0, "unit": False}, ("code", "unit"), rules=rules) == (0, False)


def test_classifies_empty_duplicate_only_and_blocked_rows_without_join_explosion():
    left = [
        _row(1, " A ", "m2", file_id=10, sheet="左Sheet", source_row=4),
        _row(2, "B", "m2", file_id=10, sheet="左Sheet", source_row=5),
        _row(3, "", "m2", file_id=10, sheet="左Sheet", source_row=6),
        _row(4, "C", "m2", file_id=10, sheet="左Sheet", source_row=7),
        _row(5, "C", "m2", file_id=10, sheet="左Sheet", source_row=8),
        _row(6, "D", "m2", file_id=10, sheet="左Sheet", source_row=9),
    ]
    right = [
        _row(11, "A", "m2", file_id=11, sheet="右Sheet", source_row=14),
        _row(12, "B", "m2", file_id=11, sheet="右Sheet", source_row=15),
        _row(13, "B", "m2", file_id=11, sheet="右Sheet", source_row=16),
        _row(14, None, "m2", file_id=11, sheet="右Sheet", source_row=17),
        _row(15, "E", "m2", file_id=11, sheet="右Sheet", source_row=18),
    ]

    result = classify_composite_keys(left, right, ["code", "unit"])

    assert [row["id"] for row in result.matched_left] == [1]
    assert [row["id"] for row in result.matched_right] == [11]
    assert [(left_row["id"], right_row["id"])
            for left_row, right_row in result.matched_pairs] == [(1, 11)]
    assert len(result.matched_pairs) == 1  # B 的 1×2 不得被连接成两行

    assert [row["id"] for row in result.null_left] == [3]
    assert [row["id"] for row in result.null_right] == [14]
    assert [row["id"] for row in result.duplicate_left] == [4, 5]
    assert [row["id"] for row in result.duplicate_right] == [12, 13]
    assert [row["id"] for row in result.left_blocked_by_right_duplicate] == [2]
    assert result.right_blocked_by_left_duplicate == ()
    assert [row["id"] for row in result.left_only_rows] == [6]
    assert [row["id"] for row in result.right_only_rows] == [15]

    assert result.duplicate_key_count_left == 1
    assert result.duplicate_record_count_left == 2
    assert result.duplicate_key_count_right == 1
    assert result.duplicate_record_count_right == 2
    assert result.categories_are_mutually_exclusive
    assert result.summary()["rule_version"] == "composite-key-v1"

    # 分类结果保留来源文件、Sheet、来源行、单元格和数据库 ID，不要求调用方
    # 再通过一次 join 才能回溯来源。
    blocked = result.left_blocked_by_right_duplicate[0]
    assert {
        blocked["id"],
        blocked["source_file_id"],
        blocked["source_sheet"],
        blocked["source_row"],
        blocked["source_cell"],
    } == {2, 10, "左Sheet", 5, "A5:B5"}


def test_duplicate_and_record_counts_are_separate_for_each_side():
    left = [
        _row(1, "A", "m", file_id=1, sheet="L", source_row=1),
        _row(2, "A", "m", file_id=1, sheet="L", source_row=2),
        _row(3, "B", "m", file_id=1, sheet="L", source_row=3),
        _row(4, "B", "m", file_id=1, sheet="L", source_row=4),
        _row(5, "B", "m", file_id=1, sheet="L", source_row=5),
    ]
    right = [_row(6, "A", "m", file_id=2, sheet="R", source_row=6)]
    result = classify_composite_keys(left, right, ["code", "unit"])

    assert result.duplicate_key_counts == {"left": 2, "right": 0}
    assert result.duplicate_record_counts == {"left": 5, "right": 0}
    assert result.duplicate_key_count == 2
    assert result.duplicate_record_count == 5
    # A 在左侧重复，右侧唯一记录应被阻断，不能被误列为 right_only。
    assert [row["id"] for row in result.right_blocked_by_left_duplicate] == [6]
    assert result.right_only_rows == ()
    assert result.matched_pairs == ()


def test_custom_missing_rule_version_and_input_validation():
    left = [{"code": "N/A", "unit": "m2"}]
    right = [{"code": "N/A", "unit": "m2"}]
    rules = KeyNormalizationRules(version="business-key-2026-08", missing_tokens=("N/A",))
    result = classify_composite_keys(left, right, ["code", "unit"], rules=rules)
    assert result.rule_version == "business-key-2026-08"
    assert len(result.null_left) == len(result.null_right) == 1
    assert result.matched_pairs == ()

    with pytest.raises(ValueError, match="至少一个"):
        classify_composite_keys(left, right, [])
    with pytest.raises(KeyError):
        classify_composite_keys([{"code": "A"}], [{"code": "A", "unit": "m2"}], ["code", "unit"])
    with pytest.raises(ValueError, match="不能同时"):
        classify_composite_keys(left, right, ["code", "unit"], rules=rules, rule_version="v3")
