"""对上控制基准对话框 UI 测试（offscreen）。"""
import os

import docx as docx_lib
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from tests.unit.test_contract_extract import SAMPLE_CONTRACT  # noqa: E402

from jiadun.core.contracts import extract  # noqa: E402
from jiadun.core.db import migrations  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def env_with_contract_and_period(tmp_path, app):
    """项目 + 已导入合同（含已确认价款条款）+ 一个对上结算期次。"""

    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        pid = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("控制基准UI测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
        cur = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction, tax_mode)
               VALUES (?,?,?,?,?)""",
            (pid, 1, "对上结算第一期", "upward", "included"),
        )
        period_id = cur.lastrowid
        for amount in ("600000.00", "600000.00"):
            conn.execute(
                "INSERT INTO line_items(period_id, name, amount) VALUES (?,?,?)",
                (period_id, "明细项", amount),
            )
    src = tmp_path / "分包合同.docx"
    d = docx_lib.Document()
    for line in SAMPLE_CONTRACT.splitlines():
        d.add_paragraph(line)
    d.save(str(src))
    extract.import_contract(conn, pid, tmp_path, src, doc_type="subcontract")
    fact_id = int(conn.execute(
        """SELECT cf.id FROM contract_facts cf
           JOIN contract_docs cd ON cd.id=cf.doc_id
           WHERE cd.project_id=? AND cf.fact_key='contract_amount'
             AND cf.fact_value LIKE '%1286500%' LIMIT 1""",
        (pid,),
    ).fetchone()["id"])
    extract.set_fact_review(conn, pid, fact_id, "confirmed", reason="与原文核对一致")
    yield conn, pid, fact_id
    conn.close()


def _open_dialog(env):
    from jiadun.ui.dialogs.control_baseline import ControlBaselineDialog

    conn, pid, _ = env
    return ControlBaselineDialog(conn, pid)


class TestControlBaselineDialog:
    def test_register_from_confirmed_fact_and_period_total(
        self, env_with_contract_and_period
    ):
        conn, pid, _ = env_with_contract_and_period
        dlg = _open_dialog(env_with_contract_and_period)
        assert dlg.fact_combo.currentData() is not None  # 有已确认金额条款可选
        assert dlg.period_combo.currentData() is not None  # 有对上期次
        assert dlg.period_total_label.text() == "明细合计：1200000.00 元"
        dlg._add_from_fact()
        assert dlg.table.rowCount() == 1
        row = conn.execute(
            "SELECT status, amount FROM control_baselines"
        ).fetchone()
        assert row["status"] == "candidate"
        assert row["amount"].startswith("1286500")

    def test_confirm_and_compare_pass_flow(self, env_with_contract_and_period):
        conn, pid, _ = env_with_contract_and_period
        dlg = _open_dialog(env_with_contract_and_period)
        dlg.tax_combo.setCurrentIndex(1)  # 含税（与对上期次 tax_mode=included 一致）
        dlg._add_from_fact()
        dlg.table.selectRow(0)
        dlg.reason_edit.setText("终审审定表已核对，含税口径一致")
        dlg._review("confirmed")
        dlg._compare()
        text = dlg.result_view.toPlainText()
        # 结算合计 1,200,000 元 < 基准 1,286,500 元 → PASS，结余 86500 元
        assert "PASS" in text
        assert "差额：-86500.00 元" in text
        assert "不构成违规或责任认定" in text
