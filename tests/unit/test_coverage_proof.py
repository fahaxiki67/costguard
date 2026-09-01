"""逐 Sheet 覆盖证明与逐行分类测试。"""

from decimal import Decimal

from jiadun.core.parsing.coverage_proof import build_sheet_coverage_proof
from jiadun.core.parsing.header_detect import HeaderDetection


def _fixture():
    cells = {
        (2, 1): "001", (2, 2): "挖土", (2, 3): "2", (2, 4): "10", (2, 5): "20",
        (3, 1): "002", (3, 2): "回填", (3, 3): "1", (3, 4): "10",
        (4, 2): "小计", (4, 5): "30",
        (5, 2): "合计", (5, 5): "30",
        (6, 2): "分部一",
        (7, 2): "备注：此行为说明，不进入累计",
        # 第 8 行故意全空，证明必须仍然覆盖数据区物理行。
        (9, 1): "003", (9, 2): "非法金额", (9, 3): "1", (9, 4): "10", (9, 5): "待补资料",
        (10, 3): "1", (10, 4): "2",
        (11, 1): "004", (11, 2): "模板", (11, 3): "3", (11, 4): "4", (11, 5): "12",
        (12, 3): "0.5", (12, 4): "2",
    }
    det = HeaderDetection(
        sheet_index=0,
        header_row_lo=1,
        header_row_hi=1,
        col_map={"code": 1, "name": 2, "quantity": 3, "unit_price": 4, "amount": 5},
        confidence=1.0,
        needs_review=False,
    )
    return cells, det


def test_sheet_proof_closes_every_physical_row_and_separates_used_rows():
    cells, det = _fixture()
    proof, rows = build_sheet_coverage_proof(cells, det, 12, data_range=(2, 12))

    assert proof.raw_data_row_count == 11
    assert proof.classified_row_count == 11
    assert sum(proof.counts.values()) == proof.raw_data_row_count
    assert proof.counts["detail"] == 3
    assert proof.counts["subtotal"] == 1
    assert proof.counts["grand_total"] == 1
    assert proof.counts["title"] == 1
    assert proof.counts["note"] == 1
    assert proof.counts["blank"] == 1
    assert proof.counts["parse_failed"] == 1
    assert proof.counts["tail_note"] == 1
    assert proof.counts["orphan_numeric"] == 1
    assert proof.business_rows_used == 3
    assert proof.raw_amount_total == str(Decimal("32"))
    assert proof.detail_amount_total == str(Decimal("42"))
    assert proof.proof_status == "partial"
    assert proof.ab_row_set_status == "same_row_set"
    assert proof.ab_independence_level == "shared_extractor"
    assert proof.ab_row_set_hash
    assert {row.class_code for row in rows} >= {
        "detail", "subtotal", "grand_total", "title", "note", "blank",
        "parse_failed", "tail_note", "orphan_numeric",
    }
    invalid = next(row for row in rows if row.class_code == "parse_failed")
    assert invalid.raw_values["amount"] == "待补资料"
    assert invalid.effective_amount is None
    assert not invalid.participates_in_a


def test_hidden_region_is_unproven_even_when_amounts_are_equal():
    cells, det = _fixture()
    proof, _rows = build_sheet_coverage_proof(
        cells, det, 12, data_range=(2, 12), hidden_rows=[10]
    )
    assert proof.proof_status == "unproven"
    assert "hidden_rows_or_columns" in proof.proof_reason
