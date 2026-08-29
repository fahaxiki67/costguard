"""成果导出（Phase 7）：Excel 审核底稿与各类汇总表。

WPS 兼容纪律：
- 只用 openpyxl 基础特性（值/公式/列宽/数字格式），不使用条件 XML 扩展；
- 金额格式 "#,##0.00"；审核底稿保留公式（合价=数量×单价、差异=对比列）；
- 全部导出写入 <project>/exports/，绝不写原始文件（原则 2/3）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from costguard.core.engine.aggregate import aggregate_project, group_key_of
from costguard.core.engine.money import NotANumberError, round2, to_decimal

D = Decimal

HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
HEADER_FONT = Font(bold=True)
MONEY_FMT = "#,##0.00"


def _style_header(ws, row: int, cols: int):
    for c in range(1, cols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")


def _autowidth(ws):
    for col in ws.columns:
        width = max((len(str(c.value or "")) for c in col), default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(width + 2, 8), 60)


# openpyxl 原生支持 Decimal（XML 层写精确十进制字符串），当前序列化无需 float。
# 若未来更换写入引擎导致必须经 float，本常量置 True：金额先 round2 再转 float，
# 且转换只允许发生在 _num 这一个边界（监督门槛 4）。
_SERIALIZE_NEEDS_FLOAT = False


def _num(value, money: bool = False):
    """序列化边界：业务值 → xlsx 数值。全模块唯一的"值 → 单元格"转换点。

    - Decimal 直写，XML 层为精确十进制字符串，无二进制误差，业务值保真
      （原表合价等证据值不得因序列化改变）；
    - money=True 仅为金额列语义标记：若 _SERIALIZE_NEEDS_FLOAT（必须转 float 的
      写入引擎），则先 round2 再转 float；
    - 缺失/不可解析一律 None（待补资料），绝不补 0；
    - 禁止在业务计算层做任何 float 转换。
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, str):
        try:
            d = to_decimal(value)
        except NotANumberError:
            return None
    else:
        d = Decimal(repr(value))
    if _SERIALIZE_NEEDS_FLOAT:
        return float(round2(d)) if money else float(d)
    return d


def _fetch_periods(conn, project_id):
    return conn.execute(
        "SELECT id, period_no, title, direction, contract_party, tax_mode FROM settlement_periods"
        " WHERE project_id=? ORDER BY period_no",
        (project_id,),
    ).fetchall()


def export_settlement_summary(conn: sqlite3.Connection, project_id: int, wb: Workbook,
                              direction: str | None = None) -> str:
    """结算累计表（对上/对下各自独立累计，绝不混入另一方向数据）。"""
    periods = [p for p in _fetch_periods(conn, project_id) if direction in (None, p["direction"])]
    aggs = aggregate_project(conn, project_id, direction=direction)
    ws = wb.create_sheet("对上结算累计表" if direction == "upward" else
                         "对下结算累计表" if direction == "downward" else "结算累计表")
    header = ["清单编码", "清单名称", "单位"] + [f"第{p['period_no']}期金额" for p in periods] + \
             ["累计数量", "累计金额", "加权平均单价", "状态"]
    ws.append(header)
    _style_header(ws, 1, len(header))
    for agg in aggs:
        row = [agg.code, agg.name, ""]
        for p in periods:
            pp = agg.per_period.get(p["id"])  # period_id 键：防对上/对下同期号串表
            row.append(_num(pp["amount"], money=True) if pp else None)
        row.append(_num(agg.cum_qty))
        row.append(_num(agg.cum_amount, money=True))
        row.append(_num(agg.wavg_price, money=True))
        status = {"ok": "正常", "incomplete": "待补资料", "incomparable": "不可比"}[agg.status]
        row.append(status + ("；".join(agg.warnings[:2]) if agg.warnings else ""))
        ws.append(row)
    _autowidth(ws)
    n = ws.max_row
    if n > 1:
        for r in range(2, n + 1):
            for c in range(4, len(header)):
                ws.cell(row=r, column=c).number_format = MONEY_FMT
    return ws.title


