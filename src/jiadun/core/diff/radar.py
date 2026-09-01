"""确定性清单差异雷达。

P1-01 要求比较两个期次、两个版本或明确选择的对上/对下方向，并分类：
新增、删除、工程量变化、单价变化、合价变化、编码/名称/特征/单位变化、
未变化、待确认和不可比。这里不使用名称模糊相似度、不使用 float、不把
缺失金额补成零，也不把待确认金额计入确认后的净影响。

匹配策略是可解释的两阶段一对一匹配：

1. 先按清洗后的编码精确匹配；
2. 对未匹配行，按名称+特征+单位，再按名称+特征（用于识别单位变化）
   精确匹配，且任一侧多行时标记待确认，不强行配对。

每个比较项保留来源文件、Sheet、原始行和 line_item ID，调用方可以把
``persist_diff_radar`` 生成的 Evidence ID 回链到导出或人工复核记录。
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from jiadun.core.contracts import run_contract
from jiadun.core.engine.aggregate import assess_amount
from jiadun.core.engine.money import NotANumberError, to_decimal
from jiadun.core.evidence import evidence as evidence_api
from jiadun.core.evidence.finding import canonical_json

D = Decimal

# 顺序是稳定协议的一部分；新增类别只能追加或同步更新文档，不能按显示顺序
# 临时排序，否则黄金回归结果会无故变化。
CATEGORIES = (
    "added",
    "deleted",
    "quantity_changed",
    "unit_price_changed",
    "amount_changed",
    "code_changed",
    "name_changed",
    "feature_changed",
    "unit_changed",
    "unchanged",
    "pending",
    "incomparable",
)

CATEGORY_LABELS = {
    "added": "新增",
    "deleted": "删除",
    "quantity_changed": "工程量变化",
    "unit_price_changed": "单价变化",
    "amount_changed": "合价变化",
    "code_changed": "编码变化",
    "name_changed": "名称变化",
    "feature_changed": "项目特征变化",
    "unit_changed": "单位变化",
    "unchanged": "未变化",
    "pending": "待确认",
    "incomparable": "不可比",
}

STATUS_LABELS = {
    "confirmed": "已确认",
    "pending": "待确认",
    "incomparable": "不可比",
}

_FIELD_ORDER = ("quantity", "unit_price", "amount", "code", "name", "feature", "unit")


def _norm_text(value: Any) -> str:
    return "".join(str(value or "").strip().split()).casefold()


def _norm_code(value: Any) -> str:
    return str(value or "").strip().casefold()


def _norm_unit(value: Any) -> str:
    raw = str(value or "").strip().casefold()
    aliases = {
        "m³": "m3",
        "立方米": "m3",
        "立米": "m3",
        "m^3": "m3",
        "m²": "m2",
        "平方米": "m2",
        "平米": "m2",
        "m^2": "m2",
    }
    return aliases.get(raw, raw)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return to_decimal(value)
    except (NotANumberError, TypeError, ValueError):
        return None


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _value_equal(left: Any, right: Any, field_name: str) -> bool:
    if field_name in {"quantity", "unit_price", "amount"}:
        l_dec = _decimal_or_none(left)
        r_dec = _decimal_or_none(right)
        if l_dec is None or r_dec is None:
            return l_dec is None and r_dec is None
        return l_dec == r_dec
    if field_name == "unit":
        return _norm_unit(left) == _norm_unit(right)
    if field_name == "code":
        return _norm_code(left) == _norm_code(right)
    return _norm_text(left) == _norm_text(right)


def _source_ref(row: dict[str, Any]) -> dict[str, Any]:
    """只保留可回溯到源文件的最小引用，不复制整行原文。"""
    flags = row.get("flags") or {}
    ref: dict[str, Any] = {
        "line_item_id": int(row["id"]),
        "period_id": int(row["period_id"]),
        "period_no": int(row["period_no"]),
        "direction": row.get("direction") or "unknown",
        "file_id": row.get("file_id"),
        "file": row.get("original_name"),
        "sheet_id": row.get("sheet_id"),
        "sheet": row.get("sheet_name"),
    }
    for key in ("row", "col", "source_row", "source_col"):
        if key in flags and flags[key] is not None:
            ref[key] = flags[key]
    return ref


def _effective(row: dict[str, Any]) -> tuple[Decimal | None, str, str]:
    assessment, _qty_missing, _price_missing = assess_amount(row)  # type: ignore[arg-type]
    return assessment.effective, assessment.source, assessment.check_status


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    flags_raw = row["flags_json"] or "{}"
    try:
        flags = json.loads(flags_raw)
    except (TypeError, json.JSONDecodeError):
        flags = {"flags_json": flags_raw}
    result = dict(row)
    result["flags"] = flags if isinstance(flags, dict) else {"flags": flags}
    return result


def _load_period_rows(conn: sqlite3.Connection, period_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT li.*, sp.period_no, sp.direction,
                  sf.id AS file_id, sf.original_name,
                  rs.sheet_name
           FROM line_items li
           JOIN settlement_periods sp ON sp.id=li.period_id
           LEFT JOIN raw_sheets rs ON rs.id=li.sheet_id
           LEFT JOIN parse_batches pb ON pb.id=rs.batch_id
           LEFT JOIN source_files sf ON sf.id=pb.file_id
           WHERE li.period_id=? ORDER BY li.id""",
        (int(period_id),),
    ).fetchall()
    result = []
    for row in rows:
        item = _row_dict(row)
        if item["flags"].get("subtotal"):
            continue
        result.append(item)
    return result


