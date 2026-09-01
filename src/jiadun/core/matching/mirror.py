"""对上/对下镜像匹配复核的确定性数据模型（P1-02）。

UI 只负责排版和筛选；字段读取、Decimal 差值、差异率和来源证据在这里
统一完成。没有对应一侧、字段缺失、单位不一致或原始金额不可解析时，
结果保持 ``pending``/``incomparable``，不会用 0 补齐，也不会把候选当作
人工最终匹配。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from jiadun.core.contracts import run_contract
from jiadun.core.engine.aggregate import assess_amount
from jiadun.core.engine.money import NotANumberError, to_decimal
from jiadun.core.matching import matching

D = Decimal

MIRROR_FIELDS = ("code", "name", "feature", "unit", "quantity", "unit_price", "amount")
NUMERIC_FIELDS = {"quantity", "unit_price", "amount"}


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return to_decimal(value)
    except (NotANumberError, TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normal_text(value: Any) -> str:
    return "".join(_text(value).split()).casefold()


def _normal_unit(value: Any) -> str:
    aliases = {
        "m³": "m3", "立方米": "m3", "立米": "m3", "m^3": "m3",
        "m²": "m2", "平方米": "m2", "平米": "m2", "m^2": "m2",
    }
    raw = _text(value).casefold()
    return aliases.get(raw, raw)


def _source(row: sqlite3.Row) -> dict[str, Any]:
    flags_raw = row["flags_json"] or "{}"
    try:
        flags = json.loads(flags_raw)
    except (TypeError, json.JSONDecodeError):
        flags = {}
    output: dict[str, Any] = {
        "line_item_id": int(row["id"]),
        "period_id": int(row["period_id"]),
        "period_no": int(row["period_no"]),
        "direction": row["direction"] or "unknown",
        "file_id": row["file_id"],
        "file": row["original_name"],
        "sheet_id": row["sheet_id"],
        "sheet": row["sheet_name"],
    }
    if isinstance(flags, dict):
        for key in ("row", "col", "source_row", "source_col"):
            if flags.get(key) is not None:
                output[key] = flags[key]
    return output


def _load_match_rows(conn: sqlite3.Connection, project_id: int, match_row: sqlite3.Row) -> list[sqlite3.Row]:
    try:
        ids = [int(value) for value in json.loads(match_row["item_ids_json"] or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        ids = []
    key = matching._unscoped_group_key(match_row["group_key"])
    if key.startswith("code:"):
        sql = """SELECT li.*, sp.period_no, sp.direction, sf.id AS file_id,
                         sf.original_name, rs.sheet_name
                  FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
                  LEFT JOIN raw_sheets rs ON rs.id=li.sheet_id
                  LEFT JOIN parse_batches pb ON pb.id=rs.batch_id
                  LEFT JOIN source_files sf ON sf.id=pb.file_id
                  WHERE sp.project_id=? AND li.code=?
                  ORDER BY CASE sp.direction WHEN 'upward' THEN 0 WHEN 'downward' THEN 1 ELSE 2 END,
                           sp.period_no, li.id"""
        return conn.execute(sql, (project_id, key[5:])).fetchall()
    if key.startswith("name:"):
        rows = conn.execute(
            """SELECT li.*, sp.period_no, sp.direction, sf.id AS file_id,
                      sf.original_name, rs.sheet_name
               FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
               LEFT JOIN raw_sheets rs ON rs.id=li.sheet_id
               LEFT JOIN parse_batches pb ON pb.id=rs.batch_id
               LEFT JOIN source_files sf ON sf.id=pb.file_id
               WHERE sp.project_id=? ORDER BY sp.period_no, li.id""",
            (project_id,),
        ).fetchall()
        wanted = key[5:]
        return [row for row in rows if matching.normalize_name(row["name"]) == wanted]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return conn.execute(
        f"""SELECT li.*, sp.period_no, sp.direction, sf.id AS file_id,
                   sf.original_name, rs.sheet_name
            FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
            LEFT JOIN raw_sheets rs ON rs.id=li.sheet_id
            LEFT JOIN parse_batches pb ON pb.id=rs.batch_id
            LEFT JOIN source_files sf ON sf.id=pb.file_id
            WHERE sp.project_id=? AND li.id IN ({placeholders})
            ORDER BY sp.period_no, li.id""",
        (project_id, *ids),
    ).fetchall()


def _aggregate_side(rows: list[sqlite3.Row], direction: str) -> dict[str, Any]:
    side_rows = [row for row in rows if (row["direction"] or "unknown") == direction]
    values: dict[str, list[Any]] = {field: [] for field in MIRROR_FIELDS}
    sources = [_source(row) for row in side_rows]
    for row in side_rows:
        for field in ("code", "name", "feature", "unit"):
            value = _text(row[field])
            if value not in values[field]:
                values[field].append(value)
        for field in ("quantity", "unit_price"):
            value = _dec(row[field])
            if value is not None:
                values[field].append(value)
        assessment, _qty_missing, _price_missing = assess_amount(row)
        if assessment.effective is not None:
            values["amount"].append(assessment.effective)
    output: dict[str, Any] = {
        "row_count": len(side_rows),
        "line_item_ids": [int(row["id"]) for row in side_rows],
        "sources": sources,
    }
    for field in MIRROR_FIELDS:
        if field in NUMERIC_FIELDS:
            unique = sorted(set(values[field]))
            output[field] = unique[0] if len(unique) == 1 else None
            output[f"{field}_values"] = unique
        else:
            output[field] = values[field][0] if len(values[field]) == 1 else None
            output[f"{field}_values"] = values[field]
    output["multiple_values"] = any(len(values[field]) > 1 for field in MIRROR_FIELDS)
    return output


def _compare_value(left: Any, right: Any, field: str) -> tuple[Any, str]:
    if left is None or right is None:
        return None, "待补资料"
    if field in NUMERIC_FIELDS:
        left_dec = _dec(left)
        right_dec = _dec(right)
        if left_dec is None or right_dec is None:
            return None, "待补资料"
        return right_dec - left_dec, "完全一致" if left_dec == right_dec else "数值存在差异"
    if field == "unit":
        return None, "完全一致" if _normal_unit(left) == _normal_unit(right) else "单位存在差异"
    if field == "name":
        return None, "完全一致" if matching.normalize_name(str(left)) == matching.normalize_name(str(right)) else "名称存在差异"
    return None, "完全一致" if _normal_text(left) == _normal_text(right) else "存在差异"


@dataclass(frozen=True)
class MirrorField:
    field: str
    label: str
    upward: Any
    downward: Any
    difference: Decimal | None
    difference_rate: Decimal | None
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "label": self.label,
            "upward": str(self.upward) if isinstance(self.upward, D) else self.upward,
            "downward": str(self.downward) if isinstance(self.downward, D) else self.downward,
            "difference": str(self.difference) if self.difference is not None else None,
            "difference_rate": str(self.difference_rate) if self.difference_rate is not None else None,
            "status": self.status,
        }


@dataclass(frozen=True)
class MirrorComparison:
    project_id: int
    match_id: int
    match_status: str
    match_level: str
    match_method: str
    confidence: Decimal | None
    reason: str
    fields: tuple[MirrorField, ...]
    quantity_difference: Decimal | None
    unit_price_difference: Decimal | None
    amount_difference: Decimal | None
    amount_difference_rate: Decimal | None
    evidence_ids: tuple[int, ...]
    upward_sources: tuple[dict[str, Any], ...]
    downward_sources: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "match_id": self.match_id,
            "match_status": self.match_status,
            "match_level": self.match_level,
            "match_method": self.match_method,
            "confidence": str(self.confidence) if self.confidence is not None else None,
            "reason": self.reason,
            "fields": [field.as_dict() for field in self.fields],
            "quantity_difference": str(self.quantity_difference) if self.quantity_difference is not None else None,
            "unit_price_difference": str(self.unit_price_difference) if self.unit_price_difference is not None else None,
            "amount_difference": str(self.amount_difference) if self.amount_difference is not None else None,
            "amount_difference_rate": str(self.amount_difference_rate) if self.amount_difference_rate is not None else None,
            "evidence_ids": list(self.evidence_ids),
            "upward_sources": [dict(item) for item in self.upward_sources],
            "downward_sources": [dict(item) for item in self.downward_sources],
        }


def build_mirror_comparison(
    conn: sqlite3.Connection, project_id: int, match_id: int
) -> MirrorComparison:
    """读取当前匹配组并生成对上/对下镜像字段与差异。"""
    scope, params = run_contract.current_scope(conn, project_id, "m")
    row = conn.execute(
        f"SELECT m.* FROM matches m WHERE m.id=? AND m.project_id=? AND {scope}",
        (match_id, project_id, *params),
    ).fetchone()
    if row is None:
        raise ValueError("匹配组不在当前运行范围，请重新运行匹配")
    rows = _load_match_rows(conn, project_id, row)
    upward = _aggregate_side(rows, "upward")
    downward = _aggregate_side(rows, "downward")
    labels = {
        "code": "编码", "name": "名称", "feature": "项目特征", "unit": "单位",
        "quantity": "数量", "unit_price": "单价", "amount": "金额",
    }
    fields: list[MirrorField] = []
    for field in MIRROR_FIELDS:
        left = upward[field]
        right = downward[field]
        difference, status = _compare_value(left, right, field)
        if left is None or right is None:
            status = "待补资料/缺一侧"
        if upward["multiple_values"] or downward["multiple_values"]:
            status = "待人工确认（同侧多值）"
        rate = None
        if field in NUMERIC_FIELDS and difference is not None:
            left_dec = _dec(left)
            if left_dec not in (None, D(0)):
                rate = difference / left_dec
            elif left_dec == D(0) and difference == D(0):
                rate = D(0)
        fields.append(MirrorField(field, labels[field], left, right, difference, rate, status))
    quantity_difference = next(item.difference for item in fields if item.field == "quantity")
    unit_price_difference = next(item.difference for item in fields if item.field == "unit_price")
    amount_difference = next(item.difference for item in fields if item.field == "amount")
    amount_rate = next(item.difference_rate for item in fields if item.field == "amount")
    if not upward["row_count"] or not downward["row_count"]:
        reason = "任一方向没有对应清单行，不能认定匹配"
    elif upward["multiple_values"] or downward["multiple_values"]:
        reason = "同一方向存在多个候选值，需人工逐项确认"
    else:
        reason = row["review_note"] or "按当前匹配规则生成左右镜像复核"
    evidence_rows = conn.execute(
        """SELECT id FROM evidence WHERE project_id=? AND scope IN ('current','human')
           AND sources_json LIKE ? ORDER BY id""",
        (project_id, f'%"match_id": {int(match_id)}%'),
    ).fetchall()
    return MirrorComparison(
        project_id=int(project_id),
        match_id=int(match_id),
        match_status=row["status"],
        match_level=row["level"],
        match_method=row["method"],
        confidence=_dec(row["score"]),
        reason=reason,
        fields=tuple(fields),
        quantity_difference=quantity_difference,
        unit_price_difference=unit_price_difference,
        amount_difference=amount_difference,
        amount_difference_rate=amount_rate,
        evidence_ids=tuple(int(item["id"]) for item in evidence_rows),
        upward_sources=tuple(upward["sources"]),
        downward_sources=tuple(downward["sources"]),
    )


__all__ = ["MIRROR_FIELDS", "MirrorComparison", "MirrorField", "build_mirror_comparison"]
