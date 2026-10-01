"""工程量核对对话框 UI 测试（offscreen）。"""
import json
import os
from decimal import Decimal

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from jiadun.core.db import migrations  # noqa: E402
from jiadun.core.engine import quantity_control as qc  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_modals(monkeypatch):
    """屏蔽模态弹窗（offscreen 下 QMessageBox.exec 会阻塞）。"""
    from PySide6.QtWidgets import QMessageBox

    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: None)


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def env(tmp_path, app):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        pid = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("工程量UI测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
        legacy = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?,?,?,'downward')""",
            (pid, 1, "旧资料第1期")).lastrowid
        conn.execute(
            "INSERT INTO line_items(period_id, name, unit, quantity)"
            " VALUES (?,?,?,?)", (legacy, "C30混凝土", "m3", "100"))
    yield conn, int(pid), legacy
    conn.close()


def _open(env):
    from jiadun.ui.dialogs.quantity_control import QuantityControlDialog

    conn, pid, _ = env
    return QuantityControlDialog(conn, pid)


class TestQuantityControlDialog:
    def test_opens_with_legacy_period_marked_unregistered(self, env):
        conn, pid, legacy = env
        dlg = _open(env)
        assert dlg.run_btn is not None
        # 旧期次显示为未登记上下文，且不出现在确认选择里
        assert len(dlg._legacy_periods) == 1
        assert dlg._selected_contexts() == []
        dlg.deleteLater()

    def test_create_confirm_and_run(self, env):
        conn, pid, legacy = env
        dlg = _open(env)
        dlg.direction_combo.setCurrentIndex(1)  # upward
        dlg.contract_edit.setText("U-总包合同")
        dlg.business_spin.setValue(1)
        dlg.unit_edit.setText("总承包单位")
        dlg.kind_combo.setCurrentIndex(1)  # contract
        dlg.scope_edit.setText("商品混凝土供应")
        dlg.reason_edit.setText("按合同清单登记")
        dlg._create_context()
        contexts = qc.list_period_contexts(conn, pid)
        assert len(contexts) == 1
        assert contexts[0]["status"] == "pending"
        # 确认（原因必填由核心 API 保证；这里给原因走确认路径）
        dlg._row_contexts = dlg._row_contexts  # reload 已刷新
        dlg.table.selectRow(0)
        dlg.reason_edit.setText("与合同原件核对一致")
        dlg._confirm_selected()
        contexts = qc.list_period_contexts(conn, pid)
        assert contexts[0]["status"] == "confirmed"
        with conn:
            conn.execute(
                "INSERT INTO line_items(period_id, name, unit, quantity)"
                " SELECT ?, '混凝土', 'm3', '500'",
                (qc.list_period_contexts(conn, pid)[0]["period_id"],))
        dlg._run_ledger()
        assert dlg._ledger is not None
        assert dlg.items_table.rowCount() >= 1
        dlg.deleteLater()

    def test_attach_legacy_and_confirm_flow(self, env):
        conn, pid, legacy = env
        dlg = _open(env)
        dlg.contract_edit.setText("C-A")
        dlg.scope_edit.setText("浇筑劳务")
        dlg.reason_edit.setText("补登旧资料")
        dlg.table.selectRow(0)   # 选择旧期次行
        dlg._attach_selected()
        contexts = qc.list_period_contexts(conn, pid)
        assert len(contexts) == 1
        assert contexts[0]["status"] == "pending"
        assert contexts[0]["period_id"] == legacy
        # 确认后进入有效核量
        dlg.table.selectRow(0)
        dlg.reason_edit.setText("核对原件")
        dlg._confirm_selected()
        ledger = qc.build_quantity_ledger(conn, pid)
        assert any(
            Decimal(item["quantity"]) == Decimal("100") if item["quantity"] else False
            for item in ledger["items"])
        dlg.deleteLater()

    def test_line_context_tab_real_calls(self, env):
        conn, pid, legacy = env
        # 明细行带楼栋文本与行号，供行口径确认页真实调用
        conn.execute(
            "INSERT INTO line_items(period_id, name, unit, quantity, flags_json)"
            " VALUES (?,?,?,?,?)",
            (legacy, "2号楼混凝土", "m3", "50", json.dumps({"row": 7})))
        dlg = _open(env)
        dlg.lines_table.selectRow(0)
        assert dlg.lines_table.rowCount() >= 1
        # 楼栋候选预填（来自明细文本），仅候选不确认
        dlg._prefill_building_candidates(0, 0)
        tokens = [dlg.line_building_combo.itemText(i)
                  for i in range(dlg.line_building_combo.count())]
        assert any("2号楼" in t for t in tokens)
        # 登记标准键 + 人工换算（真实调用后端 API）
        dlg.line_key_edit.setText("STD-混凝土")
        dlg.line_building_combo.setCurrentText("2号楼")
        dlg.line_factor_edit.setText("1")
        dlg.line_target_edit.setText("m3")
        dlg.line_basis_edit.setText("1板=1m3 施工方案人工确认")
        dlg.line_reason_edit.setText("统一计量口径")
        dlg._set_line_contexts()
        contexts = {c["line_item_id"]: c for c in qc.list_line_contexts(conn, pid)}
        selected_id = dlg._selected_line_ids()[0]
        assert contexts[selected_id]["status"] == "pending"
        # 显式确认后参与计算
        dlg.lines_table.selectRow(0)
        dlg.line_reason_edit.setText("与方案核对一致")
        dlg._confirm_line_contexts()
        contexts = {c["line_item_id"]: c for c in qc.list_line_contexts(conn, pid)}
        assert contexts[selected_id]["status"] == "confirmed"
        assert contexts[selected_id]["standard_key"] == "STD-混凝土"
        dlg.deleteLater()

    def test_sources_detail_shows_traceable_rows(self, env):
        conn, pid, legacy = env
        dlg = _open(env)
        dlg.contract_edit.setText("C-A")
        dlg.scope_edit.setText("浇筑劳务")
        dlg.reason_edit.setText("补登")
        dlg.table.selectRow(0)
        dlg._attach_selected()
        dlg.table.selectRow(0)
        dlg.reason_edit.setText("核对")
        dlg._confirm_selected()
        dlg._run_ledger()
        dlg.items_table.selectRow(0)
        dlg._show_row_sources(0, 0, dlg.items_table)
        text = dlg.detail_view.toPlainText()
        assert "行#" in text
        assert "原量 100m3" in text
        dlg.deleteLater()


class TestDialogLayoutContract:
    """布局根因回归（合成空库）：表头对齐、来源可辨、小屏高度。"""

    def test_header_matches_values_and_source_visible(self, env):
        dlg = _open(env)
        headers = [dlg.table.horizontalHeaderItem(c).text()
                   for c in range(dlg.table.columnCount())]
        assert headers == dlg._context_headers
        assert dlg.table.columnCount() == len(headers)
        row_count = dlg.table.rowCount()
        assert row_count >= 1  # 旧期次行
        for r in range(row_count):
            values = [
                dlg.table.item(r, c).text() if dlg.table.item(r, c) else None
                for c in range(dlg.table.columnCount())
            ]
            assert all(v is not None for v in values), (
                f"第{r}行存在空单元格：表头与值必须一一对齐")
        # 来源/期次标题固定在首列，旧期次也可辨识
        assert dlg.table.item(0, 0).text().strip() != ""
        assert "未登记" in dlg.table.item(0, dlg.table.columnCount() - 1).text()
        # 业务期号列正常显示（旧期次无来源映射时明确标注，不用内部序号冒充）
        assert dlg.table.item(0, 3).text() in ("（业务期号未登记）",) or (
            dlg.table.item(0, 3).text().startswith("第"))
        dlg.deleteLater()

    def test_minimum_height_fits_720(self, env):
        dlg = _open(env)
        dlg.resize(1200, 720)
        assert dlg.minimumSizeHint().height() <= 720, (
            f"最小高度 {dlg.minimumSizeHint().height()} 超过 720，小屏不可用")
        dlg.deleteLater()