def _row_key(row: dict[str, Any], mode: str) -> tuple[str, ...] | None:
    code = _norm_code(row.get("code"))
    name = _norm_text(row.get("name"))
    feature = _norm_text(row.get("feature"))
    unit = _norm_unit(row.get("unit"))
    if mode == "code":
        return (code,) if code else None
    if mode == "name_feature_unit":
        return (name, feature, unit) if name else None
    if mode == "name_feature":
        return (name, feature) if name else None
    raise ValueError(f"unknown matching mode: {mode}")


def _group_rows(rows: Iterable[dict[str, Any]], mode: str) -> dict[tuple[str, ...], list[dict[str, Any]]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = _row_key(row, mode)
        if key is not None:
            groups[key].append(row)
    return groups


def _amount_for_group(rows: list[dict[str, Any]]) -> tuple[Decimal | None, str, bool]:
    """返回有效金额、来源和是否存在金额/单价冲突。"""
    total: Decimal | None = None
    sources: set[str] = set()
    invalid = False
    for row in rows:
        effective, source, check_status = _effective(row)
        if effective is None:
            invalid = True
        else:
            total = effective if total is None else total + effective
            sources.add(source)
        if check_status == "diff":
            # 原始合价与工程量×单价不一致不自动改值，但该比较仍可显示；
            # 证据中明确保留冲突来源，confirmed net impact 仍只表示数值差。
            sources.add("raw_checked_diff")
    return total, "+".join(sorted(sources)) if sources else "missing", invalid


def _quantity_for_group(rows: list[dict[str, Any]]) -> Decimal | None:
    total: Decimal | None = None
    for row in rows:
        value = _decimal_or_none(row.get("quantity"))
        if value is None:
            return None
        total = value if total is None else total + value
    return total


def _price_for_group(rows: list[dict[str, Any]]) -> Decimal | None:
    prices = {_decimal_or_none(row.get("unit_price")) for row in rows}
    prices.discard(None)
    if not prices or len(prices) != 1 or len(rows) != 1:
        return None
    return next(iter(prices))


def _representative(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    first = rows[0]
    return {
        "code": first.get("code") or "",
        "name": first.get("name") or "",
        "feature": first.get("feature") or "",
        "unit": first.get("unit") or "",
        "quantity": _decimal_text(_quantity_for_group(rows)),
        "unit_price": _decimal_text(_price_for_group(rows)),
        "amount": _decimal_text(_amount_for_group(rows)[0]),
        "line_item_ids": [int(row["id"]) for row in rows],
    }


def _match_groups(
    baseline: list[dict[str, Any]], current: list[dict[str, Any]],
) -> tuple[list[tuple[list[dict[str, Any]], list[dict[str, Any]], str]], list[dict[str, Any]], list[dict[str, Any]]]:
    """返回已配对组和两侧未配对行，匹配始终一对一且不强行选择。"""
    base_remaining = {int(row["id"]): row for row in baseline}
    curr_remaining = {int(row["id"]): row for row in current}
    pairs: list[tuple[list[dict[str, Any]], list[dict[str, Any]], str]] = []

    def consume(mode: str) -> None:
        base_groups = _group_rows(base_remaining.values(), mode)
        curr_groups = _group_rows(curr_remaining.values(), mode)
        for key in sorted(set(base_groups) & set(curr_groups), key=lambda item: tuple(map(str, item))):
            b_rows = base_groups[key]
            c_rows = curr_groups[key]
            if len(b_rows) != 1 or len(c_rows) != 1:
                # 多行同键不能推断对应关系；留到 pending 记录，而不是任选一行。
                continue
            pairs.append((b_rows, c_rows, mode))
            base_remaining.pop(int(b_rows[0]["id"]), None)
            curr_remaining.pop(int(c_rows[0]["id"]), None)

    consume("code")
    consume("name_feature_unit")
    # 最后忽略单位用于识别单位变化；双方均需名称，仍要求唯一。
    consume("name_feature")
    return pairs, list(base_remaining.values()), list(curr_remaining.values())


def _row_has_pending(row: dict[str, Any]) -> bool:
    flags = row.get("flags") or {}
    return bool(
        flags.get("pending")
        or flags.get("parse_failed")
        or flags.get("unrecognized")
        or flags.get("needs_review")
    )


def _classify_pair(
    baseline_rows: list[dict[str, Any]],
    current_rows: list[dict[str, Any]],
    match_mode: str,
) -> tuple[str, tuple[str, ...], str, Decimal | None, bool]:
    b = _representative(baseline_rows)
    c = _representative(current_rows)
    field_changed = tuple(
        field for field in _FIELD_ORDER
        if not _value_equal(b.get(field), c.get(field), field)
    )
    b_amount, _b_source, b_invalid = _amount_for_group(baseline_rows)
    c_amount, _c_source, c_invalid = _amount_for_group(current_rows)
    amount_impact = c_amount - b_amount if b_amount is not None and c_amount is not None else None
    pending = (
        any(_row_has_pending(row) for row in baseline_rows + current_rows)
        or len(baseline_rows) != 1
        or len(current_rows) != 1
        or b_amount is None
        or c_amount is None
        or b_invalid
        or c_invalid
    )
    unit_changed = "unit" in field_changed
    if unit_changed:
        # 数量/金额在不同计量单位间不能直接净额比较；仍把单位变化作为
        # 具体字段类别保留，主类别为 incomparable。
        categories = tuple(dict.fromkeys(["unit_changed", "incomparable"] + list(field_changed)))
        return "incomparable", categories, "单位或计量口径变化，金额影响不可直接比较", None, False
    if match_mode == "name_feature" and _norm_code(b.get("code")) != _norm_code(c.get("code")):
        field_changed = tuple(dict.fromkeys(["code", *field_changed]))
    if not field_changed:
        if pending:
            return "pending", ("pending",), "清单存在待确认或金额缺失，暂不形成净影响", None, False
        return "unchanged", ("unchanged",), "两期字段和有效金额未变化", D(0), True

    categories = tuple(
        dict.fromkeys(
            [
                {
                    "quantity": "quantity_changed",
                    "unit_price": "unit_price_changed",
                    "amount": "amount_changed",
                    "code": "code_changed",
                    "name": "name_changed",
                    "feature": "feature_changed",
                    "unit": "unit_changed",
                }[field]
                for field in field_changed
            ]
            + (["pending"] if pending else [])
        )
    )
    # 若同时变更多个字段，主类别保持稳定：先标结构性变化，再标数量/价格/金额。
    precedence = (
        "code_changed", "name_changed", "feature_changed", "quantity_changed",
        "unit_price_changed", "amount_changed",
    )
    primary = next((category for category in precedence if category in categories), categories[0])
    if pending:
        return primary, categories, "存在待确认或金额不完整，净影响不计入确认汇总", None, False
    return primary, categories, "字段或有效金额发生变化", amount_impact, True


def _unmatched_item(
    rows: list[dict[str, Any]], *, added: bool,
) -> tuple[str, tuple[str, ...], str, Decimal | None, bool]:
    amount, _source, invalid = _amount_for_group(rows)
    pending = len(rows) != 1 or amount is None or invalid or any(_row_has_pending(row) for row in rows)
    category = "added" if added else "deleted"
    if pending:
        return category, (category, "pending"), "新增/删除侧资料或金额待确认，净影响不计入确认汇总", None, False
    impact = amount if added else -amount
    return category, (category,), "当前期新增" if added else "当前期删除", impact, True


@dataclass(frozen=True)
class DiffItem:
    """一个可追溯的比较项。金额字段均为 Decimal 或 None。"""

    identity_key: str
    primary_category: str
    categories: tuple[str, ...]
    status: str
    reason: str
    amount_impact: Decimal | None
    confirmed_amount_impact: Decimal | None
    baseline: dict[str, Any]
    current: dict[str, Any]
    baseline_sources: tuple[dict[str, Any], ...] = ()
    current_sources: tuple[dict[str, Any], ...] = ()
    evidence_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity_key": self.identity_key,
            "primary_category": self.primary_category,
            "primary_category_label": CATEGORY_LABELS[self.primary_category],
            "categories": list(self.categories),
            "category_labels": [CATEGORY_LABELS[item] for item in self.categories],
            "status": self.status,
            "status_label": STATUS_LABELS[self.status],
            "reason": self.reason,
            "amount_impact": _decimal_text(self.amount_impact),
            "confirmed_amount_impact": _decimal_text(self.confirmed_amount_impact),
            "baseline": dict(self.baseline),
            "current": dict(self.current),
            "baseline_sources": [dict(item) for item in self.baseline_sources],
            "current_sources": [dict(item) for item in self.current_sources],
            "evidence_id": self.evidence_id,
        }


@dataclass(frozen=True)
class DiffRadar:
    """两期/两版本比较结果。"""

    project_id: int
    baseline_period_id: int
    current_period_id: int
    baseline_period_no: int
    current_period_no: int
    baseline_direction: str
    current_direction: str
    status: str
    status_label: str
    reason_codes: tuple[str, ...]
    items: tuple[DiffItem, ...]
    category_summary: dict[str, dict[str, Any]]
    status_counts: dict[str, int]
    confirmed_net_amount_impact: Decimal | None
    evidence_id: int | None = None
    run_id: str | None = None
    run_signature: str | None = None
    diff_run_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "baseline_period_id": self.baseline_period_id,
            "current_period_id": self.current_period_id,
            "baseline_period_no": self.baseline_period_no,
            "current_period_no": self.current_period_no,
            "baseline_direction": self.baseline_direction,
            "current_direction": self.current_direction,
            "status": self.status,
            "status_label": self.status_label,
            "reason_codes": list(self.reason_codes),
            "items": [item.as_dict() for item in self.items],
            "category_summary": {
                key: dict(value) for key, value in self.category_summary.items()
            },
            "status_counts": dict(self.status_counts),
            "confirmed_net_amount_impact": _decimal_text(self.confirmed_net_amount_impact),
            "evidence_id": self.evidence_id,
            "run_id": self.run_id,
            "run_signature": self.run_signature,
            "diff_run_id": self.diff_run_id,
        }


