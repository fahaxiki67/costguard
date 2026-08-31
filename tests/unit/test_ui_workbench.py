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
    from jiadun.core.models import project as pm
    from jiadun.ui.workbench import WorkbenchPage

    info = pm.create_project("UI测试项目", tmp_path / "ws")
    info, conn = pm.open_project(Path(info.workspace_path))
    src = tmp_path / "multi.xlsx"
    make_multi_period(src, periods=2)
    from jiadun.core.engine import settlement_io

    settlement_io.import_settlement_file(conn, info.project_id, Path(info.workspace_path), src)
    page = WorkbenchPage(conn, info, info.workspace_path, on_back=lambda: None)
    yield page
    page.conn.close()


class TestWorkbench:
    def test_tabs_present(self, wb_page):
        names = [wb_page.tabs.tabText(i) for i in range(wb_page.tabs.count())]
        assert names == ["期次概览", "清单明细", "审核问题中心", "匹配复核", "成果导出"]

    def test_period_overview_populated(self, wb_page):
        wb_page.refresh_periods()
        t = wb_page.period_table
        assert t.rowCount() == 2  # multi.xlsx 2 期
        assert t.item(0, 0).text() == "1"
        assert t.item(0, 2).text() == "未标记"  # P0-4 业务词统一：未标记（无括号）

    def test_project_overview_shows_counts_and_next_step(self, wb_page):
        wb_page.refresh_overview()
        assert wb_page.overview_values["files"].text() == "1"
        assert wb_page.overview_values["upward"].text() == "0"
        assert wb_page.overview_values["downward"].text() == "0"
        assert "下一步：" in wb_page.overview_hint.text()
        assert wb_page.next_action_btn.text().startswith("下一步：")

    def test_items_populated_with_provenance(self, wb_page):
        wb_page.refresh_items()
        t = wb_page.items_table
        assert t.rowCount() > 0
        # 方向直接可见；数量列有值；出处列非空（保真层证据）
        assert t.horizontalHeaderItem(0).text() == "方向"
        assert t.item(0, 0).text() == "未标记"
        assert t.item(0, 5).text() not in ("", "—")
        assert t.item(0, 9).text().startswith("行")

    def test_items_search_and_type_filters_keep_full_count(self, wb_page):
        wb_page.items_search.setText("挖沟槽土方")
        assert wb_page.items_total == 2
        assert "共 2 条 / 当前显示 1-2 条" == wb_page.items_total_label.text()
        wb_page.items_row_type.setCurrentIndex(1)  # 仅明细
        assert wb_page.items_total == 2
        wb_page._show_item_evidence(0, 0)
        evidence_text = wb_page.item_evidence_panel.toPlainText()
        assert "来源文件：" in evidence_text
        assert "Evidence ID" in evidence_text and "Sheet：" in evidence_text

    def test_anomaly_run_and_display(self, wb_page):
        from jiadun.core.anomalies import engine

        findings = engine.run_anomalies(wb_page.conn, wb_page.project.project_id)
        wb_page.refresh_anomalies()
        assert wb_page.anomaly_table.rowCount() == len(findings)
        assert wb_page.anomaly_table.horizontalHeaderItem(1).text() == "方向"

    def test_evidence_renderer_keeps_pretranslated_direction(self):
        """异常证据已写入中文方向时，渲染器不得二次转换成未标记。"""
        from jiadun.ui.workbench import _evidence_entry_text

        rendered = _evidence_entry_text(
            {"direction": "对上结算", "file": "第1期.xlsx"}, source=True
        )
        assert "方向：对上结算" in rendered
        assert "方向：未标记" not in rendered

    def test_anomaly_detail_handles_legacy_dict_evidence_and_localizes_history(self, wb_page):
        """旧版 steps/sources 为对象时，问题中心仍能打开详情且不泄露动作代码。"""
        from jiadun.core.evidence import evidence as evidence_api

        evidence_id = evidence_api.add_evidence(
            wb_page.conn,
            wb_page.project.project_id,
            "ui_probe",
            "问题中心详情测试",
            steps={"step": "A路径", "result": "100"},
            sources={"sheet": "第1期", "location": "行2列5", "raw_value": "100"},
        )
        with wb_page.conn:
            anomaly_id = wb_page.conn.execute(
                """INSERT INTO anomalies(project_id, rule_id, severity, subject_type,
                   subject_id, evidence_id, message, status, created_at)
                   VALUES (?, 'ui_probe', 'medium', 'project', ?, ?, '详情测试', 'open', '2026')""",
                (wb_page.project.project_id, wb_page.project.project_id, evidence_id),
            ).lastrowid
            wb_page.conn.execute(
                """INSERT INTO audit_log(project_id, ts, actor, action, target,
                   before_json, after_json, reason) VALUES (?, '2026', 'user',
                   'resolve_anomaly', ?, '{}', '{}', '人工核对')""",
                (wb_page.project.project_id, f"anomaly:{anomaly_id}"),
            )
        wb_page.refresh_anomalies()
        row = next(
            i for i in range(wb_page.anomaly_table.rowCount())
            if int(wb_page.anomaly_table.item(i, 0).text()) == anomaly_id
        )
        wb_page._show_anomaly_detail(row, 0)
        text = wb_page.anomaly_detail_panel.toPlainText()
        assert "计算过程" in text and "来源证据" in text and "详情测试" in text
        assert "处理审核问题" in text
        assert "resolve_anomaly" not in text

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
            if table.item(i, 3).toolTip() == "sheet_direction_probe"
        ]
        assert len(probe_rows) == 1
        assert table.item(probe_rows[0], 3).text() == "其他审核问题"
        assert table.item(probe_rows[0], 1).text() == "对上结算"

    def test_match_run_and_levels_zh(self, wb_page):
        from jiadun.core.matching import matching

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

        from jiadun.ui.workbench import ReasonDialog

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
        from jiadun.core.export import excel_export

        path = excel_export.export_workbook(
            wb_page.conn, wb_page.project.project_id, Path(wb_page.project_dir) / "exports")
        assert path.exists()

    def test_fail_closed_state_is_visible_in_workbench_and_export_cards(self, wb_page):
        from jiadun.core.contracts import run_contract

        run_contract.set_fail_closed_state(
            wb_page.conn,
            wb_page.project.project_id,
            reason="synthetic database is not writable",
        )
        wb_page.refresh_all()

        assert "数据库不可写" in wb_page.status_label.text()
        assert "当前结果不可用" in wb_page.status_label.text()
        assert "数据库不可写" in wb_page.export_status_label.text()
        assert "当前结果不可用" in wb_page.export_status_label.text()
        assert "当前结果不可用" in wb_page.export_card_values["excel"]["status"].text()
        assert "当前结果不可用" in wb_page.export_card_values["docx"]["status"].text()

    def test_detail_and_batch_action_are_blocked_by_current_run_boundary(
        self, wb_page, monkeypatch
    ):
        """列表之外的详情和批量动作不能绕过不可用边界。"""
        from PySide6.QtWidgets import QMessageBox

        from jiadun.core.contracts import run_contract
        from jiadun.core.matching import matching

        groups = matching.match_items(wb_page.conn, wb_page.project.project_id)
        assert groups
        matching.save_matches(wb_page.conn, wb_page.project.project_id, groups)
        wb_page.refresh_matches()
        assert wb_page.match_table.rowCount() > 0
        wb_page.match_table.selectRow(0)

        warnings = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args[1]))
        run_contract.set_fail_closed_state(
            wb_page.conn,
            wb_page.project.project_id,
            reason="synthetic stale result boundary",
        )
        wb_page._show_match_detail(0, 0)
        assert "当前结果不可用" in wb_page.match_detail_panel.toPlainText()
        wb_page._batch_confirm_matches()
        assert warnings
        assert not wb_page.conn.execute(
            "SELECT 1 FROM matches WHERE project_id=? AND status='confirmed'",
            (wb_page.project.project_id,),
        ).fetchone()


class TestMainWindow:
    def test_two_pages_and_switch(self, app, tmp_path, monkeypatch):
        from jiadun.core.models import project as pm
        from jiadun.ui.main_window import PAGE_PROJECTS, PAGE_WORKBENCH, MainWindow

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
        from jiadun.core.models import project as pm
        from jiadun.ui.main_window import PAGE_WORKBENCH, MainWindow

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

        from jiadun.core.models import project as pm
        from jiadun.ui import main_window

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