def _diff_series(conn: sqlite3.Connection, project_id: int, field: str) -> list[dict]:
    """差异表数据序列：按 (direction, period_id, item_key) 取可追溯原始值。

    - 数量/金额：同期同组多行求和（Decimal）；单位不一致时该期标记不可比；
    - 单价：同期同组若存在多个不同价 → 标记"多价待复核"，绝不平均；
    - 缺失值不补 0：整期无有效值 → None（待补资料）。
    """
    rows = conn.execute(
        """SELECT li.code, li.name, li.unit, li.quantity, li.unit_price, li.amount,
                  li.flags_json, sp.period_no AS pno, sp.direction AS dir
           FROM line_items li JOIN settlement_periods sp ON sp.id = li.period_id
           WHERE sp.project_id=? ORDER BY sp.period_no, li.id""",
        (project_id,),
    ).fetchall()
    series: dict[tuple[str, str, str], dict[int, dict]] = {}
    for r in rows:
        flags = json.loads(r["flags_json"] or "{}")
        if flags.get("subtotal"):
            continue
        key = (r["dir"] or "unknown", "code:" + r["code"] if r["code"] else "name:" + (r["name"] or ""),
               r["name"] or "")
        by_period = series.setdefault(key, {})
        pp = by_period.setdefault(int(r["pno"]), {
            "qty": None, "amount": None, "prices": set(), "units": set(), "qty_missing": False,
            "period_no": int(r["pno"]),
        })
        if r["quantity"] is not None:
            try:
                q = D(r["quantity"])
            except Exception:
                q = None
            if q is not None:
                pp["qty"] = q if pp["qty"] is None else pp["qty"] + q
        else:
            pp["qty_missing"] = True
        if r["amount"] is not None:
            try:
                a = D(r["amount"])
            except Exception:
                a = None
            if a is not None:
                pp["amount"] = a if pp["amount"] is None else pp["amount"] + a
        if r["unit_price"]:
            try:
                pp["prices"].add(D(r["unit_price"]))
            except Exception:
                pass
        if r["unit"]:
            pp["units"].add(_norm_unit_local(r["unit"]))
    out = []
    for (direction, _key, name), by_period in series.items():
        code = _key[5:] if _key.startswith("code:") else ""
        out.append({"direction": direction, "code": code, "name": name, "by_period": by_period})
    return sorted(out, key=lambda x: (x["direction"], x["code"], x["name"]))


def _norm_unit_local(u: str | None) -> str:
    if not u:
        return ""
    u = u.strip()
    for canonical, aliases in UNIT_ALIAS_EXPORT.items():
        if u == canonical or u in aliases:
            return canonical
    return u


UNIT_ALIAS_EXPORT = {
    "m3": {"m³", "立方米", "立米", "M3", "m^3"},
    "m2": {"m²", "平方米", "平米", "M2", "m^2"},
}


def export_diff_sheets(conn: sqlite3.Connection, project_id: int, wb: Workbook) -> None:
    """单价/工程量/金额 差异表：方向隔离的跨期比较。

    - 按 direction 分组，同方向内按 period_no 比较；对上与对下绝不互比；
    - 每行输出方向列；
    - 单价来自可追溯原始 unit_price；同期同组多价 → 标"多价待复核"，不得平均；
    - 差异列保留公式；一侧缺失 → 标待补资料，不补 0。
    """
    titles = (("单价差异表", "unit_price"), ("工程量差异表", "quantity"), ("金额差异表", "amount"))
    dir_zh = {"upward": "对上", "downward": "对下", "unknown": "未标记"}
    all_series = {f: _diff_series(conn, project_id, f) for _t, f in titles}

    for title, field in titles:
        ws = wb.create_sheet(title)
        ws.append(["方向", "清单编码", "清单名称", "期间", "本期值", "上期值", "差异", "差异率"])
        _style_header(ws, 1, 8)
        series = all_series[field]
        r = 1
        for item in series:
            # 键为 period_id，按 period_no 排序比较
            ordered = sorted(item["by_period"].items(), key=lambda kv: kv[1]["period_no"])
            prev = None
            for _pid, pp in ordered:
                r += 1
                pno = pp["period_no"]
                if field == "unit_price":
                    if len(pp["prices"]) > 1:
                        cur_val = "多价待复核（不可比，不平均）"
                    else:
                        cur_val = _num(next(iter(pp["prices"])), money=True) if pp["prices"] else None
                elif field == "quantity":
                    cur_val = _num(pp["qty"]) if pp["qty"] is not None else None
                    if len(pp["units"]) > 1:
                        cur_val = "单位不一致（不可比）"
                else:
                    cur_val = _num(pp["amount"], money=True) if pp["amount"] is not None else None
                ws.cell(row=r, column=1, value=dir_zh.get(item["direction"], item["direction"]))
                ws.cell(row=r, column=2, value=item["code"])
                ws.cell(row=r, column=3, value=item["name"])
                ws.cell(row=r, column=4, value=f"第{pno}期")
                ws.cell(row=r, column=5, value=cur_val)
                if prev is None:
                    ws.cell(row=r, column=8, value="首期（无上期可比）")
                else:
                    if isinstance(cur_val, (int, float, Decimal)) and isinstance(prev, (int, float, Decimal)):
                        ws.cell(row=r, column=6, value=prev)
                        ws.cell(row=r, column=7, value=f"=E{r}-F{r}")
                        if prev != 0:
                            ws.cell(row=r, column=8, value=f'=IF(F{r}=0,"不可比",(E{r}-F{r})/F{r})')
                            ws.cell(row=r, column=8).number_format = "0.00%"
                        else:
                            ws.cell(row=r, column=8, value="不可比（上期为 0）")
                    else:
                        ws.cell(row=r, column=6, value=prev if isinstance(prev, str) else None)
                        ws.cell(row=r, column=7, value="待补资料")
                        ws.cell(row=r, column=8, value="不可比")
                if isinstance(cur_val, (int, float, Decimal)):
                    ws.cell(row=r, column=5).number_format = MONEY_FMT
                for c in (6, 7):
                    ws.cell(row=r, column=c).number_format = MONEY_FMT
                if isinstance(cur_val, (int, float, Decimal)):
                    prev = cur_val
                # 不可比/缺失期不作为比较基准（防污染下一期差异）
                elif isinstance(cur_val, str) and "多价" not in cur_val and "不可比" not in cur_val:
                    prev = None
        _autowidth(ws)