def _period_info(conn: sqlite3.Connection, period_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, project_id, period_no, direction FROM settlement_periods WHERE id=?",
        (int(period_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"期次不存在: {period_id}")
    return row


def _period_has_unresolved_sheet(conn: sqlite3.Connection, period_id: int) -> bool:
    row = conn.execute(
        """SELECT 1 FROM raw_sheets
           WHERE period_id=? AND sheet_status IN ('pending', 'parse_failed') LIMIT 1""",
        (int(period_id),),
    ).fetchone()
    return row is not None


def _item_identity(baseline: list[dict[str, Any]], current: list[dict[str, Any]], fallback: str) -> str:
    rows = baseline or current
    if not rows:
        return fallback
    row = rows[0]
    code = str(row.get("code") or "").strip()
    if code:
        return f"code:{code}"
    name = str(row.get("name") or "").strip()
    feature = str(row.get("feature") or "").strip()
    unit = str(row.get("unit") or "").strip()
    return f"name:{name}|feature:{feature}|unit:{unit}"


def _build_category_summary(items: Iterable[DiffItem]) -> dict[str, dict[str, Any]]:
    summary = {
        category: {"label": CATEGORY_LABELS[category], "count": 0, "amount_impact": None}
        for category in CATEGORIES
    }
    for item in items:
        # count 使用 primary category，避免同一清单同时改数量和单价时重复计数；
        # categories 仍完整输出给复核人员。
        bucket = summary[item.primary_category]
        bucket["count"] += 1
        if item.confirmed_amount_impact is not None:
            current = bucket["amount_impact"]
            bucket["amount_impact"] = (
                item.confirmed_amount_impact
                if current is None else current + item.confirmed_amount_impact
            )
    for bucket in summary.values():
        bucket["amount_impact"] = _decimal_text(bucket["amount_impact"])
    return summary


def build_diff_radar(
    conn: sqlite3.Connection,
    project_id: int,
    baseline_period_id: int,
    current_period_id: int,
    *,
    allow_cross_direction: bool = False,
) -> DiffRadar:
    """比较两个明确期次，返回不写数据库的差异雷达事实。

    对上/对下跨方向比较必须显式 ``allow_cross_direction=True``；即使允许，
    结果仍把方向差异写入 ``reason_codes``，避免用户把跨口径金额当成同方向
    期间变化。该函数不调用 ``ensure_run_contract``，适合 UI 预览和单元测试。
    """
    if int(baseline_period_id) == int(current_period_id):
        raise ValueError("基准期次和当前期次不能相同")
    baseline_info = _period_info(conn, baseline_period_id)
    current_info = _period_info(conn, current_period_id)
    if int(baseline_info["project_id"]) != int(project_id) or int(current_info["project_id"]) != int(project_id):
        raise ValueError("比较期次不属于当前项目")
    base_direction = baseline_info["direction"] or "unknown"
    curr_direction = current_info["direction"] or "unknown"
    reason_codes: list[str] = []
    if base_direction != curr_direction:
        if not allow_cross_direction:
            raise ValueError("对上结算与对下结算不能默认互比，请显式允许跨方向比较")
        reason_codes.append("cross_direction_explicit")
    if _period_has_unresolved_sheet(conn, baseline_period_id) or _period_has_unresolved_sheet(conn, current_period_id):
        reason_codes.append("sheet_scope_pending")

    baseline = _load_period_rows(conn, baseline_period_id)
    current = _load_period_rows(conn, current_period_id)
    pairs, base_remaining, curr_remaining = _match_groups(baseline, current)
    items: list[DiffItem] = []
    used_identity_keys: set[str] = set()

    def unique_identity(value: str, rows: list[dict[str, Any]]) -> str:
        candidate = value
        if candidate not in used_identity_keys:
            used_identity_keys.add(candidate)
            return candidate
        # 同期多行同码属于待确认；为满足 diff_items 的不可变唯一约束，
        # 追加源行 ID，而不是把重复行合并或任选一行覆盖。
        suffix = ":".join(str(row["id"]) for row in rows)
        candidate = f"{value}|line:{suffix}"
        used_identity_keys.add(candidate)
        return candidate

    for b_rows, c_rows, mode in pairs:
        primary, categories, reason, impact, confirmed = _classify_pair(b_rows, c_rows, mode)
        items.append(
            DiffItem(
                identity_key=unique_identity(
                    _item_identity(b_rows, c_rows, f"pair:{b_rows[0]['id']}:{c_rows[0]['id']}"),
                    b_rows + c_rows,
                ),
                primary_category=primary,
                categories=categories,
                status="confirmed" if confirmed else ("incomparable" if primary == "incomparable" else "pending"),
                reason=reason,
                amount_impact=impact,
                confirmed_amount_impact=impact if confirmed else None,
                baseline=_representative(b_rows),
                current=_representative(c_rows),
                baseline_sources=tuple(_source_ref(row) for row in b_rows),
                current_sources=tuple(_source_ref(row) for row in c_rows),
            )
        )
    for rows, added in ((base_remaining, False), (curr_remaining, True)):
        for row in sorted(rows, key=lambda item: int(item["id"])):
            primary, categories, reason, impact, confirmed = _unmatched_item([row], added=added)
            items.append(
                DiffItem(
                    identity_key=unique_identity(
                        _item_identity([], [row] if added else [], f"item:{row['id']}"),
                        [row],
                    ),
                    primary_category=primary,
                    categories=categories,
                    status="confirmed" if confirmed else "pending",
                    reason=reason,
                    amount_impact=impact,
                    confirmed_amount_impact=impact if confirmed else None,
                    baseline=_representative([] if added else [row]),
                    current=_representative([row] if added else []),
                    baseline_sources=tuple(() if added else (_source_ref(row),)),
                    current_sources=tuple((_source_ref(row),) if added else ()),
                )
            )
    items.sort(key=lambda item: (item.primary_category, item.identity_key))
    status_counts = {status: sum(1 for item in items if item.status == status) for status in STATUS_LABELS}
    confirmed_impacts = [item.confirmed_amount_impact for item in items if item.confirmed_amount_impact is not None]
    net = sum(confirmed_impacts, D(0)) if confirmed_impacts else None
    status = "available"
    status_label = "已生成"
    if not items:
        status = "no_rows"
        status_label = "当前无可比较明细"
        reason_codes.append("no_comparable_rows")
    elif status_counts["pending"] or status_counts["incomparable"]:
        status = "conditional"
        status_label = "有待确认或不可比项目"
    return DiffRadar(
        project_id=int(project_id),
        baseline_period_id=int(baseline_info["id"]),
        current_period_id=int(current_info["id"]),
        baseline_period_no=int(baseline_info["period_no"]),
        current_period_no=int(current_info["period_no"]),
        baseline_direction=base_direction,
        current_direction=curr_direction,
        status=status,
        status_label=status_label,
        reason_codes=tuple(reason_codes),
        items=tuple(items),
        category_summary=_build_category_summary(items),
        status_counts=status_counts,
        confirmed_net_amount_impact=net,
    )


def _evidence_payload(radar: DiffRadar) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    steps = [
        {
            "step": "比较范围",
            "baseline_period_id": radar.baseline_period_id,
            "current_period_id": radar.current_period_id,
            "baseline_period_no": radar.baseline_period_no,
            "current_period_no": radar.current_period_no,
            "baseline_direction": radar.baseline_direction,
            "current_direction": radar.current_direction,
            "allow_cross_direction": radar.baseline_direction != radar.current_direction,
        },
        {
            "step": "分类汇总",
            "category_summary": radar.category_summary,
            "status_counts": radar.status_counts,
            "confirmed_net_amount_impact": _decimal_text(radar.confirmed_net_amount_impact),
            "limitations": [
                "待确认和不可比项目不计入确认后的净金额影响",
                "金额使用原始合价，缺失时仅使用数量×单价计算候选，原始资料不改写",
            ],
        },
    ]
    sources: list[dict[str, Any]] = []
    for item in radar.items:
        sources.append({
            "identity_key": item.identity_key,
            "primary_category": item.primary_category,
            "status": item.status,
            "baseline": list(item.baseline_sources),
            "current": list(item.current_sources),
        })
    return steps, sources


def _current_contract_for_project(conn: sqlite3.Connection, project_id: int) -> run_contract.RunContract | None:
    return run_contract.get_current_contract(conn, project_id)


def persist_diff_radar(
    conn: sqlite3.Connection,
    radar: DiffRadar,
    *,
    actor: str = "system",
    commit: bool = True,
) -> DiffRadar:
    """将比较结果绑定当前 Run Contract 并写入 Evidence。

    当前版本在迁移 v23 后写入 ``diff_runs/diff_items``；若调用方连接尚未
    迁移到 v23，会明确失败而不写半成品。重复调用不会更新旧记录，每次都会
    新增不可变比较快照，历史运行由 ``run_id`` 隔离。
    """
    current = _current_contract_for_project(conn, radar.project_id)
    if current is None:
        current = run_contract.ensure_run_contract(conn, radar.project_id)
    steps, sources = _evidence_payload(radar)
    evidence_id = evidence_api.add_evidence(
        conn,
        radar.project_id,
        "diff_radar",
        f"清单差异雷达：第{radar.baseline_period_no}期→第{radar.current_period_no}期，"
        f"确认净金额影响 {_decimal_text(radar.confirmed_net_amount_impact) or '无法确认'}",
        steps=steps,
        sources=sources,
        commit=False,
        run_signature=current.signature,
        run_id=current.run_id,
        scope="current",
    )
    now = datetime.now().isoformat(timespec="seconds")

    def _write() -> int:
        cur = conn.execute(
            """INSERT INTO diff_runs(
                   project_id, run_id, run_signature, baseline_period_id, current_period_id,
                   baseline_direction, current_direction, status, reason_codes_json,
                   confirmed_net_amount_impact, evidence_id, created_at, metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                radar.project_id, current.run_id, current.signature,
                radar.baseline_period_id, radar.current_period_id,
                radar.baseline_direction, radar.current_direction,
                radar.status, canonical_json(list(radar.reason_codes)),
                _decimal_text(radar.confirmed_net_amount_impact), evidence_id, now,
                canonical_json({"actor": actor, "item_count": len(radar.items)}),
            ),
        )
        diff_run_id = int(cur.lastrowid)
        for item in radar.items:
            conn.execute(
                """INSERT INTO diff_items(
                       diff_run_id, project_id, identity_key, primary_category,
                       categories_json, status, reason, baseline_json, current_json,
                       amount_impact, confirmed_amount_impact, baseline_sources_json,
                       current_sources_json, evidence_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    diff_run_id, radar.project_id, item.identity_key, item.primary_category,
                    canonical_json(list(item.categories)), item.status, item.reason,
                    canonical_json(item.baseline), canonical_json(item.current),
                    _decimal_text(item.amount_impact), _decimal_text(item.confirmed_amount_impact),
                    canonical_json(list(item.baseline_sources)), canonical_json(list(item.current_sources)),
                    evidence_id, now,
                ),
            )
        return diff_run_id

    if commit:
        with conn:
            diff_run_id = _write()
    else:
        diff_run_id = _write()
    return DiffRadar(
        **{
            **radar.__dict__,
            "evidence_id": evidence_id,
            "run_id": current.run_id,
            "run_signature": current.signature,
            "diff_run_id": diff_run_id,
        }
    )


def read_current_diff_radar(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    baseline_period_id: int | None = None,
    current_period_id: int | None = None,
) -> list[dict[str, Any]]:
    """仅读取当前 Run Contract 下的比较快照；旧运行自动成为历史。"""
    contract = run_contract.get_current_contract(conn, project_id)
    if contract is None or run_contract.get_fail_closed_state(conn, project_id) is not None:
        return []
    # 读取接口保持只读，但不能因调用方绕过业务写入口直接修改期次/明细而
    # 继续展示旧运行的比较结果。重新计算预期签名只读取确定性输入；若与
    # 当前合同不一致，旧快照立即退出当前面，待下一次正式运行创建新合同。
    expected_signature = run_contract.compute_run_signature(
        run_contract.build_run_contract_components(conn, project_id)
    )
    if expected_signature != contract.signature:
        return []
    clauses = ["dr.project_id=?", "dr.run_id=?", "dr.run_signature=?"]
    params: list[Any] = [int(project_id), contract.run_id, contract.signature]
    if baseline_period_id is not None:
        clauses.append("dr.baseline_period_id=?")
        params.append(int(baseline_period_id))
    if current_period_id is not None:
        clauses.append("dr.current_period_id=?")
        params.append(int(current_period_id))
    rows = conn.execute(
        f"""SELECT dr.*, e.scope AS evidence_scope, e.historical_reason
            FROM diff_runs dr LEFT JOIN evidence e ON e.id=dr.evidence_id
            WHERE {' AND '.join(clauses)} ORDER BY dr.created_at, dr.id""",
        tuple(params),
    ).fetchall()
    result = []
    for row in rows:
        item_rows = conn.execute(
            "SELECT * FROM diff_items WHERE diff_run_id=? ORDER BY id", (row["id"],)
        ).fetchall()
        payload = dict(row)
        payload["reason_codes"] = json.loads(row["reason_codes_json"] or "[]")
        payload["items"] = [
            {
                **dict(item),
                "categories": json.loads(item["categories_json"] or "[]"),
                "baseline": json.loads(item["baseline_json"] or "{}"),
                "current": json.loads(item["current_json"] or "{}"),
                "baseline_sources": json.loads(item["baseline_sources_json"] or "[]"),
                "current_sources": json.loads(item["current_sources_json"] or "[]"),
            }
            for item in item_rows
        ]
        result.append(payload)
    return result


__all__ = [
    "CATEGORY_LABELS",
    "CATEGORIES",
    "DiffItem",
    "DiffRadar",
    "build_diff_radar",
    "persist_diff_radar",
    "read_current_diff_radar",
]
