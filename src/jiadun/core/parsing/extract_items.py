"""行抽取器：保真网格 + 表头识别结果 → line_items 草稿。

每个字段记录出处单元格（row, col, 原始文本）——证据链的地基。
缺失字段一律 None（=待补资料），绝不补 0。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from jiadun.core.engine.money import NotANumberError, to_decimal
from jiadun.core.labels import direction_label
from jiadun.core.parsing.header_detect import (
    HeaderDetection,
    build_anchor_map,
    data_rows_range,
    is_grand_total_row,
    is_subtotal_row,
)


@dataclass
class FieldSource:
    value: str | None  # 规范化后的值（数值为 Decimal 字符串）
    raw: str  # 原始文本
    row: int
    col: int


@dataclass
class ItemDraft:
    row: int
    code: FieldSource | None = None
    name: FieldSource | None = None
    feature: FieldSource | None = None
    unit: FieldSource | None = None
    quantity: FieldSource | None = None
    unit_price: FieldSource | None = None
    amount: FieldSource | None = None
    tax_rate: FieldSource | None = None
    flags: dict = field(default_factory=dict)


NUMERIC_FIELDS = {"quantity", "unit_price", "amount"}


def _text_at(cells: dict[tuple[int, int], str], anchors, row: int, col: int | None) -> str:
    if col is None:
        return ""
    key = (row, col)
    if key in anchors:
        key = anchors[key]
    return cells.get(key, "").strip()


def extract_items(
    cells: dict[tuple[int, int], str],
    merged_ranges: list[str],
    det: HeaderDetection,
    max_row: int,
    data_range: tuple[int, int] | None = None,
    stats: dict | None = None,
) -> list[ItemDraft]:
    """从网格抽取清单行草稿。跳过全空行；小计/合计行打 flags['subtotal']=True。

    小计标签扫描名称列与首个映射列之前的所有行首列（支表二的"总计"
    写在序号列，编码列从 2 开始时也会命中）。明细区末尾连续的孤儿
    数值行（尾注/比例计算行）剔除，不进入 line_items——原文仍保留
    在保真层 raw_cells。
    """
    anchors = build_anchor_map(merged_ranges)
    start, end = data_range if data_range is not None else data_rows_range(cells, det, max_row)
    items: list[ItemDraft] = []
    skip_stats = stats if stats is not None else {}
    for r in range(start, end + 1):
        row_cells = {f: _text_at(cells, anchors, r, det.col_map.get(f)) for f in det.col_map}
        if not any(row_cells.values()):
            continue
        name_src = row_cells.get("name", "")
        code_src = row_cells.get("code", "")
        min_col = min(det.col_map.values())
        lead_text = "".join(
            t for t in (_text_at(cells, anchors, r, c) for c in range(1, min_col + 1)) if t
        )
        item = ItemDraft(row=r)
        item.flags["subtotal"] = is_subtotal_row(name_src, lead_text)
        if item.flags["subtotal"]:
            item.flags["grand_total"] = is_grand_total_row(name_src, lead_text)
        # 分部/章节标题行：只有名称（或编码），无任何数值 → 标记并跳过。
        # 有编码但无数值的行（如"缺数量清单行"）不在此列——它们有名称+编码，
        # 数值缺失走待补资料流程；本分支仅针对真实结算书分部标题行
        # （名称非空、编码空、三个数值列全空）。
        if (not item.flags["subtotal"] and not code_src and name_src
                and not any(row_cells.get(f) for f in ("quantity", "unit_price", "amount"))):
            skip_stats["title_rows"] = skip_stats.get("title_rows", 0) + 1
            continue  # 分部/章节标题行：非清单项，直接跳过（原文在保真层可回溯）
        if not name_src and not code_src:
            if item.flags["subtotal"]:
                pass  # 合计行保留（异常检测需要它与明细核对）
            elif any(row_cells.get(f) for f in ("quantity", "unit_price", "amount")):
                item.flags["orphan_numeric_row"] = True  # 只有数值没有名称：保留待人工复核
            else:
                continue
        for f, col in det.col_map.items():
            raw = _text_at(cells, anchors, r, col)
            src = FieldSource(value=None, raw=raw, row=r, col=col)
            if f in NUMERIC_FIELDS:
                if raw:
                    try:
                        src.value = str(to_decimal(raw))
                    except NotANumberError:
                        src.value = None
                        item.flags[f"{f}_unparsed"] = raw  # 原文保留，进入待补/异常流程
            elif f == "tax_rate":
                if raw:
                    src.value = _normalize_tax(raw)
            else:
                src.value = raw or None
            setattr(item, f, src)
        items.append(item)
    tail_popped = 0
    while items and items[-1].flags.get("orphan_numeric_row"):
        items.pop()  # 明细区末尾连续尾注行：剔除，保真层已留原文
        tail_popped += 1
    if tail_popped:
        skip_stats["tail_note_rows"] = tail_popped
    return items


def _normalize_tax(raw: str) -> str | None:
    """'13%'/'0.13'/'13' → '0.13'；无法解析返回 None（待补）。"""
    r = raw.strip()
    try:
        if r.endswith("%"):
            return str(to_decimal(r[:-1]) / 100)
        d = to_decimal(r)
        if d > 1:  # 13 → 0.13
            return str(d / 100)
        return str(d)
    except NotANumberError:
        return None


def _evid_json(src: FieldSource | None, evidence_id: int | None = None) -> str | None:
    if src is None:
        return None
    payload = {"value": src.value, "raw": src.raw, "row": src.row, "col": src.col}
    if evidence_id is not None:
        payload["evidence_id"] = evidence_id
    return json.dumps(payload, ensure_ascii=False)


def persist_line_items(
    conn: sqlite3.Connection,
    period_id: int,
    sheet_id: int,
    items: list[ItemDraft],
    *,
    commit: bool = True,
) -> int:
    # 字段来源原本只保存在 line_items 的 JSON 中，无法进入成果工作簿的
    # 证据索引。现在为每一行建立一条真实 Evidence 记录，并把其整数 ID
    # 回写到各字段来源 JSON；同一行共享一个证据 ID，避免大项目为每个
    # 字段重复写三份证据，同时保留原始行列和值。
    meta = conn.execute(
        """SELECT sp.project_id, sp.period_no, sp.direction, rs.sheet_name,
                  sf.original_name
           FROM settlement_periods sp
           LEFT JOIN raw_sheets rs ON rs.id=?
           LEFT JOIN parse_batches pb ON pb.id=rs.batch_id
           LEFT JOIN source_files sf ON sf.id=pb.file_id
           WHERE sp.id=?""",
        (sheet_id, period_id),
    ).fetchone()
    project_id = int(meta["project_id"]) if meta else None
    now = datetime.now().isoformat(timespec="seconds")
    if commit:
        conn.execute("BEGIN")
    try:
        rows = []
        for it in items:
            if it.flags.get("title_row"):
                continue  # 分部/章节标题行：非清单项（原文在保真层可回溯）
            field_sources = []
            for field_name in ("code", "name", "feature", "unit", "quantity",
                               "unit_price", "amount", "tax_rate"):
                src = getattr(it, field_name, None)
                if src is None or not src.raw:
                    continue
                field_sources.append({
                    "field": field_name,
                    "row": src.row,
                    "col": src.col,
                    "raw_value": src.raw,
                    "value": src.value,
                })
            evidence_id = None
            if project_id is not None and field_sources:
                summary = (
                    f"第{meta['period_no']}期{direction_label(meta['direction'])}清单行来源："
                    f"第{it.row}行「{(it.name.value if it.name and it.name.value else it.name.raw if it.name else '')}」"
                )
                cur = conn.execute(
                    """INSERT INTO evidence(project_id, kind, summary, steps_json,
                       sources_json, created_at) VALUES (?,?,?,?,?,?)""",
                    (
                        project_id,
                        "line_item_source",
                        summary,
                        json.dumps(
                            [{"step": "原始字段定位", "result": f"第{it.row}行"}],
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            [{
                                "file": meta["original_name"] if meta else None,
                                "sheet": meta["sheet_name"] if meta else None,
                                "period": meta["period_no"] if meta else None,
                                "direction": meta["direction"] if meta else "unknown",
                                **source,
                            } for source in field_sources],
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
                evidence_id = int(cur.lastrowid)
            flags_payload = {"row": it.row, **it.flags}
            if evidence_id is not None:
                # 保持既有字段来源 JSON 结构兼容；证据 ID 放在行级 flags，
                # 供 UI/Excel 通过真实 evidence 表记录跳转。
                flags_payload["source_evidence_id"] = evidence_id
            rows.append(
                (
                    period_id, sheet_id,
                    it.code.value if it.code else None,
                    (it.name.value if (it.name and it.name.value) else (it.name.raw if it.name else "")),
                    it.feature.value if it.feature else None,
                    it.unit.value if it.unit else None,
                    it.quantity.value if it.quantity else None,
                    it.unit_price.value if it.unit_price else None,
                    it.amount.value if it.amount else None,
                    it.tax_rate.value if it.tax_rate else None,
                    _evid_json(it.quantity),
                    _evid_json(it.unit_price),
                    _evid_json(it.amount),
                    json.dumps(flags_payload, ensure_ascii=False),
                )
            )
        conn.executemany(
            """INSERT INTO line_items(period_id, sheet_id, code, name, feature, unit,
               quantity, unit_price, amount, tax_rate, qty_evid, price_evid, amount_evid, flags_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        n = conn.execute(
            "SELECT COUNT(*) FROM line_items WHERE period_id=?", (period_id,)
        ).fetchone()[0]
        if commit:
            conn.execute("COMMIT")
        return n
    except Exception:
        if commit and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
