"""异常检测规则库（Phase 4）。

每条规则 = 一个纯函数 (conn, project_id) -> list[Finding]。
规则只"报告"，绝不修改业务数据（纪律：容差只用于分级，不用于调平）。
subject_type: line_item | period | sheet | project
severity: high(金额正确性受威胁) / medium(需人工复核) / low(提示) / info
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal

from costguard.core.engine.money import round2

D = Decimal

ROUND_TOL = D("0.02")
PRICE_CHANGE_PCT = D("0.20")   # 单价变化阈值 20%
QTY_SPIKE_RATIO = D("2.0")     # 工程量突增/突减倍数
LARGE_INT_THRESHOLDS = [D(50000), D(100000), D(1000000)]

# 常见单位别名（等价单位，不算"单位变化"）
UNIT_ALIASES = {
    "m3": {"m³", "立方米", "立米", "M3", "m^3"},
    "m2": {"m²", "平方米", "平米", "M2", "m^2"},
    "m": {"米", "延长米"},
    "t": {"吨", "T"},
    "kg": {"千克", "公斤"},
    "个": {"只", "件"},
    "樘": {"堂", "樘/处"},
}


@dataclass
class Finding:
    rule_id: str
    severity: str
    subject_type: str
    subject_id: int
    message: str
    details: dict = field(default_factory=dict)


def _norm_unit(u: str | None) -> str:
    if not u:
        return ""
    u = u.strip()
    for canonical, aliases in UNIT_ALIASES.items():
        if u == canonical or u in aliases:
            return canonical
    return u


DIR_ZH = {"upward": "对上", "downward": "对下", "unknown": ""}


def _rows(conn, project_id: int):
    return conn.execute(
        """SELECT li.*, sp.period_no AS pno, sp.direction AS dir FROM line_items li
           JOIN settlement_periods sp ON sp.id = li.period_id
           WHERE sp.project_id=? ORDER BY sp.period_no, li.id""",
        (project_id,),
    ).fetchall()


def _is_subtotal(flags_json: str | None) -> bool:
    try:
        return bool(json.loads(flags_json or "{}").get("subtotal"))
    except json.JSONDecodeError:
        return False


def _is_amount_adjustment(name: str | None) -> bool:
    """金额调整行不强制要求清单编码或数量单价。

    仅覆盖语义明确的税额/税费行；其他费用或调整仍按普通清单项检查，
    避免用宽泛关键词吞掉真正缺字段的明细。
    """
    text = (name or "").strip().replace(" ", "")
    return bool(re.fullmatch(r"(?:税金|税额|税费|增值税)(?:[（(].*[）)])?", text))


# ---------- 行级规则 ----------

def rule_qty_price_amount(conn, project_id) -> list[Finding]:
    """数量×单价≠合价。分两级：超出容差为 high；容差内的小额差（四舍五入差异）为 low。"""
    out = []
    for r in _rows(conn, project_id):
        if _is_subtotal(r["flags_json"]) or not (r["quantity"] and r["unit_price"] and r["amount"]):
            continue
        try:
            q, p, a = D(r["quantity"]), D(r["unit_price"]), D(r["amount"])
        except Exception:
            continue
        expect = round2(q * p)
        diff = round2(a) - expect
        if diff == 0:
            continue
        if abs(diff) > ROUND_TOL:
            out.append(Finding(
                "qty_price_amount_mismatch", "high", "line_item", r["id"],
                f"第{r['pno']}期「{r['name']}」：{q}×{p}={expect}，但合价为{round2(a)}，差异{diff}",
                {"qty": str(q), "price": str(p), "amount": str(a), "expect": str(expect),
                 "row": json.loads(r["flags_json"]).get("row")},
            ))
        else:
            out.append(Finding(
                "rounding_difference", "low", "line_item", r["id"],
                f"第{r['pno']}期「{r['name']}」合价与数量×单价存在 {diff} 元四舍五入差异，请确认口径",
                {"qty": str(q), "price": str(p), "amount": str(a), "expect": str(expect)},
            ))
    return out


def rule_negative_qty(conn, project_id) -> list[Finding]:
    out = []
    for r in _rows(conn, project_id):
        if _is_subtotal(r["flags_json"]) or not r["quantity"]:
            continue
        try:
            q = D(r["quantity"])
        except Exception:
            continue
        if q < 0:
            sev = "medium" if r["amount"] and D(r["amount"]) < 0 else "high"
            out.append(Finding(
                "negative_quantity", sev, "line_item", r["id"],
                f"第{r['pno']}期「{r['name']}」数量为负（{q}），冲减或录入错误，需核实",
                {"qty": str(q)},
            ))
    return out


def rule_round_amounts(conn, project_id) -> list[Finding]:
    """大额整数金额（可疑凑整）。"""
    out = []
    for r in _rows(conn, project_id):
        if _is_subtotal(r["flags_json"]) or not r["amount"]:
            continue
        try:
            a = D(r["amount"])
        except Exception:
            continue
        if a != 0 and a == a.to_integral_value() and any(a >= t for t in LARGE_INT_THRESHOLDS):
            out.append(Finding(
                "large_round_amount", "low", "line_item", r["id"],
                f"第{r['pno']}期「{r['name']}」金额为整数 {a}（≥5万），请核实是否估算值",
                {"amount": str(a)},
            ))
    return out


def rule_unparsed_numbers(conn, project_id) -> list[Finding]:
    """数量/单价/金额存在不可解析文本 → 待补资料。"""
    out = []
    for r in _rows(conn, project_id):
        flags = json.loads(r["flags_json"] or "{}")
        for f in ("quantity", "unit_price", "amount"):
            if f"{f}_unparsed" in flags:
                out.append(Finding(
                    "unparsed_number", "high", "line_item", r["id"],
                    f"第{r['pno']}期「{r['name']}」{f} 无法解析为数值：'{flags[f + '_unparsed']}'，标记待补资料",
                    {"field": f, "raw": flags[f + "_unparsed"]},
                ))
    return out


def rule_orphan_rows(conn, project_id) -> list[Finding]:
    out = []
    for r in _rows(conn, project_id):
        flags = json.loads(r["flags_json"] or "{}")
        if flags.get("orphan_numeric_row"):
            out.append(Finding(
                "orphan_numeric_row", "medium", "line_item", r["id"],
                f"第{r['pno']}期存在只有数值没有名称/编码的行（id={r['id']}），请人工归属",
                {},
            ))
    return out


# ---------- 期次内规则 ----------

def rule_subtotal_vs_details(conn, project_id) -> list[Finding]:
    """汇总值与明细值不一致（原表小计行 vs 明细求和）。"""
    out = []
    periods = conn.execute(
        "SELECT id, period_no FROM settlement_periods WHERE project_id=?", (project_id,)
    ).fetchall()
    for p in periods:
        rows = conn.execute(
            "SELECT amount, flags_json FROM line_items WHERE period_id=?", (p["id"],)
        ).fetchall()
        enriched = []
        for r in rows:
            flags = json.loads(r["flags_json"] or "{}")
            enriched.append({"amount": r["amount"], "flags": flags,
                             "subtotal": bool(flags.get("subtotal")),
                             "row": flags.get("row")})
        details = [r for r in enriched if not r["subtotal"]]
        subtotals = [r for r in enriched if r["subtotal"]]
        if not subtotals or not details:
            continue

        # 有原始行号时，每个小计只核对其前一段明细。税金、支付比例等位于小计
        # 之后的调整项不应倒灌进前一个小计；多个章节小计也各自独立。
        ordered = all(isinstance(r["row"], int) for r in enriched)
        groups: list[tuple[list[dict], list[dict]]] = []
        if ordered:
            prev_subtotal_row = 0
            for subtotal in sorted(subtotals, key=lambda r: r["row"]):
                segment = [r for r in details
                           if prev_subtotal_row < r["row"] < subtotal["row"]]
                if segment:
                    groups.append((segment, [subtotal]))
                prev_subtotal_row = subtotal["row"]
        else:
            # 历史/合成数据可能没有 row 证据，保持原有整期比较口径。
            groups.append((details, subtotals))

        for detail_group, subtotal_group in groups:
            det_total, det_missing = D(0), 0
            for r in detail_group:
                if r["amount"]:
                    try:
                        det_total += D(r["amount"])
                    except Exception:
                        det_missing += 1
                else:
                    det_missing += 1
            sub_total = D(0)
            for r in subtotal_group:
                if r["amount"]:
                    try:
                        sub_total += D(r["amount"])
                    except Exception:
                        pass
            if det_missing:
                out.append(Finding(
                    "summary_missing_data", "medium", "period", p["id"],
                    f"第{p['period_no']}期存在 {det_missing} 行金额缺失，小计核对不可靠（待补资料）", {},
                ))
            elif abs(det_total - sub_total) > ROUND_TOL:
                out.append(Finding(
                    "summary_mismatch", "high", "period", p["id"],
                    f"第{p['period_no']}期明细合计 {round2(det_total)} ≠ 原表小计 {round2(sub_total)}，"
                    f"差异 {round2(det_total - sub_total)}",
                    {"details_sum": str(det_total), "subtotal": str(sub_total)},
                ))
    return out


def rule_duplicates(conn, project_id) -> list[Finding]:
    """重复清单（同一期次内同编码同名多行）。

    键使用 period_id：对上/对下可存在相同期号，按期号会跨方向误报。
    """
    out = []
    seen: dict[tuple, list] = {}
    for r in _rows(conn, project_id):
        if _is_subtotal(r["flags_json"]):
            continue
        key = (r["period_id"], r["code"] or "", (r["name"] or "").strip())
        seen.setdefault(key, []).append(r)
    for (period_id, code, name), rows in seen.items():
        if len(rows) > 1:
            out.append(Finding(
                "duplicate_item", "medium", "period", period_id,
                f"{_dir_label(rows[0]['dir'])}第{rows[0]['pno']}期「{name}」"
                f"（编码 {code or '无'}）出现 {len(rows)} 行，请确认是否重复结算或分期计量",
                {"item_ids": [r["id"] for r in rows]},
            ))
    return out


def rule_same_code_diff_name(conn, project_id) -> list[Finding]:
    out: list[Finding] = []
    seen: dict[tuple[str, str], set[str]] = {}
    for r in _rows(conn, project_id):
        if _is_subtotal(r["flags_json"]) or not r["code"]:
            continue
        seen.setdefault((r["dir"], r["code"]), set()).add((r["name"] or "").strip())
    for (direction, code), names in seen.items():
        if len(names) > 1:
            out.append(Finding(
                "same_code_diff_name", "medium", "project", project_id,
                f"{_dir_label(direction)}编码 {code} 对应多个名称：{sorted(names)}，请核实口径",
                {"direction": direction, "names": sorted(names)},
            ))
    return out


def rule_same_name_diff_code(conn, project_id) -> list[Finding]:
    seen: dict[tuple[str, str], set[str]] = {}
    out = []
    for r in _rows(conn, project_id):
        if _is_subtotal(r["flags_json"]) or not (r["name"] or "").strip():
            continue
        seen.setdefault((r["dir"], (r["name"] or "").strip()), set()).add(r["code"] or "")
    for (direction, name), codes in seen.items():
        codes.discard("")
        if len(codes) > 1:
            out.append(Finding(
                "same_name_diff_code", "low", "project", project_id,
                f"{_dir_label(direction)}名称「{name}」对应多个编码：{sorted(codes)}，"
                "可能为不同部位/阶段，亦可能录入不一致",
                {"direction": direction, "codes": sorted(codes)},
            ))
    return out


# ---------- 跨期规则 ----------

def _item_series(conn, project_id: int) -> dict[tuple[str, str], dict[int, list]]:
    """(item_key, direction) -> period_no -> [rows]

    跨期比较（单价/税率/数量/单位变化、疑似重复结算）只在同一方向的期次
    之间进行——对上与对下的同清单不是同一口径，混比会制造误报。
    """
    series: dict[tuple[str, str], dict[int, list]] = {}
    for r in _rows(conn, project_id):
        if _is_subtotal(r["flags_json"]):
            continue
        key = r["code"] if r["code"] else f"name:{r['name']}"
        series.setdefault((key, r["dir"]), {}).setdefault(r["pno"], []).append(r)
    return series


def _dir_label(direction: str) -> str:
    zh = DIR_ZH.get(direction, direction)
    return f"[{zh}]" if zh else ""


def rule_unit_changed(conn, project_id) -> list[Finding]:
    out = []
    for (key, direction), by_period in _item_series(conn, project_id).items():
        units = {pno: {_norm_unit(r["unit"]) for r in rows if r["unit"]} for pno, rows in by_period.items()}
        flat = {u for s in units.values() for u in s}
        if len(flat) > 1:
            out.append(Finding(
                "unit_changed", "high", "project", project_id,
                f"{_dir_label(direction)}清单 {key} 单位不一致：{units}，数量不可直接累计（不可比），请核实",
                {"units": {str(k): sorted(v) for k, v in units.items()}},
            ))
    return out


def rule_price_changed(conn, project_id) -> list[Finding]:
    """同一清单同方向跨期单价变化：任何变化记录；超阈值升级为单价异常。"""
    any_changes, abnormal = [], []
    for (key, direction), by_period in _item_series(conn, project_id).items():
        prices: dict[int, Decimal] = {}
        for pno, rows in sorted(by_period.items()):
            for r in rows:
                if r["unit_price"]:
                    try:
                        prices[pno] = D(r["unit_price"])
                        break
                    except Exception:
                        continue
        vals = list(prices.values())
        if len(set(vals)) > 1:
            first_pno = next(iter(prices))
            base = prices[first_pno]
            detail = {str(p): str(v) for p, v in prices.items()}
            any_changes.append(Finding(
                "price_changed", "low", "project", project_id,
                f"{_dir_label(direction)}清单 {key} 跨期单价不一致：{detail}", {"prices": detail},
            ))
            for pno, v in prices.items():
                if base != 0 and abs((v - base) / base) > PRICE_CHANGE_PCT:
                    abnormal.append(Finding(
                        "price_abnormal_change", "high", "project", project_id,
                        f"{_dir_label(direction)}清单 {key} 第{pno}期单价 {v} 较第{first_pno}期 {base} 变化超过 "
                        f"{PRICE_CHANGE_PCT * 100}%，请核实调价依据",
                        {"prices": detail},
                    ))
                    break
    return abnormal + any_changes


def rule_tax_changed(conn, project_id) -> list[Finding]:
    out = []
    for (key, direction), by_period in _item_series(conn, project_id).items():
        rates: dict[int, str] = {}
        for pno, rows in sorted(by_period.items()):
            for r in rows:
                if r["tax_rate"]:
                    rates[pno] = r["tax_rate"]
                    break
        vals = set(rates.values())
        if len(vals) > 1:
            out.append(Finding(
                "tax_rate_changed", "medium", "project", project_id,
                f"{_dir_label(direction)}清单 {key} 跨期税率变化：{rates}，影响含税/不含税口径，请核实",
                {"rates": {str(k): v for k, v in rates.items()}},
            ))
    return out


def rule_tax_mode_mixed(conn, project_id) -> list[Finding]:
    """含税/不含税混用：从表头文本推断每期单价口径。"""
    out = []
    modes: dict[tuple[str, int], set[str]] = {}
    headers = conn.execute(
        """SELECT th.*, sp.period_no AS pno, sp.direction AS direction FROM table_headers th
           JOIN raw_sheets rs ON rs.id = th.sheet_id
           JOIN settlement_periods sp ON sp.id = rs.period_id
           WHERE sp.project_id=?""",
        (project_id,),
    ).fetchall()
    for h in headers:
        col_map = json.loads(h["col_map_json"])
        if "unit_price" not in col_map:
            continue
        sheet_text = ""
        row = conn.execute(
            "SELECT raw_value FROM raw_cells WHERE sheet_id=? AND row BETWEEN ? AND ?",
            (h["sheet_id"], h["header_row_lo"], h["header_row_hi"]),
        ).fetchall()
        sheet_text = "".join((r["raw_value"] or "") for r in row)
        if "不含税" in sheet_text:
            modes.setdefault((h["direction"], h["pno"]), set()).add("excl_tax")
        elif "含税" in sheet_text:
            modes.setdefault((h["direction"], h["pno"]), set()).add("incl_tax")
    directions = {direction for direction, _ in modes}
    for direction in sorted(directions):
        direction_modes = {
            pno: values for (row_direction, pno), values in modes.items()
            if row_direction == direction
        }
        if len({mode for values in direction_modes.values() for mode in values}) > 1:
            out.append(Finding(
                "tax_mode_mixed", "high", "project", project_id,
                f"{_dir_label(direction)}各期单价含税口径不一致（{direction_modes}），"
                "直接累计会产生口径混淆，请统一后比较",
                {"direction": direction,
                 "modes": {str(k): sorted(v) for k, v in direction_modes.items()}},
            ))
    return out


def rule_qty_spike(conn, project_id) -> list[Finding]:
    """同方向相邻期工程量突增/突减。"""
    out = []
    for (key, direction), by_period in _item_series(conn, project_id).items():
        qtys: dict[int, Decimal] = {}
        for pno, rows in sorted(by_period.items()):
            for r in rows:
                if r["quantity"]:
                    try:
                        qtys[pno] = D(r["quantity"])
                        break
                    except Exception:
                        continue
        pnos = sorted(qtys)
        for prev, cur in zip(pnos, pnos[1:], strict=False):
            base = qtys[prev]
            if base == 0:
                continue
            ratio = qtys[cur] / base
            if ratio >= QTY_SPIKE_RATIO or ratio <= D(1) / QTY_SPIKE_RATIO:
                out.append(Finding(
                    "qty_sudden_change", "medium", "project", project_id,
                    f"{_dir_label(direction)}清单 {key} 工程量第{prev}期 {base} → 第{cur}期 {qtys[cur]}"
                    f"（{ratio:.2f} 倍），请核实计量依据",
                    {"prev": str(base), "cur": str(qtys[cur]), "ratio": str(ratio)},
                ))
    return out


def rule_suspected_duplicate_settlement(conn, project_id) -> list[Finding]:
    """同方向跨期出现数量+单价+金额完全一致的行（疑似重复结算）。"""
    out = []
    seen: dict[tuple, list] = {}
    for r in _rows(conn, project_id):
        if _is_subtotal(r["flags_json"]):
            continue
        if r["quantity"] and r["unit_price"] and r["amount"]:
            key = (r["code"] or r["name"], r["quantity"], r["unit_price"], r["amount"], r["dir"])
            seen.setdefault(key, []).append(r)
    for key, rows in seen.items():
        pnos = {r["pno"] for r in rows}
        if len(pnos) > 1:
            out.append(Finding(
                "suspected_duplicate_settlement", "medium", "project", project_id,
                f"{_dir_label(key[4])}「{key[0]}」在第 {sorted(pnos)} 期出现完全相同的数量/单价/金额，疑似重复结算",
                {"item_ids": [r["id"] for r in rows]},
            ))
    return out


# ---------- Sheet 级规则 ----------

def _sheets(conn, project_id: int):
    return conn.execute(
        """SELECT rs.*, sp.period_no AS pno FROM raw_sheets rs
           JOIN settlement_periods sp ON sp.id = rs.period_id
           WHERE sp.project_id=?""",
        (project_id,),
    ).fetchall()


def rule_hidden_cells(conn, project_id) -> list[Finding]:
    out = []
    for s in _sheets(conn, project_id):
        hidden_rows = json.loads(s["hidden_rows_json"] or "[]")
        hidden_cols = json.loads(s["hidden_cols_json"] or "[]")
        if hidden_rows:
            out.append(Finding(
                "hidden_rows", "medium", "sheet", s["id"],
                f"第{s['pno']}期 Sheet「{s['sheet_name']}」存在隐藏行 {hidden_rows}，"
                "可能漏数据，请取消隐藏后人工确认",
                {"hidden_rows": hidden_rows},
            ))
        if hidden_cols:
            out.append(Finding(
                "hidden_cols", "medium", "sheet", s["id"],
                f"第{s['pno']}期 Sheet「{s['sheet_name']}」存在隐藏列 {hidden_cols}，请确认其中无金额字段",
                {"hidden_cols": hidden_cols},
            ))
    return out


def rule_formula_issues(conn, project_id) -> list[Finding]:
    """公式错误与公式无缓存值。"""
    out = []
    for s in _sheets(conn, project_id):
        errs = conn.execute(
            """SELECT row, col, raw_value, cached_value FROM raw_cells
               WHERE sheet_id=? AND (raw_value LIKE '#%' OR cached_value LIKE '#%')""",
            (s["id"],),
        ).fetchall()
        for e in errs:
            val = e["cached_value"] or e["raw_value"]
            out.append(Finding(
                "formula_error", "high", "sheet", s["id"],
                f"第{s['pno']}期 Sheet「{s['sheet_name']}」单元格"
                f"({e['row']},{e['col']}) 公式错误 {val}，该值不可信",
                {"row": e["row"], "col": e["col"], "value": val},
            ))
        nocache = conn.execute(
            """SELECT row, col, raw_value FROM raw_cells
               WHERE sheet_id=? AND is_formula=1 AND cached_value IS NULL""",
            (s["id"],),
        ).fetchall()
        for e in nocache:
            out.append(Finding(
                "formula_no_cache", "medium", "sheet", s["id"],
                f"第{s['pno']}期 Sheet「{s['sheet_name']}」单元格({e['row']},{e['col']})"
                f"公式 {e['raw_value']} 无缓存计算值，请用 WPS/Excel 打开重算后重新导入",
                {"row": e["row"], "col": e["col"]},
            ))
    return out


def rule_text_numbers_in_value_cols(conn, project_id) -> list[Finding]:
    """数值列（数量/单价/金额位置）存在文本数字，按 Sheet×字段聚合成一条。

    真实报表里文本数字极常见，逐格出告警会淹没更有价值的发现；
    details.rows 保留全量行号，消息里预览前 10 行。
    """
    out = []
    for s in _sheets(conn, project_id):
        h = conn.execute(
            "SELECT col_map_json FROM table_headers WHERE sheet_id=?", (s["id"],)
        ).fetchone()
        if not h:
            continue
        col_map = json.loads(h["col_map_json"])
        value_cols = {col_map[f]: f for f in ("quantity", "unit_price", "amount") if f in col_map}
        cells = conn.execute(
            """SELECT row, col, raw_value FROM raw_cells
               WHERE sheet_id=? AND is_number_stored_as_text=1""",
            (s["id"],),
        ).fetchall()
        grouped: dict[str, list[dict]] = {}
        for c in cells:
            field_name = value_cols.get(c["col"])
            if field_name:
                grouped.setdefault(field_name, []).append(
                    {"row": c["row"], "raw": c["raw_value"]})
        for field_name, hits in grouped.items():
            rows = sorted(hit["row"] for hit in hits)
            preview = "、".join(str(r) for r in rows[:10])
            suffix = f" 等 {len(rows)} 行" if len(rows) > 10 else ""
            out.append(Finding(
                "text_number_in_value_col", "medium", "sheet", s["id"],
                f"第{s['pno']}期 Sheet「{s['sheet_name']}」{field_name} 列有 {len(rows)} 处"
                f"文本数字（第 {preview}{suffix}），已按数值解析，请确认无误",
                {"rows": rows, "count": len(rows), "field": field_name,
                 "samples": [hit["raw"] for hit in hits[:10]]},
            ))
    return out


def rule_merged_cells_in_data(conn, project_id) -> list[Finding]:
    out = []
    for s in _sheets(conn, project_id):
        h = conn.execute(
            "SELECT header_row_hi, col_map_json FROM table_headers WHERE sheet_id=?", (s["id"],)
        ).fetchone()
        if not h:
            continue
        merged = json.loads(s["merged_ranges_json"] or "[]")
        data_merged = []

        for rng in merged:
            m = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", rng.replace("$", ""))
            if not m:
                continue

            start_row = int(m.group(2))
            if start_row > h["header_row_hi"]:
                data_merged.append(rng)
        if data_merged:
            out.append(Finding(
                "merged_cells_in_data", "medium", "sheet", s["id"],
                f"第{s['pno']}期 Sheet「{s['sheet_name']}」数据区存在合并单元格 {data_merged[:5]}"
                f"{'…' if len(data_merged) > 5 else ''}，解析按锚点值填充，请抽查确认无错位",
                {"ranges": data_merged},
            ))
    return out


def rule_missing_key_fields(conn, project_id) -> list[Finding]:
    """缺失关键字段检查（降噪版）：

    - 整列未识别（表头列映射缺该字段）→ 表级一条异常；
    - 列存在但个别行为空 → 行级异常（待补资料）。
    避免在“本来就没有编码列”的台账/汇总表上刷出海量行级误报。
    """
    out: list[Finding] = []
    sheet_colmaps: dict[int, dict] = {}
    for s in _sheets(conn, project_id):
        h = conn.execute(
            "SELECT col_map_json FROM table_headers WHERE sheet_id=?", (s["id"],)
        ).fetchone()
        if h:
            sheet_colmaps[s["id"]] = json.loads(h["col_map_json"])

    # ---- 表级：整列缺失 ----
    field_labels = {"code": "清单编码", "name": "清单名称", "quantity": "工程量",
                    "unit_price": "单价", "amount": "合价"}
    for sid, col_map in sheet_colmaps.items():
        meta = conn.execute(
            "SELECT sheet_name, period_id FROM raw_sheets WHERE id=?", (sid,)
        ).fetchone()
        if not meta:
            continue
        n_rows = conn.execute(
            "SELECT COUNT(*) c FROM line_items WHERE sheet_id=?", (sid,)
        ).fetchone()["c"]
        if not n_rows:
            continue
        missing_cols = [field_labels[f] for f in ("code", "name") if f not in col_map]
        # 合法计价有两条路径：直接金额，或工程量×单价。人工已确认直接金额列时，
        # 工程量/单价不是缺失字段；反之，数量单价齐全时也不强制要求合价列。
        has_amount_path = "amount" in col_map
        has_qty_price_path = "quantity" in col_map and "unit_price" in col_map
        if not (has_amount_path or has_qty_price_path):
            missing_cols.append("金额路径（合价，或工程量×单价）")
        if missing_cols:
            out.append(Finding(
                "missing_key_column", "medium", "sheet", sid,
                f"Sheet「{meta['sheet_name']}」未识别到{'、'.join(missing_cols)}列"
                f"（{n_rows} 行均缺失，按列缺失处理，进入人工确认）",
                {"missing_columns": missing_cols, "n_rows": n_rows},
            ))

    # ---- 行级：列存在但该行为空 ----
    for r in _rows(conn, project_id):
        if _is_subtotal(r["flags_json"]):
            continue
        col_map = sheet_colmaps.get(r["sheet_id"])
        # 有表头记录时：整列缺失已按表级报告，行级只查存在的列；
        # 无表头记录（如手工数据/旧流程行）：退回逐字段行级判断。
        missing = []
        if col_map is None:
            # 旧流程/手工数据没有已确认表头语义，保持严格逐字段检查。
            for f, label in field_labels.items():
                if f in ("code", "name") and not (r[f] or "").strip():
                    missing.append(label)
                elif f in ("quantity", "unit_price", "amount") and not r[f]:
                    missing.append(label)
        else:
            if "name" in col_map and not (r["name"] or "").strip():
                missing.append(field_labels["name"])
            if ("code" in col_map and not _is_amount_adjustment(r["name"])
                    and not (r["code"] or "").strip()):
                missing.append(field_labels["code"])

            amount_complete = "amount" in col_map and bool(r["amount"])
            qty_price_complete = (
                "quantity" in col_map and "unit_price" in col_map
                and bool(r["quantity"]) and bool(r["unit_price"])
            )
            if not (amount_complete or qty_price_complete):
                missing.append("合价或工程量×单价")
        if missing:
            out.append(Finding(
                "missing_key_fields", "medium", "line_item", r["id"],
                f"第{r['pno']}期第 {json.loads(r['flags_json']).get('row', '?')} 行缺失：{'、'.join(missing)}"
                "（待补资料，未参与相关累计）",
                {"missing": missing},
            ))
    return out


def rule_needs_review_headers(conn, project_id) -> list[Finding]:
    out = []
    for s in _sheets(conn, project_id):
        h = conn.execute(
            "SELECT confidence, needs_review, col_map_json FROM table_headers WHERE sheet_id=?", (s["id"],)
        ).fetchone()
        if h and h["needs_review"]:
            out.append(Finding(
                "header_needs_review", "medium", "sheet", s["id"],
                f"第{s['pno']}期 Sheet「{s['sheet_name']}」表头识别置信度低（{h['confidence']}），"
                f"列映射 {h['col_map_json']}，请人工核对",
                {},
            ))
    return out


ALL_RULES = [
    rule_qty_price_amount,
    rule_negative_qty,
    rule_round_amounts,
    rule_unparsed_numbers,
    rule_orphan_rows,
    rule_subtotal_vs_details,
    rule_duplicates,
    rule_same_code_diff_name,
    rule_same_name_diff_code,
    rule_unit_changed,
    rule_price_changed,
    rule_tax_changed,
    rule_tax_mode_mixed,
    rule_qty_spike,
    rule_suspected_duplicate_settlement,
    rule_hidden_cells,
    rule_formula_issues,
    rule_text_numbers_in_value_cols,
    rule_merged_cells_in_data,
    rule_missing_key_fields,
    rule_needs_review_headers,
]
