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
from costguard.core.engine.money import round2

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


def _fetch_periods(conn, project_id):
    return conn.execute(
        "SELECT id, period_no, title, direction, contract_party, tax_mode FROM settlement_periods"
        " WHERE project_id=? ORDER BY period_no",
        (project_id,),
    ).fetchall()


def export_settlement_summary(conn: sqlite3.Connection, project_id: int, wb: Workbook,
                              direction: str | None = None) -> str:
    """结算累计表（可按对上/对下过滤）。"""
    periods = [p for p in _fetch_periods(conn, project_id) if direction in (None, p["direction"])]
    aggs = aggregate_project(conn, project_id)
    ws = wb.create_sheet("对上结算累计表" if direction == "upward" else
                         "对下结算累计表" if direction == "downward" else "结算累计表")
    header = ["清单编码", "清单名称", "单位"] + [f"第{p['period_no']}期金额" for p in periods] + \
             ["累计数量", "累计金额", "加权平均单价", "状态"]
    ws.append(header)
    _style_header(ws, 1, len(header))
    for agg in aggs:
        row = [agg.code, agg.name, ""]
        for p in periods:
            pp = agg.per_period.get(p["period_no"])
            row.append(float(pp["amount"]) if pp and pp["amount"] is not None else None)
        row.append(float(agg.cum_qty) if agg.cum_qty is not None else None)
        row.append(float(agg.cum_amount) if agg.cum_amount is not None else None)
        row.append(float(round2(agg.wavg_price)) if agg.wavg_price is not None else None)
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


def export_diff_sheets(conn: sqlite3.Connection, project_id: int, wb: Workbook) -> None:
    """单价/工程量/金额 差异表（同组跨期对比，差异由公式计算）。"""
    aggs = aggregate_project(conn, project_id)
    periods = _fetch_periods(conn, project_id)
    for title, field in (("单价差异表", "unit_price"), ("工程量差异表", "quantity"), ("金额差异表", "amount")):
        ws = wb.create_sheet(title)
        ws.append(["清单编码", "清单名称", "期间", "本期值", "上期值", "差异", "差异率"])
        _style_header(ws, 1, 7)
        for agg in aggs:
            pnos = sorted(agg.per_period)
            prev = None
            for pno in pnos:
                pp = agg.per_period[pno]
                cur = pp.get(field)
                pno_title = f"第{pno}期"
                if prev is not None and cur is not None and prev["value"] is not None:
                    r = ws.max_row + 1
                    ws.append([agg.code, agg.name, pno_title,
                               float(cur), float(prev["value"]), None, None])
                    # 差异公式（保留公式：WPS/Excel 均可复核）
                    ws.cell(row=r, column=6, value=f"=D{r}-E{r}")
                    ws.cell(row=r, column=7, value=f'=IF(E{r}=0,"不可比",(D{r}-E{r})/E{r})')
                    for c in (4, 5, 6):
                        ws.cell(row=r, column=c).number_format = MONEY_FMT
                    ws.cell(row=r, column=7).number_format = "0.00%"
                if cur is not None:
                    prev = {"value": cur}
                _ = pno_title
        _autowidth(ws)
    _ = periods


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
            if val:
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
            ws.cell(row=r, column=3, value=float(u["qty"]))
        if d and d["qty"] is not None:
            ws.cell(row=r, column=4, value=float(d["qty"]))
        if u and u["amount"] is not None:
            ws.cell(row=r, column=5, value=float(u["amount"]))
        if d and d["amount"] is not None:
            ws.cell(row=r, column=6, value=float(d["amount"]))
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
        qty = float(row["quantity"]) if row["quantity"] else None
        price = float(row["unit_price"]) if row["unit_price"] else None
        amount = float(row["amount"]) if row["amount"] else None
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


def export_management_summary(conn: sqlite3.Connection, project_id: int, wb: Workbook) -> None:
    """管理层摘要 sheet。"""
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
    data = [
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("期次数", len(periods)),
        ("清单组数", len(aggs)),
        ("累计金额合计（可用部分）", float(round2(total))),
        ("正常清单组", n_ok),
        ("待补资料清单组", n_inc),
        ("不可比清单组", n_inc2),
        ("高风险异常数", sev["high"]),
        ("中风险异常数", sev["medium"]),
        ("低风险异常数", sev["low"]),
    ]
    for k, v in data:
        ws.append([k, v])
    ws.append([])
    ws.append(["说明：缺失数据未补 0；金额为可用部分累计；不可比/待补资料项详见待核实事项清单。"])
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
    doc = docx_lib.Document()
    doc.add_heading("CostGuard 管理层摘要", level=0)
    doc.add_paragraph(
        f"截至 {datetime.now().strftime('%Y-%m-%d %H:%M')}，共识别清单组 {len(aggs)} 组，"
        f"累计金额（可用部分）{round2(total)} 元，高风险异常 {high} 项。"
    )
    doc.add_paragraph("数据纪律：缺失数据未自动补 0；不可比数据未强行比较；所有结论可在证据索引中追溯。")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"CostGuard管理层摘要_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
    doc.save(str(path))
    return path
