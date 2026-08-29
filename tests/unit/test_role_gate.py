"""业务类型门控（监督第八轮，先红后绿，全部公开合成样例）。

阻断事实：弱表头台账、键值内容页、超长汇总/核销表被误写入结算模型并全流程导出。
门控设计（sheet 级写入闸门 + 文件级 overall）：
1. det is None → 表单路由 / no_header（既有）；
2. 键值对表单结构（form_like）优先于弱清单识别 → non_settlement_form；
3. det.needs_review 且 confidence < 0.70 → needs_role_review（不写 canonical，
   保留 raw + evidence 候选 + 审计）；
4. 文件级大表护栏：任一 sheet 数据行 > 500 → 整个文件 needs_role_review
   （超长汇总/台账/核销表疑似，需人工确认角色）；
5. 规范结算表（强表头）与中置信清单（conf≥0.70 无歧义）仍正常解析（主线不破坏）；
6. 文件 overall：仅当存在 canonical sheet 才 ok；否则 partial/needs_role_review，
   不进入结算计算/导出管线。
"""
import json
from pathlib import Path

import pytest

from costguard.core.db import migrations
from costguard.core.engine import settlement_io
from costguard.core.models import project as pm


def _wb_sheet(ws, header_row, rows, header):
    for c, v in enumerate(header, start=1):
        ws.cell(row=header_row, column=c, value=v)
    for i, row in enumerate(rows, start=header_row + 1):
        for c, v in enumerate(row, start=1):
            if v is not None:
                ws.cell(row=i, column=c, value=v)


def make_strong_settlement(path: Path) -> None:
    """规范结算清单（主线回归：必须解析并写 canonical）。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "第1期"
    _wb_sheet(ws, 1, [
        ("K1", "混凝土垫层", "m3", 10, 465, 4650, "0.09"),
        ("K2", "钢筋制作安装", "t", 5, 4850, 24250, "0.13"),
        ("K3", "平整场地", "m2", 100, 8.5, 850, "0.09"),
    ], ["清单编码", "清单名称", "单位", "工程量", "综合单价", "合价", "税率"])
    wb.save(path)


def make_weak_ledger(path: Path) -> None:
    """弱表头台账（模拟收入合同台账）：字段弱命中、无数量/合价结构。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "台账明细"
    _wb_sheet(ws, 1, [
        (f"HT-{i}", f"某合同{i}", f"对方单位{i}", 1000 + i * 7) for i in range(1, 9)
    ], ["合同编号", "合同名称", "承包人", "合同金额"])
    wb.save(path)


def make_kv_content_page(path: Path) -> None:
    """键值内容页（模拟项目基本信息表）：键值对为主，含个别数字列。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "项目基本信息"
    rows = [
        ("项目名称：", "某工程（脱敏）"),
        ("项目经理：", "张三"),
        ("合同金额：", "12,000,000.00"),
        ("开工日期：", "2025-03-01"),
        ("建筑面积：", "45,000.00"),
        ("结构形式：", "框架"),
    ] * 4
    for r, row in enumerate(rows, start=1):
        ws.cell(row=r, column=1, value=row[0])
        ws.cell(row=r, column=2, value=row[1])
        ws.cell(row=r, column=3, value=r * 13.5)  # 数值列（诱使弱识别）
        ws.cell(row=r, column=4, value=r * 2)
    wb.save(path)


def make_oversized_resource_summary(path: Path) -> None:
    """超长资源汇总（模拟人材机/核销）：规范列结构但 500+ 行、无期次信号。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "资源汇总"
    rows = [(f"R{i:04d}", f"材料{i}", "kg", i % 90 + 1, round(3.5 + i % 20, 2),
             round((i % 90 + 1) * (3.5 + i % 20), 2)) for i in range(1, 520)]
    _wb_sheet(ws, 1, rows,
              ["编号", "名称规格", "单位", "数量", "单价", "合价"])
    wb.save(path)


