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
    "code": (
        (
            "清单编码", "项目编码", "清单编号", "项目编号",
            "子目号", "细目号", "子目编码", "细目编码",
            "编码", "编号",
        ),
        ("编码", "编号", "子目号", "细目号"),
    ),
    "name": (
        (
            "清单名称", "项目名称", "清单项目名称", "工程项目名称",
            "工程或费用项目名称", "子目名称", "细目名称", "名称",
        ),
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
SUBTOTAL_WORDS = ("小计", "合计", "总计", "累计", "分部小计", "本章小计", "本页小计", "本页合计")

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


def _merge_width(cells: dict[tuple[int, int], str], merged_ranges: list[str],
                 row: int, col: int) -> int:
    """(row,col) 若是横向合并锚点，返回合并宽度；否则 1。"""
    import re as _re

    from openpyxl.utils import column_index_from_string

    key = (row, col)
    for rng in merged_ranges:
        m = _re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", rng.replace("$", ""))
        if not m:
            continue
        r1 = int(m.group(2))
        c1 = column_index_from_string(m.group(1))
        if (r1, c1) == key:
            c2 = column_index_from_string(m.group(3))
            if int(m.group(2)) == r1:
                return c2 - c1 + 1
    return 1


def detect_header(sheet_index: int, cells: dict[tuple[int, int], str],
                  merged_ranges: list[str], max_row: int, max_col: int) -> HeaderDetection | None:
    """扫描前 N 行识别表头。返回 None = 不是结算清单表。

    在全部候选起点中取最优（无 needs_review 且置信度最高者优先），避免
    标题行/表单行产生的低质候选抢先返回（真实结算书实测缺陷）。
    叶子资格：横向合并宽度 ≥3 的锚点（表标题/组表头）不能作为叶子——
    除非该列在块内没有更深命中。
    """
    anchors = build_anchor_map(merged_ranges)
    best: HeaderDetection | None = None

    def _better(a: HeaderDetection | None, b: HeaderDetection) -> HeaderDetection:
        if a is None:
            return b
        a_ok, b_ok = not a.needs_review, not b.needs_review
        if b_ok != a_ok:
            return b if b_ok else a
        if b.confidence != a.confidence:
            return b if b.confidence > a.confidence else a
        return a

    for lo in range(1, min(_HEADER_ROW_MAX, max_row) + 1):
        # 块式表头（2-3 行）：组表头（"金额（元）"横跨综合单价+合价、"其中"横跨
        # 定额人工费等）在浅层，叶子字段在更深层（国标 表-08/表-12-3 版式）。
        # 每列取最深词典命中为叶子；锚点宽度≥3 的命中不是叶子（标题/组表头），
        # 除非该列块内无更深命中；列间同字段重复仍判歧义。
        for depth in (3, 2):
            hi = lo + depth - 1
            if hi <= min(_HEADER_ROW_MAX, max_row):
                det_n = _try_header(lo, hi, cells, anchors, max_col, sheet_index,
                                    two_row=depth >= 2, block=depth,
                                    merged_ranges=merged_ranges)
                if det_n is not None:
                    best = _better(best, det_n)
        det = _try_header(lo, lo, cells, anchors, max_col, sheet_index,
                          merged_ranges=merged_ranges)
        if det is not None:
            best = _better(best, det)
    return best


def _try_header(lo: int, hi: int, cells, anchors, max_col: int, sheet_index: int,
                two_row: bool = False, block: int = 1,
                merged_ranges: list[str] | None = None) -> HeaderDetection | None:
    merged_ranges = merged_ranges or []
    col_hits: dict[int, list[tuple[str, float]]] = {}
    lo_hit_cols: set[int] = set()
    hi_hit_cols: set[int] = set()
    if block >= 2:
        # 块式：每列取"最深词典命中"为叶子；浅层同列命中视为组表头（不占映射）。
        # 锚点宽度≥3（表标题/组表头横跨多列）不得作为叶子，除非该列无更深命中。
        hit_rows: dict[int, int] = {}
        width_cache: dict[tuple[int, int], int] = {}
        for r in range(lo, hi + 1):
            for c in range(1, max_col + 1):
                text = _cell_text(cells, r, c, anchors)
                hits = _score_header_cell(text)
                if not hits:
                    continue
                if (r, c) not in width_cache:
                    width_cache[(r, c)] = _merge_width(cells, merged_ranges, r, c)
                if (r, c) in anchors and width_cache[(r, c)] >= 3 and hit_rows.get(c, lo - 1) > r:
                    continue  # 宽组命中且该列有更深叶子 → 不占映射
                col_hits[c] = hits
                hit_rows[c] = r
                if r == lo:
                    lo_hit_cols.add(c)
                if r == hi:
                    hi_hit_cols.add(c)
        if not col_hits:
            return None
        # 有效块：最深命中行必须有 ≥2 个命中（块底不能整体空悬），且块顶有命中
        deepest_row = max(hit_rows.values())
        deepest_count = sum(1 for r in hit_rows.values() if r == deepest_row)
        # 块顶必须自身有 ≥2 个命中（块顶是表头行，不是噪声/标题行）
        if len(lo_hit_cols) < 2 or deepest_count < 2:
            return None
        two_row = False  # 块式自带叶子解析，不再走两行覆盖逻辑
    else:
        for c in range(1, max_col + 1):
            text = _cell_text(cells, lo, c, anchors)
            hits = _score_header_cell(text)
            if not hits:
                continue
            if (lo, c) in anchors and _merge_width(cells, merged_ranges, lo, c) >= 3                     and any(_score_header_cell(_cell_text(cells, r2, c, anchors))
                            for r2 in range(lo + 1, min(lo + 3, hi if hi > lo else lo + 3) + 1)):
                continue  # 宽组命中且更深行有命中 → 不占映射
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
    if "amount" not in col_map and "unit_price" not in col_map:
        # 有数量但无任何金额列：无法参与金额校核（如 表-09 综合单价分析表），
        # 自动解析会写入无法对账的半盲明细——必须人工确认角色/映射。
        needs_review = True
        notes.append("no money column (amount/unit_price) — manual confirmation required")
    if block >= 2 and col_hits:
        hit_rows = {}
        for r in range(lo, hi + 1):
            for c in range(1, max_col + 1):
                if _score_header_cell(_cell_text(cells, r, c, anchors)):
                    hit_rows[c] = r
        if hit_rows:
            hi = max(hit_rows.values())
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
    r"[\s0-9一二三四五六七八九十第\.、（）()\-—:：,，ⅠⅡⅢ]+|分部分项|部分|[章节类段]"
)


