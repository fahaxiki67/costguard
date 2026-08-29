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


class AmbiguousPeriodError(ValueError):
    """同一期号存在多个方向（或不存在）且调用方未声明 direction。

    明确拒绝，绝不静默选择任意方向（监督指令一.3）。
    """


@dataclass
class CheckResult:
    period_id: int
    period_no: int
    direction: str
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


def check_period(conn: sqlite3.Connection, period_id: int) -> CheckResult:
    """对单期做 A/B/C 三重校核。全部查询以 period_id 锁定，
    A/B/C 三路径、异常与证据都落在同一期次上。"""
    meta = conn.execute(
        "SELECT period_no, direction FROM settlement_periods WHERE id=?", (period_id,)
    ).fetchone()
    if not meta:
        raise ValueError(f"period {period_id} not found")
    period_no, direction = int(meta["period_no"]), meta["direction"]

    # A：直接累计清洗后的明细
    rows = conn.execute(
        """SELECT li.amount FROM line_items li
           WHERE li.period_id=? AND (li.flags_json NOT LIKE '%"subtotal": true%')
           AND (li.flags_json NOT LIKE '%"subtotal":true%')""",
        (period_id,),
    ).fetchall()
    total_a, missing = _sum_amounts(rows)

    # C：原表小计行金额
    subtotal_rows = conn.execute(
        """SELECT li.amount FROM line_items li
           WHERE li.period_id=? AND (li.flags_json LIKE '%"subtotal": true%'
           OR li.flags_json LIKE '%"subtotal":true%')""",
        (period_id,),
    ).fetchall()
    raw_subtotal, _ = _sum_amounts(subtotal_rows)

    # B：从原始网格独立重算（不复用 line_items）
    sheet_ids = conn.execute(
        "SELECT id FROM raw_sheets rs WHERE rs.period_id=?",
        (period_id,),
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
            # 表头未识别出合价列时 it.amount 为 None（字段级缺失，不可比）
            rows_b.append({"amount": it.amount.value if it is not None and it.amount else None})
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
        period_id=period_id, period_no=period_no, direction=direction,
        path_a_total=total_a, path_b_total=total_b, raw_subtotal=raw_subtotal,
        diff_ab=diff_ab, status=status, missing_rows=missing + b_missing,
    )


def check_period_by_no(conn: sqlite3.Connection, project_id: int, period_no: int,
                       direction: str | None = None) -> CheckResult:
    """按 (project_id, period_no[, direction]) 的兼容入口。

    direction 缺省且该期号存在多个方向 → AmbiguousPeriodError（明确拒绝）。
    """
    sql = "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?"
    params: list = [project_id, period_no]
    if direction is not None:
        sql += " AND direction=?"
        params.append(direction)
    rows = conn.execute(sql, params).fetchall()
    if len(rows) == 0:
        raise ValueError(f"period {period_no} (direction={direction}) not found in project {project_id}")
    if len(rows) > 1:
        raise AmbiguousPeriodError(
            f"period_no {period_no} has {len(rows)} periods with different directions;"
            " call with explicit direction"
        )
    return check_period(conn, int(rows[0]["id"]))


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


def run_crosscheck(conn: sqlite3.Connection, project_id: int, period_nos: list[int],
                   direction: str | None = None) -> list[CheckResult]:
    """执行多期校核；差异与结论写入 evidence 表并回填 period_totals。

    direction=None 时若某期号对应多个方向，抛 AmbiguousPeriodError。
    证据与 period_totals 回写锁定同一 period_id。
    """
    results = []
    now = datetime.now().isoformat(timespec="seconds")
    for pno in period_nos:
        res = check_period_by_no(conn, project_id, pno, direction=direction)
        results.append(res)
        dir_label = {"upward": "对上", "downward": "对下"}.get(res.direction, "")
        with conn:
            cur = conn.execute(
                "INSERT INTO evidence(project_id, kind, summary, steps_json, sources_json, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (project_id, "cross_check",
                 f"第{pno}期{dir_label}双向校核：{res.status}".replace("期", "期", 1),
                 json.dumps(res.__dict__ | {"path_a_total": str(res.path_a_total),
                                            "path_b_total": str(res.path_b_total),
                                            "raw_subtotal": str(res.raw_subtotal),
                                            "diff_ab": str(res.diff_ab)}, ensure_ascii=False, default=str),
                 json.dumps({"period_id": res.period_id, "period": pno, "direction": res.direction}), now),
            )
            ev_id = cur.lastrowid
            conn.execute(
                """UPDATE period_totals SET cross_check_status=?, cross_check_diff=?, evidence_id=?
                   WHERE period_id=?""",
                (res.status, str(res.diff_ab) if res.diff_ab is not None else None, ev_id, res.period_id),
            )
    return results
