"""工作台 UI 测试（offscreen）：驱动完整业务流程。"""
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

sys.path.insert(0, str(Path(__file__).parents[2] / "synthetic_test_data"))

from generator import make_multi_period  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def wb_page(tmp_path, app):
    """创建项目并打开工作台页。"""
    from costguard.core.models import project as pm
    from costguard.ui.workbench import WorkbenchPage

    info = pm.create_project("UI测试项目", tmp_path / "ws")
    info, conn = pm.open_project(Path(info.workspace_path))
    src = tmp_path / "multi.xlsx"
    make_multi_period(src, periods=2)
    from costguard.core.engine import settlement_io

    settlement_io.import_settlement_file(conn, info.project_id, Path(info.workspace_path), src)
    page = WorkbenchPage(conn, info, info.workspace_path, on_back=lambda: None)
    yield page
    page.conn.close()


class TestWorkbench:
    def test_tabs_present(self, wb_page):
        names = [wb_page.tabs.tabText(i) for i in range(wb_page.tabs.count())]
        assert names == ["期次概览", "清单明细", "异常检测", "匹配复核", "成果导出"]

    def test_period_overview_populated(self, wb_page):
        wb_page.refresh_periods()
        t = wb_page.period_table
        assert t.rowCount() == 2  # multi.xlsx 2 期
        assert t.item(0, 0).text() == "1"
        assert "（未标记）" in t.item(0, 2).text()

    def test_items_populated_with_provenance(self, wb_page):
        wb_page.refresh_items()
        t = wb_page.items_table
        assert t.rowCount() > 0
        # 方向直接可见；数量列有值；出处列非空（保真层证据）
        assert t.horizontalHeaderItem(0).text() == "方向"
        assert t.item(0, 0).text() == "未标记"
        assert t.item(0, 5).text() not in ("", "—")
        assert t.item(0, 9).text().startswith("行")

    def test_anomaly_run_and_display(self, wb_page):
        from costguard.core.anomalies import engine

        findings = engine.run_anomalies(wb_page.conn, wb_page.project.project_id)
        wb_page.refresh_anomalies()
        assert wb_page.anomaly_table.rowCount() == len(findings)
        assert wb_page.anomaly_table.horizontalHeaderItem(1).text() == "方向"

    def test_sheet_anomaly_displays_its_period_direction(self, wb_page):
        row = wb_page.conn.execute(
            """SELECT rs.id AS sheet_id, rs.period_id
               FROM raw_sheets rs
               WHERE rs.period_id IS NOT NULL
               ORDER BY rs.id LIMIT 1"""
        ).fetchone()
        assert row is not None
        with wb_page.conn:
            wb_page.conn.execute(
                "UPDATE settlement_periods SET direction='upward' WHERE id=?",
                (row["period_id"],),
            )
            wb_page.conn.execute(
                """INSERT INTO anomalies(
                   project_id, rule_id, severity, subject_type, subject_id,
                   message, status, created_at)
                   VALUES (?, 'sheet_direction_probe', 'medium', 'sheet', ?,
                           '工作表方向回溯测试', 'open', '2026-08-30T00:00:00')""",
                (wb_page.project.project_id, row["sheet_id"]),
            )

        wb_page.refresh_anomalies()
        table = wb_page.anomaly_table
        probe_rows = [
            i for i in range(table.rowCount())
            if table.item(i, 3).text() == "sheet_direction_probe"
        ]
        assert len(probe_rows) == 1
        assert table.item(probe_rows[0], 1).text() == "对上结算"

    def test_match_run_and_levels_zh(self, wb_page):
        from costguard.core.matching import matching

        groups = matching.match_items(wb_page.conn, wb_page.project.project_id)
        matching.save_matches(wb_page.conn, wb_page.project.project_id, groups)
        wb_page.refresh_matches()
        assert wb_page.match_table.rowCount() == len(groups)
        levels = {wb_page.match_table.item(i, 2).text() for i in range(wb_page.match_table.rowCount())}
        assert levels & {
            "规则完全匹配（待人工确认）", "高概率匹配", "疑似匹配", "不可比", "待补资料"
        }

    def test_reason_dialog_requires_reason(self, app, wb_page, monkeypatch):
        """空原因拒绝接受； QMessageBox 打桩避免 offscreen 模态阻塞。"""
        from PySide6.QtWidgets import QDialog, QMessageBox

        from costguard.ui.workbench import ReasonDialog

        warned = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(1))

        dlg = ReasonDialog("t", "p")
        dlg.reason_edit.setPlainText("   ")
        dlg._accept()  # 空原因：弹警告且不 accept
        assert warned, "empty reason must trigger warning"
        assert dlg.result() != QDialog.Accepted
        dlg.reason_edit.setPlainText("正常原因")
        dlg._accept()
        assert dlg.result() == QDialog.Accepted

    def test_export_via_ui(self, wb_page, tmp_path):
        # 导出按钮逻辑直接调用（避免 QMessageBox 阻塞）
        from costguard.core.export import excel_export

        path = excel_export.export_workbook(
            wb_page.conn, wb_page.project.project_id, Path(wb_page.project_dir) / "exports")
        assert path.exists()


