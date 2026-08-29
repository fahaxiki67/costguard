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
import sqlite3
from dataclasses import dataclass, field

from costguard.core.engine.money import Decimal, NotANumberError, to_decimal, weighted_avg_price

D = Decimal


@dataclass
class ItemAggregate:
    item_key: str  # 归组键：code 优先；无 code 用 'name:<名称>'
    code: str
    name: str
    per_period: dict[int, dict] = field(default_factory=dict)  # period_no -> {qty, amount, rows}
    cum_qty: Decimal | None = None
    cum_amount: Decimal | None = None
    wavg_price: Decimal | None = None
    status: str = "ok"  # 'ok' | 'incomplete' | 'incomparable'
    warnings: list[str] = field(default_factory=list)


def _dec_or_none(raw: str | None) -> tuple[Decimal | None, bool]:
    """(可解析值, 是否缺失)。None 值或不可解析都算缺失，绝不补 0。"""
    if raw is None or raw == "":
        return None, True
    try:
        return to_decimal(raw), False
    except NotANumberError:
        return None, True


def group_key_of(code: str | None, name: str) -> str:
    if code:
        return f"code:{code}"
    return f"name:{name}"


def load_line_items(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT li.*, sp.period_no FROM line_items li
           JOIN settlement_periods sp ON sp.id = li.period_id
           WHERE sp.project_id=? ORDER BY sp.period_no, li.id""",
        (project_id,),
    ).fetchall()


def aggregate_project(conn: sqlite3.Connection, project_id: int) -> list[ItemAggregate]:
    """按 item_key 累计项目全部期次。跳过小计行；缺失值不补 0。

    漏项检测：某清单未覆盖项目全部期号 → status='incomplete' 并警示，
    绝不静默当作 0 处理。
    """
    all_periods = {
        int(r["period_no"])
        for r in conn.execute(
            "SELECT period_no FROM settlement_periods WHERE project_id=?", (project_id,)
        ).fetchall()
    }
    aggs: dict[str, ItemAggregate] = {}
    for row in load_line_items(conn, project_id):
        flags = json.loads(row["flags_json"] or "{}")
        if flags.get("subtotal"):
            continue
        name = row["name"] or f"<第{row['period_no']}期第{row['id']}行无名称>"
        key = group_key_of(row["code"], name)
        agg = aggs.setdefault(key, ItemAggregate(item_key=key, code=row["code"] or "", name=name))

        qty, qty_missing = _dec_or_none(row["quantity"])
        amount, amount_missing = _dec_or_none(row["amount"])
        pp = agg.per_period.setdefault(int(row["period_no"]), {"qty": None, "amount": None, "rows": 0})
        pp["rows"] += 1
        if qty is not None:
            pp["qty"] = qty if pp["qty"] is None else pp["qty"] + qty
        if amount is not None:
            pp["amount"] = amount if pp["amount"] is None else pp["amount"] + amount
        if qty_missing or amount_missing:
            if agg.status == "ok":
                agg.status = "incomplete"
            if qty_missing:
                agg.warnings.append(f"第{row['period_no']}期 数量缺失/不可解析（行{row['id']}），未参与累计")
            if amount_missing:
                agg.warnings.append(f"第{row['period_no']}期 金额缺失/不可解析（行{row['id']}），未参与累计")

    for agg in aggs.values():
        missing_periods = sorted(all_periods - set(agg.per_period))
        for p in missing_periods:
            agg.status = "incomplete"
            agg.warnings.append(f"第{p}期无此清单（疑似漏项），未计入累计，待核实")
        periods = sorted(agg.per_period)
        qty_total: Decimal | None = None
        amt_total: Decimal | None = None
        for p in periods:
            q = agg.per_period[p]["qty"]
            a = agg.per_period[p]["amount"]
            if q is not None:
                qty_total = q if qty_total is None else qty_total + q
            if a is not None:
                amt_total = a if amt_total is None else amt_total + a
        agg.cum_qty = qty_total
        agg.cum_amount = amt_total
        if qty_total is not None and amt_total is not None:
            if qty_total == 0:
                agg.status = "incomparable" if agg.status == "ok" else agg.status
                agg.warnings.append("累计数量为 0，加权平均单价不可定义（不可比）")
            else:
                agg.wavg_price = weighted_avg_price(amt_total, qty_total)
        else:
            agg.status = "incomplete"
    return sorted(aggs.values(), key=lambda a: (a.item_key,))


def persist_period_totals(conn: sqlite3.Connection, project_id: int, aggs: list[ItemAggregate],
                          evidence_id: int | None = None) -> int:
    n = 0
    pmap = {
        int(r["period_no"]): int(r["id"])
        for r in conn.execute(
            "SELECT id, period_no FROM settlement_periods WHERE project_id=?", (project_id,)
        ).fetchall()
    }
    with conn:
        for agg in aggs:
            for pno, pp in agg.per_period.items():
                pid = pmap.get(pno)
                if pid is None:
                    continue
                conn.execute(
                    """INSERT INTO period_totals(project_id, period_id, item_key, qty_sum, amount_sum,
                       wavg_price, cross_check_diff, cross_check_status, evidence_id)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(period_id, item_key) DO UPDATE SET
                         qty_sum=excluded.qty_sum, amount_sum=excluded.amount_sum,
                         wavg_price=excluded.wavg_price, cross_check_status=excluded.cross_check_status,
                         evidence_id=excluded.evidence_id""",
                    (project_id, pid, agg.item_key,
                     str(pp["qty"]) if pp["qty"] is not None else None,
                     str(pp["amount"]) if pp["amount"] is not None else None,
                     None, None, "pending", evidence_id),
                )
                n += 1
    return n
