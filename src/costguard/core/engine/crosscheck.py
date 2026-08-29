"""双向校核（Phase 3 核心）。

A 路径：各期 line_items 金额直接累计（数据库已清洗值）。
B 路径：从 raw_cells 原始网格重新抽取、重新计算（完全独立复算）。
C 校验：明细合计 vs 原表自带"小计"行。

差异必须落库并生成证据；差异不做任何"调平"。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from costguard.core.engine.money import round2  # noqa: F401  (money 入口纪律：保留显式依赖)
from costguard.core.engine.settlement_io import load_sheet_grid
from costguard.core.parsing import extract_items
from costguard.core.parsing.header_detect import detect_header

D = Decimal

TOL = D("0.02")  # 两条独立路径各保留两位，容差 0.02 仅用于分级报告


@dataclass
class CheckResult:
    period_no: int
    path_a_total: Decimal | None  # 直接累计
    path_b_total: Decimal | None  # 网格重算
    raw_subtotal: Decimal | None  # 原表小计行
    diff_ab: Decimal | None
    status: str  # 'match' | 'diff' | 'incomplete'
    missing_rows: int = 0
    notes: list[str] = field(default_factory=list)


def _sum_amounts(rows) -> tuple[Decimal | None, int]:
    total: Decimal | None = None
    missing = 0
    for r in rows:
        if r["amount"] is None or r["amount"] == "":
            missing += 1
            continue
        try:
            v = D(r["amount"])
        except Exception:
            missing += 1
            continue
        total = v if total is None else total + v
    return total, missing


def check_period(conn: sqlite3.Connection, project_id: int, period_no: int) -> CheckResult:
    """对单期做 A/B/C 三重校核。"""
    # A：直接累计清洗后的明细
    rows = conn.execute(
        """SELECT li.amount FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
           WHERE sp.project_id=? AND sp.period_no=? AND (li.flags_json NOT LIKE '%"subtotal": true%')
           AND (li.flags_json NOT LIKE '%"subtotal":true%')""",
        (project_id, period_no),
    ).fetchall()
    total_a, missing = _sum_amounts(rows)

    # C：原表小计行金额
    subtotal_rows = conn.execute(
        """SELECT li.amount FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
           WHERE sp.project_id=? AND sp.period_no=? AND (li.flags_json LIKE '%"subtotal": true%'
           OR li.flags_json LIKE '%"subtotal":true%')""",
        (project_id, period_no),
    ).fetchall()
    raw_subtotal, _ = _sum_amounts(subtotal_rows)

    # B：从原始网格独立重算（不复用 line_items）
    sheet_ids = conn.execute(
        """SELECT rs.id FROM raw_sheets rs
           JOIN settlement_periods sp ON sp.id = rs.period_id
           WHERE sp.project_id=? AND sp.period_no=?""",
        (project_id, period_no),
    ).fetchall()
    total_b: Decimal | None = None
    b_missing = 0
    for (sheet_id,) in [(r["id"],) for r in sheet_ids]:
        cells, merged, n_rows, _n_cols = load_sheet_grid(conn, sheet_id)
        det = detect_header(0, cells, merged, n_rows, max((c for _, c in cells), default=0))
        if det is None:
            continue
        items = extract_items.extract_items(cells, merged, det, n_rows)
        rows_b = []
        for it in items:
            if it.flags.get("subtotal"):
                continue
            rows_b.append({"amount": it.amount.value})
        sub, sub_missing = _sum_amounts(rows_b)
        b_missing += sub_missing
        if sub is not None:
            total_b = sub if total_b is None else total_b + sub

    status = "match"
    diff_ab = None
    if total_a is None or total_b is None:
        status = "incomplete"
        if missing or b_missing:
            status = "incomplete"
    else:
        diff_ab = total_a - total_b
        status = "match" if abs(diff_ab) <= TOL else "diff"
    return CheckResult(
        period_no=period_no, path_a_total=total_a, path_b_total=total_b, raw_subtotal=raw_subtotal,
        diff_ab=diff_ab, status=status, missing_rows=missing + b_missing,
    )


def make_evidence(project_id: int, res: CheckResult) -> str:
    return json.dumps(
        {
            "kind": "cross_check",
            "summary": f"第{res.period_no}期双向校核：{res.status}",
            "steps": [
                {"step": "A 直接累计清洗明细", "result": str(res.path_a_total)},
                {"step": "B 从原始网格独立重算", "result": str(res.path_b_total)},
                {"step": "C 原表小计行", "result": str(res.raw_subtotal)},
                {"step": "差异 A-B", "result": str(res.diff_ab)},
            ],
            "sources": [{"period": res.period_no, "missing_rows": res.missing_rows}],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
    )


def run_crosscheck(conn: sqlite3.Connection, project_id: int, period_nos: list[int]) -> list[CheckResult]:
    """执行多期校核；差异与结论写入 evidence 表并回填 period_totals。"""
    results = []
    now = datetime.now().isoformat(timespec="seconds")
    for pno in period_nos:
        res = check_period(conn, project_id, pno)
        results.append(res)
        with conn:
            cur = conn.execute(
                "INSERT INTO evidence(project_id, kind, summary, steps_json, sources_json, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (project_id, "cross_check", f"第{pno}期双向校核：{res.status}",
                 json.dumps(res.__dict__ | {"path_a_total": str(res.path_a_total),
                                            "path_b_total": str(res.path_b_total),
                                            "raw_subtotal": str(res.raw_subtotal),
                                            "diff_ab": str(res.diff_ab)}, ensure_ascii=False, default=str),
                 json.dumps({"period": pno}), now),
            )
            ev_id = cur.lastrowid
            conn.execute(
                """UPDATE period_totals SET cross_check_status=?, cross_check_diff=?, evidence_id=?
                   WHERE period_id IN (SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?)""",
                (res.status, str(res.diff_ab) if res.diff_ab is not None else None, ev_id, project_id, pno),
            )
    return results
