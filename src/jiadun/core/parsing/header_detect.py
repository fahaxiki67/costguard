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
# 合计级（全表控制值）vs 页级/分部级：C 路径控制值只认合计级——真实分页表
# "本页小计"与"合计"并存，全部求和会把控制值翻倍（v0.1.5 已知限制）。
GRAND_TOTAL_WORDS = ("合计", "总计", "累计")


def is_grand_total_row(name_text: str, first_cell_text: str) -> bool:
    """合计级行（全表控制值）。判定规则与 is_subtotal_row 相同，词表不同。"""
    for text in (name_text, first_cell_text):
        t = _norm_ws(text)
        if not t:
            continue
        for w in GRAND_TOTAL_WORDS:
            if t == w:
                return True
            if t.startswith(w) and not _SUBTOTAL_TRIM.sub("", t[len(w):]):
                return True
            if t.endswith(w) and not _SUBTOTAL_TRIM.sub("", t[: -len(w)]):
                return True
    return False

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
    # 这些字段是解析门控元数据，不参与业务金额计算。它们必须随检测结果
    # 一起向上层暴露，避免“选到了一个看似合理的表头”就丢失范围风险。
    candidate_headers: list[dict[str, object]] = field(default_factory=list)
    unresolved_candidate_count: int = 0
    duplicate_header_rows: list[int] = field(default_factory=list)
    pre_header_nonempty_rows: list[int] = field(default_factory=list)
    pre_header_suspect_rows: list[int] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)


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


def _raw_nonempty_texts(
    cells: dict[tuple[int, int], str], row: int, max_col: int
) -> list[str]:
    """取一行实际存储的非空值，不把合并锚点复制成多个值。"""
    values: list[str] = []
    for col in range(1, max_col + 1):
        value = str(cells.get((row, col), "") or "").strip()
        if value:
            values.append(value)
    return values


_NUMERIC_CELL_RE = re.compile(
    r"^[+-]?(?:\d[\d,，\s]*)(?:[.．]\d+)?%?$"
)
_CODE_LIKE_RE = re.compile(r"^[A-Za-z]?[A-Za-z0-9][A-Za-z0-9._/\-]*$")


def _looks_numeric_cell(text: str) -> bool:
    compact = (
        text.strip()
        .replace("¥", "")
        .replace("￥", "")
        .replace("(", "")
        .replace(")", "")
        .replace("（", "")
        .replace("）", "")
    )
    return bool(compact and _NUMERIC_CELL_RE.fullmatch(compact))


def _looks_code_like(text: str) -> bool:
    compact = "".join(text.split())
    return bool(
        compact
        and _CODE_LIKE_RE.fullmatch(compact)
        and any(char.isdigit() for char in compact)
    )


def _row_header_fields(
    cells: dict[tuple[int, int], str],
    row: int,
    max_col: int,
    anchors: dict[tuple[int, int], tuple[int, int]],
    expected_map: dict[str, int] | None = None,
) -> tuple[set[str], set[str]]:
    """返回该行命中的字段及按既定列位置命中的字段。"""
    fields: set[str] = set()
    mapped_fields: set[str] = set()
    expected_by_col = {col: field for field, col in (expected_map or {}).items()}
    for col in range(1, max_col + 1):
        text = _cell_text(cells, row, col, anchors)
        hits = _score_header_cell(text)
        for field_name, _score in hits:
            fields.add(field_name)
            if expected_by_col.get(col) == field_name:
                mapped_fields.add(field_name)
    return fields, mapped_fields


