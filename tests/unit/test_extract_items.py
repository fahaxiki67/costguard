"""行抽取器回归测试（issue #3：合计/总计/尾注行收编导致累计失真）。

三个根因各配锁定用例：
1. "分部分项合计" 曾因 _SUBTOTAL_TRIM 交替分支拆散"分部分项"而漏判；
2. 支表二"总计"写在序号列（编码列从 2 开始），旧逻辑只看首个映射列；
3. 明细区末尾的尾注/比例孤儿数值行曾被持久化，大额计算值流入累计。
"""
from __future__ import annotations

from jiadun.core.parsing.extract_items import extract_items
from jiadun.core.parsing.header_detect import HeaderDetection, is_subtotal_row

DET = HeaderDetection(
    sheet_index=0,
    header_row_lo=1,
    header_row_hi=1,
    col_map={"code": 2, "name": 3, "quantity": 4, "amount": 5},
    confidence=1.0,
    needs_review=False,
)


def _cells(rows: dict[int, dict[int, str]]) -> dict[tuple[int, int], str]:
    return {(r, c): t for r, cols in rows.items() for c, t in cols.items()}


class TestSubtotalLabelPrefix:
    def test_fenbufenxiang_heji(self):
        assert is_subtotal_row("分部分项合计", "")
        assert is_subtotal_row("第二部分合计", "")
        assert is_subtotal_row("一、分部分项小计", "")

    def test_conservative_negatives_hold(self):
        assert is_subtotal_row("小计", "")
        assert is_subtotal_row("一、二部分 小计", "A.1")
        assert not is_subtotal_row("钢筋合计用量表", "")


class TestLeadingColumnLabel:
    def test_zongji_in_index_column(self):
        cells = _cells({
            2: {2: "010101001", 3: "平整场地", 4: "100", 5: "1000"},
            3: {1: "小计", 5: "1000"},
            4: {1: "总计", 5: "2000"},
        })
        items = extract_items(cells, [], DET, max_row=4, data_range=(2, 4))
        assert len(items) == 3
        assert [i.flags["subtotal"] for i in items] == [False, True, True]

    def test_no_false_positive_from_lead_text(self):
        cells = _cells({2: {1: "备注：详见小计说明", 2: "010101001",
                            3: "平整场地", 4: "100", 5: "1000"}})
        items = extract_items(cells, [], DET, max_row=2, data_range=(2, 2))
        assert len(items) == 1
        assert not items[0].flags["subtotal"]


class TestTailNoteRows:
    def test_trailing_orphan_rows_dropped(self):
        cells = _cells({
            2: {2: "010101001", 3: "平整场地", 4: "100", 5: "1000"},
            3: {1: "总计", 5: "1000"},
            4: {5: "0.21"},            # 尾注：安全生产措施费比例
            5: {5: "17698492.38"},     # 尾注：比例计算金额
        })
        items = extract_items(cells, [], DET, max_row=5, data_range=(2, 5))
        assert [i.row for i in items] == [2, 3]
        assert all(not i.flags.get("orphan_numeric_row") for i in items)

    def test_midsheet_orphan_row_kept(self):
        cells = _cells({
            2: {2: "010101001", 3: "平整场地", 4: "100", 5: "1000"},
            3: {5: "123.45"},  # 表中间的孤儿数值行：保留待人工复核
            4: {2: "010401001", 3: "砖基础", 4: "10", 5: "4200"},
        })
        items = extract_items(cells, [], DET, max_row=4, data_range=(2, 4))
        assert [i.row for i in items] == [2, 3, 4]
        assert items[1].flags.get("orphan_numeric_row") is True