def _aggregate_by_direction(conn: sqlite3.Connection, project_id: int, direction: str) -> dict[str, dict]:
    """按方向聚合：item_key -> {qty, amount, names}。缺失值不补 0。"""
    rows = conn.execute(
        """SELECT li.id, li.code, li.name, li.unit, li.quantity, li.amount, li.flags_json FROM line_items li
           JOIN settlement_periods sp ON sp.id = li.period_id
           WHERE sp.project_id=? AND sp.direction=?""",
        (project_id, direction),
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        flags = json.loads(r["flags_json"] or "{}")
        if flags.get("subtotal"):
            continue
        key = group_key_of(r["code"], r["name"] or "")
        agg = out.setdefault(key, {"qty": None, "amount": None, "names": set()})
        agg["names"].add(r["name"] or "")
        for field, val in (("qty", r["quantity"]), ("amount", r["amount"])):
            if val is not None:  # "0" 是有效值参与累计；缺失(None)不参与也不补 0
                try:
                    d = D(val)
                except Exception:
                    continue
                agg[field] = d if agg[field] is None else agg[field] + d
    return out


def export_updown_comparison(conn: sqlite3.Connection, project_id: int, wb: Workbook) -> None:
    """对上对下对比表：同一清单的对上累计 vs 对下累计（差异列为公式）。"""
    ws = wb.create_sheet("对上对下对比表")
    ws.append(["清单编码", "清单名称", "对上累计数量", "对下累计数量", "对上累计金额",
               "对下累计金额", "金额差异(公式)", "口径说明"])
    _style_header(ws, 1, 8)
    up = _aggregate_by_direction(conn, project_id, "upward")
    down = _aggregate_by_direction(conn, project_id, "downward")
    if not up and not down:
        ws.append(["", "无可比数据：请先在期次管理中标记对上/对下方向", None, None, None, None, None,
                   "期次方向未标记时无法对比（不可比，不做强行比较）"])
    keys = sorted(set(up) | set(down))
    r = 1
    for key in keys:
        r += 1
        u, d = up.get(key), down.get(key)
        code = key[5:] if key.startswith("code:") else ""
        names = sorted((u or d or {}).get("names") or {""})
        ws.cell(row=r, column=1, value=code)
        ws.cell(row=r, column=2, value=names[0])
        if u and u["qty"] is not None:
            ws.cell(row=r, column=3, value=_num(u["qty"]))
        if d and d["qty"] is not None:
            ws.cell(row=r, column=4, value=_num(d["qty"]))
        if u and u["amount"] is not None:
            ws.cell(row=r, column=5, value=_num(u["amount"], money=True))
        if d and d["amount"] is not None:
            ws.cell(row=r, column=6, value=_num(d["amount"], money=True))
        # 差异公式：两侧都有值才算（一侧缺失标记待补资料，不补 0）
        if u and d and u["amount"] is not None and d["amount"] is not None:
            ws.cell(row=r, column=7, value=f"=E{r}-F{r}")
            ws.cell(row=r, column=7).number_format = MONEY_FMT
        else:
            ws.cell(row=r, column=8, value="待补资料（一侧缺失，不做比较）")
        if u and d and set(map(str, u["names"])) != set(map(str, d["names"])):
            ws.cell(row=r, column=8, value="两侧名称不一致，请核实归组")
        for c in (3, 4, 5, 6):
            ws.cell(row=r, column=c).number_format = MONEY_FMT
    _autowidth(ws)


def export_anomaly_lists(conn: sqlite3.Connection, project_id: int, wb: Workbook) -> None:
    """异常清单 + 待核实事项清单。"""
    ws = wb.create_sheet("异常清单")
    ws.append(["编号", "规则", "级别", "对象", "说明", "证据ID", "状态"])
    _style_header(ws, 1, 7)
    for r in conn.execute(
        """SELECT a.id, a.rule_id, a.severity, a.subject_type, a.subject_id, a.evidence_id, a.message, a.status
           FROM anomalies a WHERE a.project_id=? ORDER BY CASE a.severity
           WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END, a.id""",
        (project_id,),
    ):
        sev = {"high": "高", "medium": "中", "low": "低", "info": "提示"}.get(r["severity"], r["severity"])
        ws.append([r["id"], r["rule_id"], sev, f"{r['subject_type']}#{r['subject_id']}",
                   r["message"], r["evidence_id"], r["status"]])
    _autowidth(ws)

    ws2 = wb.create_sheet("待核实事项清单")
    ws2.append(["编号", "类别", "说明", "证据ID"])
    _style_header(ws2, 1, 4)
    idx = 1
    for r in conn.execute(
        """SELECT id, rule_id, message, evidence_id FROM anomalies
           WHERE project_id=? AND severity IN ('high','medium') AND status='open'""",
        (project_id,),
    ):
        ws2.append([idx, r["rule_id"], r["message"], r["evidence_id"]])
        idx += 1
    for agg in aggregate_project(conn, project_id):
        if agg.status in ("incomplete", "incomparable"):
            for w in agg.warnings:
                ws2.append([idx, "aggregate", f"「{agg.name}」{w}", None])
                idx += 1
    _autowidth(ws2)


def export_contract_risks(conn: sqlite3.Connection, project_id: int, wb: Workbook) -> None:
    ws = wb.create_sheet("合同风险清单")
    ws.append(["编号", "级别", "合同文档", "风险说明", "证据ID"])
    _style_header(ws, 1, 5)
    rows = conn.execute(
        """SELECT a.id, a.severity, a.message, a.evidence_id, cd.title
           FROM anomalies a JOIN contract_docs cd ON cd.id = a.subject_id
           WHERE a.project_id=? AND a.rule_id='contract_risk'""",
        (project_id,),
    ).fetchall()
    for i, r in enumerate(rows, start=1):
        sev = {"high": "高", "medium": "中", "low": "低"}.get(r["severity"], r["severity"])
        ws.append([i, sev, r["title"], r["message"], r["evidence_id"]])
    _autowidth(ws)


def export_evidence_index(conn: sqlite3.Connection, project_id: int, wb: Workbook) -> None:
    ws = wb.create_sheet("证据索引")
    ws.append(["证据ID", "类型", "摘要", "计算过程(JSON)", "来源(JSON)", "生成时间"])
    _style_header(ws, 1, 6)
    for r in conn.execute(
        "SELECT id, kind, summary, steps_json, sources_json, created_at FROM evidence WHERE project_id=? ORDER BY id",
        (project_id,),
    ):
        ws.append([r["id"], r["kind"], r["summary"], r["steps_json"], r["sources_json"], r["created_at"]])
    _autowidth(ws)


def export_audit_worksheet(conn: sqlite3.Connection, project_id: int, wb: Workbook) -> None:
    """Excel 审核底稿：逐行明细 + 出处 + 保留公式的校验列。"""
    ws = wb.create_sheet("审核底稿")
    ws.append(["期次", "清单编码", "清单名称", "单位", "数量", "单价", "合价(底稿公式)",
               "原表合价", "差异(公式)", "数量出处", "单价出处", "合价出处", "行ID"])
    _style_header(ws, 1, 13)
    rows = conn.execute(
        """SELECT li.*, sp.period_no AS pno FROM line_items li
           JOIN settlement_periods sp ON sp.id = li.period_id
           WHERE sp.project_id=? ORDER BY sp.period_no, li.id""",
        (project_id,),
    ).fetchall()
    r = 1
    for row in rows:
        flags = json.loads(row["flags_json"] or "{}")
        if flags.get("subtotal"):
            continue
        r += 1
        # 缺失仅按 is None 判断：Decimal("0") 是有效值必须保留（监督门槛 1）
        qty = _num(row["quantity"]) if row["quantity"] is not None else None
        price = _num(row["unit_price"], money=True) if row["unit_price"] is not None else None
        amount = _num(row["amount"], money=True) if row["amount"] is not None else None
        ws.cell(row=r, column=1, value=row["pno"])
        ws.cell(row=r, column=2, value=row["code"])
        ws.cell(row=r, column=3, value=row["name"])
        ws.cell(row=r, column=4, value=row["unit"])
        ws.cell(row=r, column=5, value=qty)
        ws.cell(row=r, column=6, value=price)
        if qty is not None and price is not None:
            ws.cell(row=r, column=7, value=f"=E{r}*F{r}")  # 保留公式
        else:
            ws.cell(row=r, column=7, value="待补资料")
        ws.cell(row=r, column=8, value=amount)
        if qty is not None and price is not None and amount is not None:
            ws.cell(row=r, column=9, value=f"=ROUND(G{r}-H{r},2)")
        else:
            ws.cell(row=r, column=9, value="不可比")
        ws.cell(row=r, column=13, value=row["id"])  # 行ID：唯一标识，复核回溯用
        for col, evid in ((10, "qty_evid"), (11, "price_evid"), (12, "amount_evid")):
            ev = row[evid]
            if ev:
                e = json.loads(ev)
                ws.cell(row=r, column=col, value=f"行{e['row']}列{e['col']}: {e['raw'][:30]}")
        for c in (5, 6, 7, 8, 9):
            ws.cell(row=r, column=c).number_format = MONEY_FMT
    _autowidth(ws)


def _tax_mode_status(conn: sqlite3.Connection, project_id: int) -> str:
    """各期单价含税口径状态（供摘要声明范围）。"""
    modes: dict[str, set] = {}
    for h in conn.execute(
        """SELECT th.col_map_json, th.header_row_lo, th.header_row_hi, rs.id AS sid
           FROM table_headers th JOIN raw_sheets rs ON rs.id = th.sheet_id
           JOIN settlement_periods sp ON sp.id = rs.period_id WHERE sp.project_id=?""",
        (project_id,),
    ):
        col_map = json.loads(h["col_map_json"])
        if "unit_price" not in col_map:
            continue
        texts = [r["raw_value"] or "" for r in conn.execute(
            "SELECT raw_value FROM raw_cells WHERE sheet_id=? AND row BETWEEN ? AND ?",
            (h["sid"], h["header_row_lo"], h["header_row_hi"]),
        )]
        joined = "".join(texts)
        if "不含税" in joined:
            modes.setdefault("excl_tax", set()).add(h["sid"])
        elif "含税" in joined:
            modes.setdefault("incl_tax", set()).add(h["sid"])
    if not modes:
        return "未识别（表头未标注含税/不含税，请人工确认）"
    if len(modes) > 1:
        return "混用（存在含税与不含税口径，详见异常清单 tax_mode_mixed）"
    return "含税单价" if "incl_tax" in modes else "不含税单价"


def export_management_summary(conn: sqlite3.Connection, project_id: int, wb: Workbook) -> None:
    """管理层摘要 sheet：范围、期次与方向、税口径、异常与证据计数。"""
    ws = wb.create_sheet("管理层摘要")
    ws.append(["CostGuard 管理层摘要"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    aggs = aggregate_project(conn, project_id)
    total = sum((a.cum_amount for a in aggs if a.cum_amount is not None), D(0))
    n_ok = sum(1 for a in aggs if a.status == "ok")
    n_inc = sum(1 for a in aggs if a.status == "incomplete")
    n_inc2 = sum(1 for a in aggs if a.status == "incomparable")
    sev = {"high": 0, "medium": 0, "low": 0}
    for r in conn.execute("SELECT severity, COUNT(*) c FROM anomalies WHERE project_id=? GROUP BY severity", (project_id,)):
        sev[r["severity"]] = r["c"]
    periods = _fetch_periods(conn, project_id)
    n_up = sum(1 for p in periods if p["direction"] == "upward")
    n_down = sum(1 for p in periods if p["direction"] == "downward")
    n_none = len(periods) - n_up - n_down
    n_ev = conn.execute("SELECT COUNT(*) c FROM evidence WHERE project_id=?", (project_id,)).fetchone()["c"]
    n_src = conn.execute("SELECT COUNT(*) c FROM source_files WHERE project_id=?", (project_id,)).fetchone()["c"]
    data = [
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("—— 统计范围 ——", ""),
        ("已导入原始文件数", n_src),
        ("期次数", len(periods)),
        ("其中：对上", n_up),
        ("其中：对下", n_down),
        ("其中：未标记方向", n_none),
        ("清单组数", len(aggs)),
        ("—— 金额与状态 ——", ""),
        ("累计金额合计（可用部分）", _num(round2(total))),
        ("正常清单组", n_ok),
        ("待补资料清单组", n_inc),
        ("不可比清单组", n_inc2),
        ("—— 口径与风险 ——", ""),
        ("单价税口径", _tax_mode_status(conn, project_id)),
        ("高风险异常数", sev["high"]),
        ("中风险异常数", sev["medium"]),
        ("低风险异常数", sev["low"]),
        ("—— 追溯 ——", ""),
        ("证据记录数", n_ev),
        ("证据索引位置", "本工作簿《证据索引》工作表（evidence ID）"),
    ]
    for k, v in data:
        ws.append([k, v])
    ws.append([])
    ws.append(["说明：本摘要由 CostGuard 自动生成，仅反映已导入数据。缺失数据未补 0；"
               "不可比数据未强行比较；方向未标记的期次仅计入未分离汇总，请先标记对上/对下。"])
    ws.append(["合成测试数据仅用于软件测试验证，不构成任何真实业务结论。"])
    _autowidth(ws)


def export_workbook(conn: sqlite3.Connection, project_id: int, out_dir: Path) -> Path:
    """导出全部报表到一个 xlsx。返回文件路径。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)
    export_management_summary(conn, project_id, wb)
    export_settlement_summary(conn, project_id, wb)
    export_updown_comparison(conn, project_id, wb)
    export_diff_sheets(conn, project_id, wb)
    export_anomaly_lists(conn, project_id, wb)
    export_contract_risks(conn, project_id, wb)
    export_evidence_index(conn, project_id, wb)
    export_audit_worksheet(conn, project_id, wb)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"CostGuard审核底稿_{stamp}.xlsx"
    wb.save(path)
    return path


def export_management_summary_docx(conn: sqlite3.Connection, project_id: int, out_dir: Path) -> Path:
    """管理层摘要 Word 版。"""
    import docx as docx_lib

    aggs = aggregate_project(conn, project_id)
    total = sum((a.cum_amount for a in aggs if a.cum_amount is not None), D(0))
    high = conn.execute(
        "SELECT COUNT(*) c FROM anomalies WHERE project_id=? AND severity='high'", (project_id,)
    ).fetchone()["c"]
    periods = _fetch_periods(conn, project_id)
    n_up = sum(1 for p in periods if p["direction"] == "upward")
    n_down = sum(1 for p in periods if p["direction"] == "downward")
    n_none = len(periods) - n_up - n_down
    n_ev = conn.execute("SELECT COUNT(*) c FROM evidence WHERE project_id=?", (project_id,)).fetchone()["c"]
    doc = docx_lib.Document()
    doc.add_heading("CostGuard 管理层摘要", level=0)
    doc.add_paragraph(
        f"截至 {datetime.now().strftime('%Y-%m-%d %H:%M')}，共识别清单组 {len(aggs)} 组，"
        f"累计金额（可用部分）{round2(total)} 元，高风险异常 {high} 项。"
    )
    doc.add_paragraph(
        f"统计范围：期次 {len(periods)} 期（对上 {n_up} 期、对下 {n_down} 期、未标记方向 {n_none} 期）；"
        f"金额为可用部分累计，缺失与不可比项未计入且未补值。"
    )
    doc.add_paragraph("数据纪律：缺失数据未自动补 0；不可比数据未强行比较。")
    doc.add_paragraph(
        "追溯说明：本 Word 摘要不包含证据入口。逐单元格证据链与证据索引"
        f"（当前共 {n_ev} 条证据记录）请查阅 CostGuard 导出的 Excel 审核底稿工作簿"
        "《证据索引》工作表，按证据 ID 查询。"
    )
    doc.add_paragraph(
        "本摘要由软件自动生成，仅反映已导入数据；导入的合成测试数据仅用于软件测试验证，"
        "不构成任何真实业务结论。"
    )
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"CostGuard管理层摘要_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
    doc.save(str(path))
    return path