def detect_duplicate_header_rows(
    cells: dict[tuple[int, int], str],
    det: HeaderDetection,
    max_row: int,
    max_col: int | None = None,
    merged_ranges: list[str] | None = None,
) -> list[int]:
    """在完整 Sheet 网格中找出选定表头之外的重复表头行。

    只要一行同时出现“编码+名称”或“名称+数量/单价/金额”等稳定字段组合，
    就作为待复核候选。这里宁可多报风险，也不把跨页重复表头当成业务明细。
    """
    width = max_col if max_col is not None else max_col_of(cells)
    if width <= 0 or max_row <= 0:
        return []
    anchors = build_anchor_map(merged_ranges or [])
    # 只豁免表头首行。多行表头的后续行通常只有少量叶子字段，不会满足
    # 下方的完整表头组合；若后续行再次出现完整“编码+名称+金额”等字段，
    # 即使它落在检测器误扩大的 header_range 内，也必须报为重复表头。
    # 这一步同时防止检测器把“首表头 + 明细 + 重复表头”误选成一个三行
    # 表头，从而把首段明细静默排除在数据区之外。
    selected_rows = set(range(det.header_row_lo, det.header_row_hi + 1))
    duplicated: list[int] = []
    for row in range(1, max_row + 1):
        if not _raw_nonempty_texts(cells, row, width):
            continue
        fields, mapped_fields = _row_header_fields(
            cells, row, width, anchors, det.col_map
        )
        has_name_pair = "name" in fields and bool(
            fields & {"code", "quantity", "unit_price", "amount", "unit"}
        )
        is_duplicate = (len(mapped_fields) >= 2 and has_name_pair) or (
            "name" in fields and len(fields) >= 3
        )
        # 选定表头范围中的叶子行可能命中 2 个词典字段，但只有“完整
        # 稳定组合”才算重复；因此不会把常见的两行块式表头叶子行误报。
        # 范围之外的行仍按较低阈值识别，以便捕获只重复“编码+名称”的
        # 跨页表头。范围内只有同时命中至少 3 个字段的行才升级为重复，
        # 避免合法的多行表头叶子被当成漏行风险。
        if row in selected_rows and row != det.header_row_lo and len(fields) < 3:
            continue
        if is_duplicate and (row not in selected_rows or row > det.header_row_lo):
            duplicated.append(row)
    return duplicated