class TestMainWindow:
    def test_two_pages_and_switch(self, app, tmp_path, monkeypatch):
        from costguard.core.models import project as pm
        from costguard.ui.main_window import PAGE_PROJECTS, PAGE_WORKBENCH, MainWindow

        monkeypatch.setattr(pm, "_SETTINGS_FILE", tmp_path / "settings.json")
        monkeypatch.setattr(
            pm.platform_paths, "default_workspace_root", lambda: tmp_path / "ws"
        )
        win = MainWindow()
        assert win.stack.count() == 2
        assert win.stack.currentIndex() == PAGE_PROJECTS
        pm.create_project("切换项目", tmp_path / "ws")
        win.refresh_projects()
        assert win.project_list.count() == 1
        from PySide6.QtCore import Qt as _Qt

        item = win.project_list.item(0)
        win._open(item.data(_Qt.UserRole))
        assert win.stack.currentIndex() == PAGE_WORKBENCH
        assert "切换项目" in win.statusBar().currentMessage()
        win._back_to_projects()
        assert win.stack.currentIndex() == PAGE_PROJECTS
        win.close()

    def test_reopen_replaces_workbench(self, app, tmp_path, monkeypatch):
        """二次打开项目：旧工作台页被替换，连接被关闭，不泄漏。"""
        from costguard.core.models import project as pm
        from costguard.ui.main_window import PAGE_WORKBENCH, MainWindow

        monkeypatch.setattr(pm, "_SETTINGS_FILE", tmp_path / "settings.json")
        monkeypatch.setattr(
            pm.platform_paths, "default_workspace_root", lambda: tmp_path / "ws"
        )
        info = pm.create_project("重开项目", tmp_path / "ws")
        win = MainWindow()
        win._open(item_data := info)
        first_page = win.stack.widget(PAGE_WORKBENCH)
        win._open(item_data)
        second_page = win.stack.widget(PAGE_WORKBENCH)
        assert first_page is not second_page
        win.close()

    def test_new_project_remembers_custom_workspace_across_restart(
        self, app, tmp_path, monkeypatch
    ):
        from PySide6.QtWidgets import QDialog

        from costguard.core.models import project as pm
        from costguard.ui import main_window

        settings_file = tmp_path / "settings.json"
        default_root = tmp_path / "default"
        custom_root = tmp_path / "custom"
        monkeypatch.setattr(pm, "_SETTINGS_FILE", settings_file)
        monkeypatch.setattr(
            pm.platform_paths, "default_workspace_root", lambda: default_root
        )

        class FakeDialog:
            def __init__(self, parent=None):
                pass

            def exec(self):
                return QDialog.Accepted

            def values(self):
                return "重启仍可见", custom_root

        monkeypatch.setattr(main_window, "NewProjectDialog", FakeDialog)
        win = main_window.MainWindow()
        monkeypatch.setattr(win, "_open", lambda info: None)
        win._on_new()
        win.close()

        # 新窗口模拟应用重启；项目列表应从持久化设置中恢复。
        restarted = main_window.MainWindow()
        try:
            restarted.refresh_projects()
            names = [
                restarted.project_list.item(i).text()
                for i in range(restarted.project_list.count())
            ]
            assert names == ["重启仍可见"]
            assert pm.workspace_root() == custom_root
        finally:
            restarted.close()
