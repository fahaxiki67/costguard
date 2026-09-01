"""重复表头、前置业务行与多候选表头的 fail-closed 回归。"""

from jiadun.core.parsing.coverage_proof import build_sheet_coverage_proof
from jiadun.core.parsing.header_detect import HeaderDetection, detect_header

HEADERS = ("清单编码", "清单名称", "单位", "工程量", "综合单价", "合价")


def _header(cells: dict[tuple[int, int], str], row: int) -> None:
    for col, value in enumerate(HEADERS, start=1):
        cells[(row, col)] = value


def _detail(
    cells: dict[tuple[int, int], str], row: int, code: str = "A-001"
) -> None:
    values = (code, "混凝土", "m3", "2", "10", "20")
    for col, value in enumerate(values, start=1):
        cells[(row, col)] = value


def test_detect_header_scans_full_sheet_for_duplicate_header_rows():
    cells: dict[tuple[int, int], str] = {}
    _header(cells, 1)
    _detail(cells, 2)
    _header(cells, 3)
    _detail(cells, 4, "A-002")

    detection = detect_header(0, cells, [], max_row=4, max_col=6)

    assert detection is not None
    assert detection.duplicate_header_rows == [3]
    assert "duplicate_header_rows" in detection.risk_flags
    assert detection.needs_review is True
    assert any("重复表头" in note for note in detection.notes)


def test_header_after_preceding_business_row_is_not_allowed_to_auto_pass():
    cells: dict[tuple[int, int], str] = {}
    _detail(cells, 1, "A-000")
    _header(cells, 2)
    _detail(cells, 3)

    detection = detect_header(0, cells, [], max_row=3, max_col=6)

    assert detection is not None
    assert detection.header_row_lo == 2
    assert detection.pre_header_nonempty_rows == [1]
    assert detection.pre_header_suspect_rows == [1]
    assert "pre_header_business_rows" in detection.risk_flags
    assert detection.needs_review is True


def test_multiple_header_candidates_are_exposed_as_unresolved():
    cells: dict[tuple[int, int], str] = {}
    _header(cells, 1)
    _detail(cells, 2)
    _header(cells, 4)
    _detail(cells, 5, "A-002")

    detection = detect_header(0, cells, [], max_row=5, max_col=6)

    assert detection is not None
    assert detection.unresolved_candidate_count >= 2
    assert {tuple(item["header_range"]) for item in detection.candidate_headers} >= {
        (1, 1),
        (4, 4),
    }
    assert "multiple_header_candidates" in detection.risk_flags
    assert detection.needs_review is True


def test_coverage_marks_duplicate_header_pending_even_after_manual_mapping():
    cells: dict[tuple[int, int], str] = {}
    _header(cells, 1)
    _detail(cells, 2)
    _header(cells, 3)
    _detail(cells, 4, "A-002")
    detection = HeaderDetection(
        sheet_index=0,
        header_row_lo=1,
        header_row_hi=1,
        col_map={
            "code": 1,
            "name": 2,
            "unit": 3,
            "quantity": 4,
            "unit_price": 5,
            "amount": 6,
        },
        confidence=1.0,
        needs_review=False,
    )

    proof, rows = build_sheet_coverage_proof(
        cells, detection, max_row=4, data_range=(2, 4)
    )

    duplicate = next(row for row in rows if row.row_number == 3)
    assert duplicate.class_code == "pending_review"
    assert duplicate.reason_code == "duplicate_header_row"
    assert proof.duplicate_header_rows == [3]
    assert "duplicate_header_rows" in proof.risk_flags
    assert proof.proof_status != "complete"


def test_coverage_records_preheader_business_rows_as_unproven():
    cells: dict[tuple[int, int], str] = {}
    _detail(cells, 1, "A-000")
    _header(cells, 2)
    _detail(cells, 3)
    detection = HeaderDetection(
        sheet_index=0,
        header_row_lo=2,
        header_row_hi=2,
        col_map={
            "code": 1,
            "name": 2,
            "unit": 3,
            "quantity": 4,
            "unit_price": 5,
            "amount": 6,
        },
        confidence=1.0,
        needs_review=False,
    )

    proof, _rows = build_sheet_coverage_proof(
        cells, detection, max_row=3, data_range=(3, 3)
    )

    assert proof.pre_header_nonempty_rows == [1]
    assert proof.pre_header_suspect_rows == [1]
    assert "pre_header_business_rows" in proof.risk_flags
    assert proof.proof_status == "unproven"


def test_manual_hidden_confirmation_cannot_cover_duplicate_header_risk():
    """人工确认可见范围不能把重复表头误变成完整证明。"""
    cells: dict[tuple[int, int], str] = {}
    _header(cells, 1)
    _detail(cells, 2)
    _header(cells, 3)
    _detail(cells, 4, "A-002")
    detection = HeaderDetection(
        sheet_index=0,
        header_row_lo=1,
        header_row_hi=1,
        col_map={
            "code": 1,
            "name": 2,
            "unit": 3,
            "quantity": 4,
            "unit_price": 5,
            "amount": 6,
        },
        confidence=1.0,
        needs_review=False,
    )

    proof, _rows = build_sheet_coverage_proof(
        cells,
        detection,
        max_row=4,
        data_range=(2, 4),
        hidden_rows=[2],
        manual_range_confirmed=True,
    )

    assert proof.proof_status != "complete"
    assert "hidden_rows_or_columns_manually_confirmed" in proof.proof_reason
    assert "duplicate_header_rows" in proof.proof_reason


def test_manual_hidden_confirmation_cannot_cover_preheader_business_row():
    """人工确认隐藏范围不能覆盖表头前疑似业务行。"""
    cells: dict[tuple[int, int], str] = {}
    _detail(cells, 1, "A-000")
    _header(cells, 2)
    _detail(cells, 3)
    detection = HeaderDetection(
        sheet_index=0,
        header_row_lo=2,
        header_row_hi=2,
        col_map={
            "code": 1,
            "name": 2,
            "unit": 3,
            "quantity": 4,
            "unit_price": 5,
            "amount": 6,
        },
        confidence=1.0,
        needs_review=False,
    )

    proof, _rows = build_sheet_coverage_proof(
        cells,
        detection,
        max_row=3,
        data_range=(3, 3),
        hidden_rows=[3],
        manual_range_confirmed=True,
    )

    assert proof.proof_status == "unproven"
    assert "pre_header_business_rows" in proof.proof_reason


def test_manual_hidden_confirmation_cannot_cover_unresolved_rows():
    """人工确认隐藏范围不能把解析失败的金额行变成完整证明。"""
    cells: dict[tuple[int, int], str] = {}
    _header(cells, 1)
    for col, value in enumerate(("A-001", "混凝土", "m3", "2", "待补资料"), start=1):
        cells[(2, col)] = value
    detection = HeaderDetection(
        sheet_index=0,
        header_row_lo=1,
        header_row_hi=1,
        col_map={"code": 1, "name": 2, "unit": 3, "quantity": 4,
                 "unit_price": 5, "amount": 6},
        confidence=1.0,
        needs_review=False,
    )

    proof, _rows = build_sheet_coverage_proof(
        cells,
        detection,
        max_row=2,
        data_range=(2, 2),
        hidden_rows=[2],
        manual_range_confirmed=True,
    )

    assert proof.proof_status == "partial"
    assert "unresolved_rows_present" in proof.proof_reason
