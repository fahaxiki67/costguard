"""结算累计引擎（Phase 3）。

职责：按期次分组累计 → 项目级累计 → 加权平均单价。
纪律：
- 全 Decimal；缺失值不补 0：某清单任一期缺失/不可解析 → 该清单累计状态
  标记 'incomplete'（待补资料），金额累计仍如实累加可得部分，但给出警示；
- 小计行（flags.subtotal）永不参与累计；
- item 归组采用"同编码或同名精确匹配"（Phase 4 的模糊归组在此基础上提级，
  任何归组差异都保留明细行，不做合并改写）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from jiadun.core.anomalies.rules import _norm_unit
from jiadun.core.contracts import run_contract
from jiadun.core.engine.money import (
    Decimal,
    NotANumberError,
    money_mul,
    to_decimal,
    weighted_avg_price,
)

D = Decimal
AMOUNT_TOL = D("0.02")


@dataclass
class ItemAggregate:
    item_key: str  # 归组键：code 优先；无 code 用 'name:<名称>'
    code: str
    name: str
    feature: str = ""
    unit: str = ""
    # period_id -> raw/calculated/effective amounts；键用 period_id 防止对上/对下同期号串表。
    # amount/raw_amount 保留原始合价；effective_amount 仅用于可复算累计和持久化。
    per_period: dict[int, dict] = field(default_factory=dict)
    cum_qty: Decimal | None = None
    cum_amount: Decimal | None = None  # 有效合价：原始值优先，缺失时使用计算候选
    raw_cum_amount: Decimal | None = None
    calculated_cum_amount: Decimal | None = None  # 所有可计算行的候选合价
    calculated_cum_amount_used: Decimal | None = None  # 原始合价缺失时实际采用的候选
    wavg_price: Decimal | None = None
    status: str = "ok"  # 'ok' | 'incomplete' | 'incomparable'
    warnings: list[str] = field(default_factory=list)
    quantity_required: bool = field(default=False, repr=False)
    amount_source: str = "missing"  # 'raw' | 'calculated' | 'mixed' | 'missing'
    amount_status: str = "missing"  # 'raw' | 'calculated' | 'mixed' | 'missing'
    amount_check_status: str = "not_available"  # 'match' | 'diff' | 'not_available'


@dataclass(frozen=True)
class AmountAssessment:
    """单行金额的原始值、计算候选和实际采用值。"""

    raw: Decimal | None
    calculated: Decimal | None
    effective: Decimal | None
    source: str
    status: str
    check_status: str
    difference: Decimal | None


def _dec_or_none(raw) -> tuple[Decimal | None, bool]:
    """(可解析值, 是否缺失)。None 值或不可解析都算缺失，绝不补 0。"""
    if raw is None or raw == "":
        return None, True
    try:
        return to_decimal(raw), False
    except NotANumberError:
        return None, True


def assess_amount(row: sqlite3.Row) -> tuple[AmountAssessment, bool, bool]:
    """评估一行金额，返回 ``(金额评估, 数量缺失, 单价缺失)``。

    ``line_items.amount`` 永远只代表原始合价；当原始合价缺失且数量、单价
    均可解析时，计算候选只进入 flags/聚合结果，不回写原始列。
    """
    quantity, quantity_missing = _dec_or_none(row["quantity"])
    unit_price, price_missing = _dec_or_none(row["unit_price"])
    raw, _amount_missing = _dec_or_none(row["amount"])
    calculated = money_mul(quantity, unit_price) if quantity is not None and unit_price is not None else None

    if raw is not None:
        effective = raw
        source = "raw"
        status = "raw"
    elif calculated is not None:
        effective = calculated
        source = "calculated"
        status = "calculated"
    else:
        effective = None
        source = "missing"
        status = "missing"

    if raw is not None and calculated is not None:
        difference = raw - calculated
        check_status = "match" if abs(difference) <= AMOUNT_TOL else "diff"
    else:
        difference = None
        check_status = "not_available"
    return (
        AmountAssessment(raw, calculated, effective, source, status, check_status, difference),
        quantity_missing,
        price_missing,
    )


def _merge_source(current: str, incoming: str) -> str:
    """合并有效金额来源；缺失来源不覆盖已有有效来源。"""
    if incoming == "missing":
        return current
    if current in ("missing", incoming):
        return incoming
    return "mixed"


def _merge_check_status(current: str, incoming: str) -> str:
    if incoming == "diff" or current == "diff":
        return "diff"
    if incoming == "match" or current == "match":
        return "match"
    return "not_available"


def _update_amount_flags(flags: dict, assessment: AmountAssessment) -> dict:
    """在既有 flags 上保存来源、状态和计算依据，不改写原始金额列。"""
    updated = dict(flags)
    updated["amount_source"] = assessment.source
    updated["amount_status"] = assessment.status
    updated["amount_check_status"] = assessment.check_status
    if assessment.calculated is not None:
        updated["calculated_amount"] = str(assessment.calculated)
        updated["calculated_amount_source"] = "quantity*unit_price"
    else:
        updated.pop("calculated_amount", None)
        updated.pop("calculated_amount_source", None)
    if assessment.difference is not None:
        updated["amount_check"] = {
            "formula": "quantity*unit_price",
            "status": assessment.check_status,
            "difference": str(assessment.difference),
        }
    else:
        updated.pop("amount_check", None)
    return updated


def _add_decimal(current: Decimal | None, value: Decimal | None) -> Decimal | None:
    if value is None:
        return current
    return value if current is None else current + value


def group_key_of(code: str | None, name: str, feature: str | None = "", unit: str | None = "") -> str:
    """聚合归组键：code/归一名称 + 归一特征 + 归一单位。

    单位必须参与归组：m² 与 m³ 即便同名同码也是不同计量口径，绝不允许进入
    同一累计（不同单位不可比，禁止跨单位求和）。特征参与归组：同名称不同
    特征的清单行语义不同，也不得合并。名称归一与 matching.normalize_name
    同口径（去空白/标点、全角转半角、小写），保证"水泥砂浆 楼地面"与
    "水泥砂浆楼地面"聚合为同一清单。
    """
    from jiadun.core.matching.matching import normalize_name

    feature_part = normalize_name(feature or "")
    unit_part = _norm_unit(unit or "")
    if code:
        return f"code:{code}|f:{feature_part}|u:{unit_part}"
    return f"name:{normalize_name(name)}|f:{feature_part}|u:{unit_part}"


def load_line_items(conn: sqlite3.Connection, project_id: int,
                    direction: str | None = None) -> list[sqlite3.Row]:
    sql = """SELECT li.*, sp.period_no FROM line_items li
       JOIN settlement_periods sp ON sp.id = li.period_id
       WHERE sp.project_id=?"""
    params: list = [project_id]
    if direction is not None:
        sql += " AND sp.direction=?"
        params.append(direction)
    sql += " ORDER BY sp.period_no, li.id"
    return conn.execute(sql, params).fetchall()


def _confirmed_col_maps(conn: sqlite3.Connection, project_id: int) -> dict[int, dict]:
    rows = conn.execute(
        """SELECT th.sheet_id, th.col_map_json FROM table_headers th
           JOIN raw_sheets rs ON rs.id=th.sheet_id
           JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE sf.project_id=? AND th.needs_review=0""",
        (project_id,),
    ).fetchall()
    return {int(r["sheet_id"]): json.loads(r["col_map_json"]) for r in rows}


def _quantity_is_optional(row: sqlite3.Row, col_maps: dict[int, dict]) -> bool:
    """直接金额型计价与明确税额行不要求虚构数量。"""
    name = (row["name"] or "").strip().replace(" ", "")
    if re.fullmatch(r"(?:税金|税额|税费|增值税)(?:[（(].*[）)])?", name):
        return True
    col_map = col_maps.get(row["sheet_id"])
    return bool(
        col_map and "amount" in col_map
        and not ("quantity" in col_map and "unit_price" in col_map)
    )


def aggregate_project(
    conn: sqlite3.Connection,
    project_id: int,
    direction: str | None = None,
    *,
    include_all_directions: bool = False,
    persist_derived_flags: bool = True,
) -> list[ItemAggregate]:
    """按 item_key 累计项目全部期次。跳过小计行；缺失值不补 0。

    direction 给定时（'upward'/'downward'）只聚合该方向期次——对上/对下
    报表必须各自独立累计，禁止混入另一方向数据。
    direction=None 默认仅允许项目只有一个方向；多方向项目必须显式传入
    ``include_all_directions=True`` 才能生成“全项目展示总量”，该总量不作为
    对上/对下结算结果。
    per_period 以 period_id 为键：对上/对下可存在相同期号，按期号取值会串表。

    漏项检测：某清单未覆盖（同方向）全部期次 → status='incomplete' 并警示，
    绝不静默当作 0 处理。
    """
    if direction is None and not include_all_directions:
        directions = {
            r["direction"] or "unknown"
            for r in conn.execute(
                "SELECT direction FROM settlement_periods WHERE project_id=?",
                (project_id,),
            ).fetchall()
        }
        if len(directions) > 1:
            raise ValueError(
                "project has multiple settlement directions; pass direction or "
                "include_all_directions=True for an explicitly non-settlement total"
            )
    period_meta = {
        int(r["id"]): int(r["period_no"])
        for r in conn.execute(
            "SELECT id, period_no FROM settlement_periods WHERE project_id=?"
            + (" AND direction=?" if direction is not None else ""),
            (project_id, direction) if direction is not None else (project_id,),
        ).fetchall()
    }
    col_maps = _confirmed_col_maps(conn, project_id)
    aggs: dict[str, ItemAggregate] = {}
    flag_updates: list[tuple[str, int]] = []
    for row in load_line_items(conn, project_id, direction=direction):
        flags = json.loads(row["flags_json"] or "{}")
        if flags.get("subtotal"):
            continue
        name = row["name"] or f"<第{row['period_no']}期第{row['id']}行无名称>"
        key = group_key_of(row["code"], name, row["feature"], row["unit"])
        agg = aggs.setdefault(
            key,
            ItemAggregate(
                item_key=key,
                code=row["code"] or "",
                name=name,
                feature=(row["feature"] or "").strip(),
                unit=_norm_unit(row["unit"] or ""),
            ),
        )

        qty, qty_missing = _dec_or_none(row["quantity"])
        amount_assessment, amount_quantity_missing, _price_missing = assess_amount(row)
        amount = amount_assessment.raw
        calculated_amount = amount_assessment.calculated
        effective_amount = amount_assessment.effective
        amount_missing = amount is None
        quantity_optional = _quantity_is_optional(row, col_maps)
        if not quantity_optional:
            agg.quantity_required = True
        pp = agg.per_period.setdefault(
            int(row["period_id"]),
            {
                "qty": None,
                "amount": None,
                "raw_amount": None,
                "calculated_amount": None,
                "calculated_amount_used": None,
                "effective_amount": None,
                "rows": 0,
                "period_no": int(row["period_no"]),
                "amount_source": "missing",
                "amount_status": "missing",
                "amount_check_status": "not_available",
            },
        )
        pp["rows"] += 1
        if qty is not None:
            pp["qty"] = qty if pp["qty"] is None else pp["qty"] + qty
        if amount is not None:
            pp["amount"] = amount if pp["amount"] is None else pp["amount"] + amount
            pp["raw_amount"] = _add_decimal(pp["raw_amount"], amount)
        if calculated_amount is not None:
            pp["calculated_amount"] = _add_decimal(pp["calculated_amount"], calculated_amount)
        if amount_assessment.source == "calculated":
            pp["calculated_amount_used"] = _add_decimal(
                pp["calculated_amount_used"], calculated_amount
            )
        if effective_amount is not None:
            pp["effective_amount"] = _add_decimal(pp["effective_amount"], effective_amount)
            pp["amount_source"] = _merge_source(pp["amount_source"], amount_assessment.source)
        pp["amount_check_status"] = _merge_check_status(
            pp["amount_check_status"], amount_assessment.check_status
        )
        if pp["amount_source"] == "mixed":
            pp["amount_status"] = "mixed"
        elif pp["amount_source"] in ("raw", "calculated"):
            pp["amount_status"] = pp["amount_source"]
        else:
            pp["amount_status"] = "missing"

        updated_flags = _update_amount_flags(flags, amount_assessment)
        if updated_flags != flags:
            flag_updates.append((json.dumps(updated_flags, ensure_ascii=False), int(row["id"])))

        if (amount_quantity_missing and not quantity_optional) or amount_missing:
            if agg.status == "ok":
                agg.status = "incomplete"
            if amount_quantity_missing and not quantity_optional:
                agg.warnings.append(f"第{row['period_no']}期 数量缺失/不可解析（行{row['id']}），未参与累计")
            if amount_missing:
                if calculated_amount is None:
                    agg.warnings.append(
                        f"第{row['period_no']}期 金额缺失/不可解析（行{row['id']}），未参与累计"
                    )
                else:
                    agg.warnings.append(
                        f"第{row['period_no']}期 原始金额缺失（行{row['id']}），"
                        f"使用数量×单价计算合价 {calculated_amount}；原始金额未覆盖"
                    )
        if amount_assessment.check_status == "diff":
            if agg.status == "ok":
                agg.status = "incomplete"
            agg.warnings.append(
                f"第{row['period_no']}期 原始金额与数量×单价计算值差异 "
                f"{amount_assessment.difference}（行{row['id']}），待复核"
            )

    for agg in aggs.values():
        missing_ids = sorted(set(period_meta) - set(agg.per_period))
        for pid in missing_ids:
            agg.status = "incomplete"
            agg.warnings.append(f"第{period_meta[pid]}期无此清单（疑似漏项），未计入累计，待核实")
        periods = sorted(agg.per_period)
        qty_total: Decimal | None = None
        effective_amt_total: Decimal | None = None
        raw_amt_total: Decimal | None = None
        calculated_amt_total: Decimal | None = None
        calculated_amt_used_total: Decimal | None = None
        amount_source = "missing"
        amount_check_status = "not_available"
        for p in periods:
            q = agg.per_period[p]["qty"]
            if q is not None:
                qty_total = q if qty_total is None else qty_total + q
            pp = agg.per_period[p]
            effective_amt_total = _add_decimal(effective_amt_total, pp["effective_amount"])
            raw_amt_total = _add_decimal(raw_amt_total, pp["raw_amount"])
            calculated_amt_total = _add_decimal(calculated_amt_total, pp["calculated_amount"])
            calculated_amt_used_total = _add_decimal(
                calculated_amt_used_total, pp["calculated_amount_used"]
            )
            amount_source = _merge_source(amount_source, pp["amount_source"])
            amount_check_status = _merge_check_status(
                amount_check_status, pp["amount_check_status"]
            )
            if (
                pp["qty"] is not None
                and pp["effective_amount"] is not None
                and pp["qty"] != 0
            ):
                pp["wavg_price"] = weighted_avg_price(pp["effective_amount"], pp["qty"])
            else:
                pp["wavg_price"] = None
        agg.cum_qty = qty_total
        agg.cum_amount = effective_amt_total
        agg.raw_cum_amount = raw_amt_total
        agg.calculated_cum_amount = calculated_amt_total
        agg.calculated_cum_amount_used = calculated_amt_used_total
        agg.amount_source = amount_source
        agg.amount_check_status = amount_check_status
        agg.amount_status = "mixed" if amount_source == "mixed" else amount_source
        if qty_total is not None and effective_amt_total is not None:
            if qty_total == 0:
                agg.status = "incomparable" if agg.status == "ok" else agg.status
                agg.warnings.append("累计数量为 0，加权平均单价不可定义（不可比）")
            else:
                agg.wavg_price = weighted_avg_price(effective_amt_total, qty_total)
        elif effective_amt_total is None or agg.quantity_required:
            agg.status = "incomplete"
    # 同码/同名+同特征存在多个单位桶：键中含单位已经天然分行累计，这里补
    # 显式"不可比"标记，防止 UI/报表把多个单位桶误读为同一清单的多个期次。
    logical_buckets: dict[str, list[ItemAggregate]] = {}
    for agg in aggs.values():
        base = agg.item_key.rsplit("|u:", 1)[0]
        logical_buckets.setdefault(base, []).append(agg)
    for bucket in logical_buckets.values():
        if len(bucket) < 2:
            continue
        unit_parts = sorted(agg.item_key.rsplit("|u:", 1)[1] for agg in bucket)
        for agg in bucket:
            if agg.status == "ok":
                agg.status = "incomparable"
            agg.warnings.append(
                f"同名同特征存在多个单位 {unit_parts}：不同单位不可比，未跨单位汇总"
            )
    if flag_updates and persist_derived_flags:
        with conn:
            conn.executemany("UPDATE line_items SET flags_json=? WHERE id=?", flag_updates)
    return sorted(aggs.values(), key=lambda a: (a.item_key,))


def persist_period_totals(conn: sqlite3.Connection, project_id: int, aggs: list[ItemAggregate],
                          evidence_id: int | None = None) -> int:
    previous_contract = run_contract.get_current_contract(conn, project_id)
    n = 0
    period_ids = sorted({int(pid) for agg in aggs for pid in agg.per_period})
    settlement_io = None
    if period_ids:
        # 聚合重算也属于输入/结果边界变化，统一通过结算 IO 的失效化入口
        # 先保存旧校核 Evidence 的历史事件，再刷新 Run Contract。不能只删
        # crosscheck_results，否则旧 Evidence 会继续显示为 current。
        from jiadun.core.engine import settlement_io as settlement_io_module

        settlement_io = settlement_io_module

        settlement_io.invalidate_crosscheck_results(conn, project_id, period_ids)
    active_contract = run_contract.ensure_run_contract(conn, project_id)
    with run_contract._transaction(conn, "persist_period_totals"):
        if settlement_io is not None:
            # aggregate_project 可能只更新了行级派生标记；这会形成新的
            # Run Contract，但不改变原始网格和逐行覆盖事实。失效化已把旧
            # proof Evidence 标为 historical，这里追加同内容的新运行证明，
            # 避免后续 A/B/C 因缺少当前覆盖证明而被错误降级。
            settlement_io._rebind_coverage_proofs_for_run(
                conn,
                project_id,
                previous_contract,
                active_contract,
                reason="聚合派生标记更新；原始网格和逐行覆盖分类未改变",
            )
        for agg in aggs:
            for pid, pp in agg.per_period.items():  # 键即 period_id
                conn.execute(
                    """INSERT INTO period_totals(
                       project_id, period_id, item_key, qty_sum, amount_sum, wavg_price,
                       cross_check_diff, cross_check_status, evidence_id,
                       raw_amount_sum, calculated_amount_sum, calculated_amount_used_sum,
                       effective_amount_sum, amount_source, amount_status, verification_level,
                       run_signature, run_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(period_id, item_key) DO UPDATE SET
                         qty_sum=excluded.qty_sum, amount_sum=excluded.amount_sum,
                         wavg_price=excluded.wavg_price,
                         cross_check_diff=excluded.cross_check_diff,
                         cross_check_status=excluded.cross_check_status,
                         evidence_id=excluded.evidence_id,
                         ab_diff=NULL, ab_status='pending',
                         control_diff=NULL, control_status='not_available',
                         raw_amount_sum=excluded.raw_amount_sum,
                         calculated_amount_sum=excluded.calculated_amount_sum,
                         calculated_amount_used_sum=excluded.calculated_amount_used_sum,
                         effective_amount_sum=excluded.effective_amount_sum,
                         amount_source=excluded.amount_source,
                         amount_status=excluded.amount_status,
                         verification_level='insufficient',
                         run_signature=excluded.run_signature,
                         run_id=excluded.run_id""",
                    (project_id, pid, agg.item_key,
                     str(pp["qty"]) if pp["qty"] is not None else None,
                     str(pp["effective_amount"]) if pp["effective_amount"] is not None else None,
                     str(pp["wavg_price"]) if pp["wavg_price"] is not None else None,
                     None, "pending", evidence_id,
                     str(pp["raw_amount"]) if pp["raw_amount"] is not None else None,
                     str(pp["calculated_amount"]) if pp["calculated_amount"] is not None else None,
                     (str(pp["calculated_amount_used"])
                      if pp["calculated_amount_used"] is not None else None),
                     (str(pp["effective_amount"])
                      if pp["effective_amount"] is not None else None),
                     pp["amount_source"], pp["amount_status"], "insufficient",
                     active_contract.signature, active_contract.run_id),
                )
                n += 1
    return n
