"""合成生成器 + 保真层解析测试：解析结果必须与构造意图一致。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "synthetic_test_data"))

from generator import make_clean, make_messy, make_multi_period  # noqa: E402

from jiadun.core.parsing import excel_parser  # noqa: E402


@pytest.fixture(scope="module")
def synth_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("synth")
    return d, make_clean(d / "clean_1期.xlsx"), None


class TestClean:
    def test_clean_parses_all_cells(self, synth_dir):
        d, gt, _ = synth_dir
        r = excel_parser.parse_file(d / gt.file_name, "xlsx")
        assert r.status == "ok"
        assert len(r.sheets) == 1
        assert r.stats["n_cells"] > 40

    def test_text_number_markers(self, synth_dir):
        d, gt, _ = synth_dir
        r = excel_parser.parse_file(d / gt.file_name, "xlsx")
        # clean 表中唯一"数字存文本"是 6 个清单编码格（编码必须保留前导零，属正常业务，
        # 异常检测只对数量/单价/金额列应用文本数字规则）
        assert r.stats["n_text_numbers"] == 6


class TestMessy:
    @pytest.fixture(scope="class")
    def messy(self, tmp_path_factory):
        d = tmp_path_factory.mktemp("messy")
        gts = make_messy(d / "messy.xlsx", seed=7)
        return d, gts

    def test_merges_hidden_and_formulas_captured(self, messy):
        d, gts = messy
        r = excel_parser.parse_file(d / gts[0].file_name, "xlsx")
        assert r.status == "ok"
        sheet1 = next(s for s in r.sheets if s.sheet_name == gts[0].sheet_name)
        assert sheet1.merged_ranges, "merged header cell must be captured"
        assert sheet1.hidden_rows == [11], gtmsg(sheet1.hidden_rows)
        assert sheet1.hidden_cols == [8]
        assert sheet1.n_rows >= 12

    def test_formula_and_error_captured(self, messy):
        d, gts = messy
        r = excel_parser.parse_file(d / gts[0].file_name, "xlsx")
        sheet2 = next(s for s in r.sheets if s.sheet_name == gts[1].sheet_name)
        formulas = [c for c in sheet2.cells if c.is_formula]
        assert len(formulas) >= 2  # 一条 E*F 与一条除零错误公式
        div0 = [c for c in formulas if "/F" in (c.raw_value or "")]
        assert div0

    def test_sheet_names_random_still_found(self, messy):
        d, gts = messy
        r = excel_parser.parse_file(d / gts[0].file_name, "xlsx")
        names = {s.sheet_name for s in r.sheets}
        assert gts[0].sheet_name in names and gts[1].sheet_name in names and "封面" in names

    def test_unsupported_types_reported(self):
        r = excel_parser.parse_file(Path("/nonexistent.pdf"), "pdf")
        assert r.status == "unsupported"
        r = excel_parser.parse_file(Path("/nonexistent.xlsx"), "xlsx")
        assert r.status == "failed"


def gtmsg(v):
    return f"hidden rows mismatch: {v}"


class TestMultiPeriod:
    @pytest.fixture(scope="class")
    def multi(self, tmp_path_factory):
        d = tmp_path_factory.mktemp("multi")
        gts = make_multi_period(d / "multi.xlsx", periods=3)
        return d, gts

    def test_three_sheets(self, multi):
        d, gts = multi
        r = excel_parser.parse_file(d / gts[0].file_name, "xlsx")
        assert len(r.sheets) == 3
        assert [s.sheet_name for s in r.sheets] == ["第1期", "第2期", "第3期"]