def make_mixed_oversized(path: Path) -> None:
    """混合文件：一个大表 + 一个小表（文件级护栏应整体挡下）。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "汇总大表"
    rows = [(f"R{i:04d}", f"项{i}", "kg", 1, 2, 2) for i in range(1, 520)]
    _wb_sheet(ws1, 1, rows, ["编号", "名称规格", "单位", "数量", "单价", "合价"])
    ws2 = wb.create_sheet("小清单")
    _wb_sheet(ws2, 1, [("K1", "某清单", "m3", 3, 10, 30)],
              ["清单编码", "清单名称", "单位", "工程量", "综合单价", "合价"])
    wb.save(path)


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        pid = conn.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at)"
            " VALUES ('g',1,'/t','2026')").lastrowid
    yield conn, pid, tmp_path
    conn.close()


def _import(db, name_maker):
    conn, pid, tmp = db
    src = tmp / "in.xlsx"
    name_maker(src)
    info = pm.ProjectInfo(pid, "g", str(tmp), 1, "2026")
    report = settlement_io.import_settlement_file(conn, pid, tmp, src)
    return conn, pid, report


def _counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            for t in ("settlement_periods", "line_items", "period_totals")}


class TestGate:
    def test_strong_settlement_still_parsed(self, db):
        """主线回归：规范结算表必须照常解析并写 canonical，overall=ok。"""
        conn, pid, report = _import(db, make_strong_settlement)
        assert report.status == "ok"
        assert not report.needs_manual_review
        assert any(s.status == "parsed" for s in report.sheets)
        c = _counts(conn)
        assert c["settlement_periods"] == 1 and c["line_items"] == 3

    def test_weak_ledger_gated(self, db):
        """弱表头台账：needs_role_review，零 canonical 写入，overall 非 ok。"""
        conn, pid, report = _import(db, make_weak_ledger)
        assert report.status == "partial"
        assert report.needs_manual_review
        assert all(s.status == "needs_role_review" for s in report.sheets)
        assert _counts(conn) == {"settlement_periods": 0, "line_items": 0,
                                 "period_totals": 0}
        ev = conn.execute(
            "SELECT COUNT(*) c FROM evidence WHERE kind='sheet_role_candidate'").fetchone()["c"]
        assert ev >= 1, "被挡 sheet 必须留 evidence 候选供人工选择"

    def test_kv_content_page_form_routed(self, db):
        """键值内容页：表单路由优先（即使 det 非 None 误命中）。"""
        conn, pid, report = _import(db, make_kv_content_page)
        assert report.needs_manual_review
        assert all(s.status == "non_settlement_form" for s in report.sheets)
        assert _counts(conn)["line_items"] == 0

    def test_oversized_summary_gated_file_level(self, db):
        """超长资源汇总：文件级 needs_role_review（500+ 行大表护栏）。"""
        conn, pid, report = _import(db, make_oversized_resource_summary)
        assert report.needs_manual_review
        assert all(s.status == "needs_role_review" for s in report.sheets)
        assert _counts(conn)["line_items"] == 0

    def test_mixed_file_gated_file_level(self, db):
        """混合文件：任一大表触发护栏 → 文件整体挡下（含小表）。"""
        conn, pid, report = _import(db, make_mixed_oversized)
        assert report.needs_manual_review
        assert _counts(conn)["line_items"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM raw_sheets").fetchone()["c"] == 2  # 保真层保留

    def test_gated_sheets_leave_evidence_and_audit(self, db):
        conn, pid, report = _import(db, make_weak_ledger)
        ev = conn.execute(
            "SELECT summary, sources_json FROM evidence WHERE kind='sheet_role_candidate'"
        ).fetchall()
        assert ev and "待人工" in ev[0]["summary"]
        src = json.loads(ev[0]["sources_json"])[0]
        assert src["sheet_id"] and src["confidence"] is not None
        from costguard.core.evidence import audit as audit_log

        assert any("role" in e.action or "角色" in e.reason
                   for e in audit_log.history_for(conn, pid))