def detect_pre_header_rows(
    cells: dict[tuple[int, int], str],
    det: HeaderDetection,
    max_col: int | None = None,
) -> tuple[list[int], list[int]]:
    """记录选定表头前所有非空行及其中疑似业务数据行。"""
    width = max_col if max_col is not None else max_col_of(cells)
    if width <= 0 or det.header_row_lo <= 1:
        return [], []
    nonempty: list[int] = []
    suspect: list[int] = []
    for row in range(1, det.header_row_lo):
        values = _raw_nonempty_texts(cells, row, width)
        if not values:
            continue
        nonempty.append(row)
        numeric_or_code = any(
            _looks_numeric_cell(value) or _looks_code_like(value) for value in values
        )
        # 单个合并标题/说明行允许存在；多值行或含数字/编码的行必须
        # 暂停自动解析，防止真实明细位于误选表头之前而被静默漏掉。
        if (
            len(values) >= 3
            or (len(values) >= 2 and numeric_or_code)
            or (len(values) == 1 and numeric_or_code)
        ):
            suspect.append(row)
    return nonempty, suspect


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
    candidates: list[HeaderDetection] = []

    def _better(a: HeaderDetection | None, b: HeaderDetection) -> HeaderDetection:
        if a is None:
            return b
        a_ok, b_ok = not a.needs_review, not b.needs_review
        if b_ok != a_ok:
            return b if b_ok else a
        if b.confidence != a.confidence:
            return b if b.confidence > a.confidence else a
        # 同一列映射、同等置信度时，优先选择更窄的表头范围。真实表格常在
        # 正式字段行前放置合并标题/编制说明；把这些说明行吸进多行表头会
        # 让正式字段行被误报为“重复表头”，并从数据区排除首段明细。若确
        # 有真正的多行表头，浅层候选通常无法得到同样完整的列映射，仍会
        # 由置信度/字段覆盖规则保留多行候选。
        a_span = a.header_row_hi - a.header_row_lo
        b_span = b.header_row_hi - b.header_row_lo
        if b_span != a_span:
            return b if b_span < a_span else a
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
                    candidates.append(det_n)
                    best = _better(best, det_n)
        det = _try_header(lo, lo, cells, anchors, max_col, sheet_index,
                          merged_ranges=merged_ranges)
        if det is not None:
            candidates.append(det)
            best = _better(best, det)
    if best is None:
        return None

    # 同一真实表头会因单行/两行/三行尝试产生多个内部候选。候选中常会有
    # 一个“浅层表头”因同名列而被标为 needs_review，随后更深的块式表头
    # 才能确定叶子列。不能把这些已经被确定性排序淘汰的低质候选重新算作
    # “多个未解决候选”，否则正常三层表头会被无谓地挡在角色审阅门外。
    # 只有同样通过自身歧义检查的候选才有资格参与多候选闸门；若没有任何
    # 通过候选，则保留原有 needs_review 结果，避免放宽真正的歧义输入。
    unique_candidates: dict[tuple[int, int, tuple[tuple[str, int], ...]], HeaderDetection] = {}
    candidate_pool = [candidate for candidate in candidates if not candidate.needs_review]
    if not candidate_pool:
        candidate_pool = candidates
    for candidate in candidate_pool:
        key = (
            candidate.header_row_lo,
            candidate.header_row_hi,
            tuple(sorted(candidate.col_map.items())),
        )
        previous = unique_candidates.get(key)
        if previous is None or (
            (not candidate.needs_review, candidate.confidence)
            > (not previous.needs_review, previous.confidence)
        ):
            unique_candidates[key] = candidate
    # 同一列映射下，较短且被较长候选完全覆盖的范围只是多行表头的浅层
    # 变体，不构成真实的“多个未解决候选”。保留最长候选后，跨页的两个
    # 独立起点（例如 1-1 与 4-4）仍会触发闸门。
    dominated: set[tuple[int, int, tuple[tuple[str, int], ...]]] = set()
    for key, candidate in unique_candidates.items():
        for other_key, other in unique_candidates.items():
            if key == other_key or candidate.col_map != other.col_map:
                continue
            if (
                other.header_row_lo <= candidate.header_row_lo
                and other.header_row_hi >= candidate.header_row_hi
                and (
                    other.header_row_lo < candidate.header_row_lo
                    or other.header_row_hi > candidate.header_row_hi
                )
            ):
                dominated.add(key)
                break
    unique_candidates = {
        key: candidate for key, candidate in unique_candidates.items()
        if key not in dominated
    }
    # 在候选排序后再做完整 Sheet 重复表头扫描。若“最佳”候选把一行真实
    # 重复表头误吸进多行表头范围，优先回退到同一列映射、且在重复行之前
    # 结束的候选（通常是单行表头）。否则数据区会从重复表头之后才开始，
    # 首段明细将被静默漏掉。
    duplicate_probe = detect_duplicate_header_rows(
        cells, best, max_row, max_col, merged_ranges
    )
    duplicate_inside = [
        row for row in duplicate_probe
        if best.header_row_lo < row <= best.header_row_hi
    ]
    if duplicate_inside:
        cutoff = min(duplicate_inside)
        alternatives = [
            candidate for candidate in candidates
            if (
                candidate.header_row_lo == best.header_row_lo
                and candidate.header_row_hi < cutoff
                and candidate.col_map == best.col_map
            )
        ]
        if alternatives:
            best = max(
                alternatives,
                key=lambda candidate: (candidate.header_row_hi, candidate.confidence),
            )
            # 重新计算探测结果，确保新范围之外的重复表头仍被记录。
            duplicate_probe = detect_duplicate_header_rows(
                cells, best, max_row, max_col, merged_ranges
            )

    candidate_headers = [
        {
            "header_range": [candidate.header_row_lo, candidate.header_row_hi],
            "col_map": dict(candidate.col_map),
            "confidence": candidate.confidence,
            "needs_review": candidate.needs_review,
            "notes": list(candidate.notes),
        }
        for candidate in sorted(
            unique_candidates.values(),
            key=lambda item: (item.header_row_lo, item.header_row_hi, item.confidence),
        )
    ]
    # 回退到重复表头之前的较窄候选后，``best`` 可能不再属于已裁剪的
    # ``unique_candidates``。候选清单必须包含最终实际使用的范围，否则
    # Evidence 会出现“记录了候选但没有记录选中的表头”的不可审计状态。
    if not any(
        item["header_range"] == [best.header_row_lo, best.header_row_hi]
        and item["col_map"] == best.col_map
        for item in candidate_headers
    ):
        candidate_headers.append({
            "header_range": [best.header_row_lo, best.header_row_hi],
            "col_map": dict(best.col_map),
            "confidence": best.confidence,
            "needs_review": best.needs_review,
            "notes": list(best.notes),
        })
        candidate_headers.sort(
            key=lambda item: (item["header_range"][0], item["header_range"][1], item["confidence"])
        )
    best.candidate_headers = candidate_headers
    if len(unique_candidates) > 1:
        best.unresolved_candidate_count = len(unique_candidates)
        best.needs_review = True
        best.risk_flags.append("multiple_header_candidates")
        ranges = ", ".join(
            f"{candidate.header_row_lo}-{candidate.header_row_hi}"
            for candidate in sorted(
                unique_candidates.values(),
                key=lambda item: (item.header_row_lo, item.header_row_hi),
            )
        )
        best.notes.append(f"多个未解决表头候选（行范围：{ranges}），需人工确认")

    duplicate_rows = duplicate_probe
    pre_header_rows, pre_header_suspect_rows = detect_pre_header_rows(
        cells, best, max_col
    )
    best.duplicate_header_rows = duplicate_rows
    best.pre_header_nonempty_rows = pre_header_rows
    best.pre_header_suspect_rows = pre_header_suspect_rows
    if duplicate_rows:
        best.needs_review = True
        best.risk_flags.append("duplicate_header_rows")
        best.notes.append(
            "检测到完整 Sheet 范围内的重复表头行："
            + ", ".join(str(row) for row in duplicate_rows)
            + "，需人工确认数据范围"
        )
    if pre_header_suspect_rows:
        best.needs_review = True
        best.risk_flags.append("pre_header_business_rows")
        best.notes.append(
            "选定表头前存在疑似业务数据行："
            + ", ".join(str(row) for row in pre_header_suspect_rows)
            + "，已暂停自动解析"
        )
    best.risk_flags = sorted(set(best.risk_flags))
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
    r"[\s0-9一二三四五六七八九十第\.、（）()\-—:：,，ⅠⅡⅢ]+|分部分项|部分|[章节类段页]"
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
    # 金额型表头 + 其下 ≥3 个数值行 = 真表格信号：对 strong/weak 一票否决。
    # 真实缺陷依据：①E.6 汇总表表头仅"金额"一个词典命中（汇总内容不在词典），
    # weak 签字行误拦；②小页表-08 的特征列满是"1.名称：…2.型号：…"冒号文本，
    # kv 行占多数触发 strong——两者都是真实结算书的常态结构，不是表单。
    money_header_row = None
    money_cols: set[int] = set()
    for r in range(1, min(15, max_row) + 1):
        for (rr, c), text in cells.items():
            if rr != r:
                continue
            hits = [f for f, _s in _score_header_cell(text or "")]
            if any(f in ("amount", "unit_price") for f in hits):
                money_header_row = r
                money_cols.add(c)
        if money_header_row is not None:
            break
    if money_header_row is not None:
        # 数值必须出现在金额命中的列下方（真表金额列下方是数字；
        # 键值页的金额词在标签格里，其列下方是文本——不构成表格信号）。
        numeric_re = __import__("re").compile(r"^[\d,，\s．.+-]+$")
        numeric_rows = 0
        seen_rows: set[int] = set()
        for (r, c), text in cells.items():
            if r <= money_header_row or c not in money_cols or not (text or "").strip():
                continue
            t = text.strip().replace("¥", "").replace("￥", "")
            if numeric_re.match(t) and r not in seen_rows:
                seen_rows.add(r)
                numeric_rows += 1
                if numeric_rows >= 3:
                    return None  # 是数据表，不是表单
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
