"""表头识别引擎（ADR-008 第二段：语义层）。

在保真层网格之上识别结算清单表：
1. 字段词典打分定位表头行（支持多行表头、表头不在首行）；
2. 生成 列映射 col_map（field → 列号），带 confidence 与 needs_review；
3. 每个字段值都能回溯到原始单元格。

匹配纪律：
- 词典命中按"包含/相等"分级计分，不做模糊联想（宁可 needs_review，不可错认）；
- 同名字段（如两列都叫"单价"）取分高者，并记录歧义到 stats，由复核决定。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 字段词典：field → (精确别名, 包含词)。顺序即优先级。
FIELD_DICT: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "code": (("清单编码", "项目编码", "清单编号", "项目编号", "编码", "编号"), ("编码", "编号")),
    "name": (
        ("清单名称", "项目名称", "清单项目名称", "工程项目名称", "工程或费用项目名称", "名称"),
        ("名称",),
    ),
    "feature": (("项目特征", "项目特征描述", "特征描述", "工程内容"), ("特征", "工程内容")),
    "unit": (("计量单位", "单位"), ("单位",)),
    "quantity": (("工程量", "数量", "本期工程量", "结算工程量", "工程数量"), ("数量", "工程量")),
    "unit_price": (
        ("综合单价", "单价", "含税单价", "不含税单价", "投标单价", "合同单价", "审定单价"),
        ("单价",),
    ),
    "amount": (
        ("合价", "综合合价", "金额", "含税合价", "不含税合价", "合价(元)", "合价（元）"),
        ("合价", "金额"),
    ),
    "tax_rate": (("税率", "增值税率", "税率%"), ("税率",)),
}

# 名称列中出现的"小计/合计"标记词（不计入明细，也不能当作清单名称）
SUBTOTAL_WORDS = ("小计", "合计", "总计", "累计", "分部小计", "本章小计")

_HEADER_ROW_MAX = 15  # 表头识别扫描的最大行号


@dataclass
class HeaderDetection:
    sheet_index: int
    header_row_lo: int  # 1-based，表头首行
    header_row_hi: int  # 1-based，表头末行（单行表头时 = lo）
    col_map: dict[str, int]  # field → 1-based 列号
    confidence: float
    needs_review: bool
    notes: list[str] = field(default_factory=list)


def _cell_text(sheet_cells: dict[tuple[int, int], str], row: int, col: int,
               anchors: dict[tuple[int, int], tuple[int, int]]) -> str:
    """取单元格文本，合并单元格取锚点值。"""
    key = (row, col)
    if key in anchors:
        key = anchors[key]
    return sheet_cells.get(key, "").strip()


def build_anchor_map(merged_ranges: list[str]) -> dict[tuple[int, int], tuple[int, int]]:
    """'B2:C4' → {(2,3):(2,2),(2,4):(2,2),...}：被覆盖格 → 左上锚点格。(1-based)"""
    import re

    anchors: dict[tuple[int, int], tuple[int, int]] = {}
    for rng in merged_ranges:
        m = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", rng.replace("$", ""))
        if not m:
            continue
        from openpyxl.utils import column_index_from_string

        c1, r1, c2, r2 = (
            column_index_from_string(m.group(1)), int(m.group(2)),
            column_index_from_string(m.group(3)), int(m.group(4)),
        )
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                if (r, c) != (r1, c1):
                    anchors[(r, c)] = (r1, c1)
    return anchors


def _score_header_cell(text: str) -> list[tuple[str, float]]:
    """单个候选表头单元格 → [(field, score)]。score: 相等 1.0，包含 0.7。"""
    t = text.strip().replace("\n", "").replace(" ", "")
    if not t or len(t) > 30:
        return []
    hits = []
    for fld, (exact, contains) in FIELD_DICT.items():
        if t in exact:
            hits.append((fld, 1.0))
        elif any(w in t for w in contains):
            hits.append((fld, 0.7))
    return hits


def detect_header(sheet_index: int, cells: dict[tuple[int, int], str],
                  merged_ranges: list[str], max_row: int, max_col: int) -> HeaderDetection | None:
    """扫描前 N 行识别表头。返回 None = 不是结算清单表。"""
    anchors = build_anchor_map(merged_ranges)
    best: HeaderDetection | None = None

    for lo in range(1, min(_HEADER_ROW_MAX, max_row) + 1):
        # 两行表头优先判定：第一行为组表头（如"金额"跨列），第二行为具体字段。
        # 若 lo 与 lo+1 都有词典命中，必须按两行处理，否则数据行会从组表头下一行
        # 误抽（"合价"行被当作数据）。
        if lo + 1 <= min(_HEADER_ROW_MAX, max_row):
            det2 = _try_header(lo, lo + 1, cells, anchors, max_col, sheet_index, two_row=True)
            if det2:
                return det2
        # 单行表头候选
        det = _try_header(lo, lo, cells, anchors, max_col, sheet_index)
        if det:
            return det
    return best


def _try_header(lo: int, hi: int, cells, anchors, max_col: int, sheet_index: int,
                two_row: bool = False) -> HeaderDetection | None:
    col_hits: dict[int, list[tuple[str, float]]] = {}
    lo_hit_cols: set[int] = set()
    hi_hit_cols: set[int] = set()
    for c in range(1, max_col + 1):
        text = _cell_text(cells, lo, c, anchors)
        hits = _score_header_cell(text)
        if hits:
            col_hits[c] = hits
            lo_hit_cols.add(c)
        if two_row:
            text2 = _cell_text(cells, hi, c, anchors)
            hits2 = _score_header_cell(text2)
            if hits2:
                col_hits[c] = hits2
                hi_hit_cols.add(c)
    total_score = 0.0
    for hits in sorted(col_hits.values()):
        if not hits:
            continue
        total_score += max(s for _, s in hits)
    if len(col_hits) < 3:  # 至少要识别出 3 个字段才承认是清单表
        return None
    if two_row and not (hi_hit_cols and hi_hit_cols & lo_hit_cols):
        # 第二行自身无命中、或与第一行命中列无重叠 → 不是两行表头
        return None
    col_map: dict[str, int] = {}
    notes: list[str] = []
    ambiguous = 0
    for c, hits in sorted(col_hits.items()):
        if not hits:
            continue
        fld, score = hits[0]
        if fld in col_map:
            ambiguous += 1
            notes.append(f"ambiguous field '{fld}' at col {col_map[fld]} and {c}")
            continue
        col_map[fld] = c
    required = ["name"]
    missing_required = [f for f in required if f not in col_map]
    if missing_required:
        return None
    strong = sum(1 for c, hits in col_hits.items() if max(s for _, s in hits) >= 1.0)
    confidence = min(1.0, 0.25 * len(col_map) + 0.08 * strong - 0.1 * ambiguous)
    needs_review = bool(missing_required or ambiguous or confidence < 0.75)
    if "quantity" not in col_map and "amount" not in col_map:
        confidence = min(confidence, 0.5)
        needs_review = True
        notes.append("neither quantity nor amount column found")
    return HeaderDetection(
        sheet_index=sheet_index,
        header_row_lo=lo,
        header_row_hi=hi,
        col_map=col_map,
        confidence=round(confidence, 2),
        needs_review=needs_review,
        notes=notes,
    )


def data_rows_range(cells: dict[tuple[int, int], str], det: HeaderDetection,
                    max_row: int) -> tuple[int, int]:
    """数据行区间：表头下一行 → 最后一个非空行。"""
    anchors = build_anchor_map([])  # 数据行不需要锚点回填
    start = det.header_row_hi + 1
    end = start - 1
    for r in range(start, max_row + 1):
        row_has_data = any(_cell_text(cells, r, c, anchors) for c in range(1, max_col_of(cells) + 1))
        if row_has_data:
            end = r
    return (start, end) if end >= start else (start, start - 1)


def max_col_of(cells: dict[tuple[int, int], str]) -> int:
    return max((c for (_, c) in cells), default=0)


_SUBTOTAL_TRIM = re.compile(
    r"[\s0-9一二三四五六七八九十\.、（）()\-—:：,，ⅠⅡⅢ]+|部分|章|节|类|段"
)


def is_subtotal_row(name_text: str, first_cell_text: str) -> bool:
    """名称或首列出现小计/合计词 → 汇总行标记。

    规则（保守，防误伤"钢筋合计用量表"这类名称）：
    - 文本等于小计词；或
    - 以小计词开头且余下全是编号/标点/空白（"合计：100"）；或
    - 以小计词结尾且前缀全是编号/章节/标点（"一、二部分 小计"）。
    中间夹字（"合计用量表"）不算。
    """
    for text in (name_text.strip(), first_cell_text.strip()):
        if not text:
            continue
        for w in SUBTOTAL_WORDS:
            if text == w:
                return True
            if text.startswith(w) and not _SUBTOTAL_TRIM.sub("", text[len(w):]):
                return True
            if text.endswith(w) and not _SUBTOTAL_TRIM.sub("", text[: -len(w)]):
                return True
    return False
