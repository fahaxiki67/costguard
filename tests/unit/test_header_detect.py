"""表头识别引擎测试：表头位置、多行表头、非清单表、歧义列。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "synthetic_test_data"))

from generator import make_clean, make_messy  # noqa: E402

from costguard.core.parsing.excel_parser import parse_file  # noqa: E402
from costguard.core.parsing.header_detect import (  # noqa: E402
    detect_header,
    is_subtotal_row,
)


def _grid_and_detect(sheet):
    cells = {(c.row, c.col): (c.cached_value if c.is_formula and c.cached_value else (c.raw_value or "")) for c in sheet.cells}
    return detect_header(sheet.sheet_index, cells, sheet.merged_ranges, sheet.n_rows, sheet.n_cols)


class TestDetect:
    def test_clean_header_row1(self, tmp_path):
        make_clean(tmp_path / "c.xlsx")
        sheet = parse_file(tmp_path / "c.xlsx", "xlsx").sheets[0]
        det = _grid_and_detect(sheet)
        assert det is not None
        assert det.header_row_lo == 1
        assert det.col_map["code"] == 1
        assert det.col_map["name"] == 2
        assert det.col_map["quantity"] == 5
        assert det.col_map["amount"] == 7
        assert det.confidence >= 0.75
        assert not det.needs_review

    def test_header_not_at_row1(self, tmp_path):
        d = tmp_path
        gts = make_messy(d / "m.xlsx", seed=7)
        result = parse_file(d / "m.xlsx", "xlsx")
        by_name = {s.sheet_name: s for s in result.sheets}
        det1 = _grid_and_detect(by_name[gts[0].sheet_name])
        assert det1 is not None
        assert det1.header_row_lo == 4, f"expected header at row 4, got {det1.header_row_lo}"
        assert det1.confidence >= 0.6

    def test_two_row_header(self, tmp_path):
        d = tmp_path
        gts = make_messy(d / "m2.xlsx", seed=7)
        result = parse_file(d / "m2.xlsx", "xlsx")
        by_name = {s.sheet_name: s for s in result.sheets}
        det2 = _grid_and_detect(by_name[gts[1].sheet_name])
        assert det2 is not None
        assert det2.header_row_hi == det2.header_row_lo + 1
        assert det2.col_map.get("amount") == 7

    def test_cover_sheet_rejected(self, tmp_path):
        d = tmp_path
        make_messy(d / "m3.xlsx", seed=7)
        result = parse_file(d / "m3.xlsx", "xlsx")
        cover = next(s for s in result.sheets if s.sheet_name == "封面")
        assert _grid_and_detect(cover) is None


class TestSubtotal:
    def test_positive(self):
        assert is_subtotal_row("小计", "")
        assert is_subtotal_row("合计", "")
        assert is_subtotal_row("一、二部分 小计", "A.1")

    def test_negative_not_confused(self):
        assert not is_subtotal_row("钢筋合计用量表", "")
        assert not is_subtotal_row("C25混凝土垫层", "010501001001")
        assert not is_subtotal_row("", "平整场地")