def _norm_ws(text: str) -> str:
    """归一化全部空白（含全角空格/制表/换行）——真实表小计标签常有排版空格。"""
    return "".join(text.split()).replace("\u3000", "")


def is_subtotal_row(name_text: str, first_cell_text: str) -> bool:
    """名称或首列出现小计/合计词 → 汇总行标记。

    规则（保守，防误伤"钢筋合计用量表"这类名称）：
    - 文本等于小计词；或
    - 以小计词开头且余下全是编号/标点/空白（"合计：100"）；或
    - 以小计词结尾且前缀全是编号/章节/标点（"一、二部分 小计"）。
    中间夹字（"合计用量表"）不算。
    """
    for text in (name_text, first_cell_text):
        t = _norm_ws(text)
        if not t:
            continue
        for w in SUBTOTAL_WORDS:
            if t == w:
                return True
            if t.startswith(w) and not _SUBTOTAL_TRIM.sub("", t[len(w):]):
                return True
            if t.endswith(w) and not _SUBTOTAL_TRIM.sub("", t[: -len(w)]):
                return True
    return False


def detect_form_like(cells: dict[tuple[int, int], str],
                     merged_ranges: list[str]) -> str | None:
    """检测键值对表单结构（如支付审批单）。

    返回 'strong'（键值行占多数——可无视表头词典误命中直接路由）、
    'weak'（合并密集或少量键值行——仅在完全无表头识别时路由）、None。
    普通结算表标题区虽含冒号，但占比低、数据行多，不会命中 strong。
    """
    if not cells:
        return None
    max_row = max(r for r, _c in cells)
    if max_row > 30:
        return None
    row_texts: dict[int, list[str]] = {}
    for (r, _c), text in cells.items():
        t = (text or "").strip()
        if t:
            row_texts.setdefault(r, []).append(t)
    data_rows = len(row_texts)
    kv_rows = sum(
        1 for texts in row_texts.values()
        if ("：" in "".join(texts) or ":" in "".join(texts))
        and not any(w in "".join(texts) for w in ("清单编码", "清单名称", "合价", "综合单价"))
    )
    if kv_rows >= 3 and kv_rows * 2 >= data_rows:
        return "strong"
    # 存在"金额型表头行"（≥2 个词典命中且含金额/单价字段）→ 是真表格，
    # weak 表单不得抢在表头/语义门控之前（国标 E.6 汇总表实测误判场景）。
    for r in range(1, min(15, max_row) + 1):
        row_text_cells = [t for (_r, c), t in cells.items() if _r == r and (t or "").strip()]
        hits: list[str] = []
        for t in row_text_cells:
            hits.extend(f for f, _s in _score_header_cell(t))
        if len(hits) >= 2 and any(f in ("amount", "unit_price") for f in hits):
            return None
    if data_rows and kv_rows >= 2:
        return "weak"
    if len(merged_ranges) >= 3 and data_rows <= 20:
        return "weak"
    return None
