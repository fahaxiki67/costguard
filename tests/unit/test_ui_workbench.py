"""工作台 UI 测试（offscreen）：驱动完整业务流程。"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

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
        assert names == [
            "期次概览", "清单明细", "审核问题中心", "匹配复核", "成果导出",
            "版本与历史资产", "资料中心",
        ]

    def test_version_history_tab_is_readable_before_assets_exist(self, wb_page):
        wb_page.refresh_version_assets()
        assert wb_page.version_chain_table.rowCount() == 0
        assert wb_page.historical_price_table.rowCount() == 0
        assert "尚未形成两个可比较版本" in wb_page.version_compare_label.text()
        assert "历史单价" in wb_page.version_asset_status_label.text()

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

    def test_reclassifying_contract_refreshes_materialized_run_contract(
        self, wb_page, tmp_path, monkeypatch
    ):
        from PySide6.QtWidgets import QDialog, QMessageBox

        from jiadun.core.contracts import extract, run_contract
        from jiadun.ui import workbench

        source = tmp_path / "合同资料.txt"
        source.write_text("合同金额：1000元\n付款：30天", encoding="utf-8")
        extract.import_contract(
            wb_page.conn,
            wb_page.project.project_id,
            Path(wb_page.project_dir),
            source,
        )
        old_contract = run_contract.ensure_run_contract(
            wb_page.conn, wb_page.project.project_id
        )
        assert old_contract.components["contract_facts"]
        wb_page.refresh_documents()
        row_index = next(
            index
            for index in range(wb_page.document_table.rowCount())
            if wb_page.document_table.item(index, 0).text() == source.name
        )
        wb_page.document_table.selectRow(row_index)

        class FakeCategoryDialog:
            def __init__(self, *_args, **_kwargs):
                pass

            def setWindowTitle(self, _title):
                pass

            def exec(self):
                return QDialog.Accepted

            def category(self):
                return "other_agreement"

        monkeypatch.setattr(workbench, "ImportCategoryDialog", FakeCategoryDialog)
        monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: None)

        wb_page._reclassify_source_file()

        current = run_contract.get_current_contract(
            wb_page.conn, wb_page.project.project_id
        )
        assert current is not None
        assert current.run_id != old_contract.run_id
        assert current.components["contract_facts"] == []
        assert wb_page.conn.execute(
            "SELECT invalidated_at FROM run_contracts WHERE run_id=?",
            (old_contract.run_id,),
        ).fetchone()["invalidated_at"] is not None
        audit = wb_page.conn.execute(
            """SELECT action, target, before_json, after_json, run_id, run_signature
               FROM audit_log WHERE project_id=? AND action='reclassify_source_file'
               ORDER BY id DESC LIMIT 1""",
            (wb_page.project.project_id,),
        ).fetchone()
        assert audit is not None
        assert audit["target"].startswith("source_file:")
        assert '"category": "upward_contract"' in audit["before_json"]
        assert '"category": "other_agreement"' in audit["after_json"]
        assert audit["run_id"] == current.run_id
        assert audit["run_signature"] == current.signature

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

    def test_match_mirror_filters_keep_rows_and_search(self, wb_page):
        from jiadun.core.matching import matching

        groups = matching.match_items(wb_page.conn, wb_page.project.project_id)
        matching.save_matches(wb_page.conn, wb_page.project.project_id, groups)
        wb_page.refresh_matches()
        assert wb_page.match_search.placeholderText() == "搜索编码、名称或匹配理由"
        total = wb_page.match_table.rowCount()
        assert total > 0
        wb_page.match_search.setText("不存在的关键词")
        assert all(wb_page.match_table.isRowHidden(row) for row in range(total))
        wb_page.match_search.clear()
        wb_page.match_filter.setCurrentIndex(
            wb_page.match_filter.findData("pending")
        )
        assert any(
            not wb_page.match_table.isRowHidden(row)
            for row in range(total)
        )

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

    def test_export_status_obeys_shared_project_state(self, wb_page, monkeypatch):
        """共享摘要判定不可形成结论时，成果页不得自行拼出绿色状态。"""
        from jiadun.ui import workbench

        summary = SimpleNamespace(
            run_availability={"available": True, "reason": None},
            source_files=1,
            directions={"upward": 1},
            pending={
                "sheets": 0,
                "high_risk": 0,
                "anomalies": 0,
                "matches": 0,
                "manifest_status": "complete",
            },
            verification={
                "levels": {"sufficient": 1, "findings": 0, "insufficient": 0},
                "periods_checked": 1,
                "range_unproven_sheets": 0,
                "evidence_complete": False,
            },
            risk={"status": {"deferred": 0}},
            detection_coverage={"status": "complete"},
            aggregate_coverage={"status": "complete"},
            statuses={
                "project_status_code": "cannot_conclude",
                "project_status": "不可形成项目结论",
                "project_status_reason_codes": ["evidence_incomplete"],
                "period_status_code": "insufficient",
            },
        )
        monkeypatch.setattr(
            workbench,
            "build_report_model",
            lambda *_args, **_kwargs: SimpleNamespace(project_summary=summary),
        )

        wb_page.refresh_export_status()

        status = wb_page.export_status_label.text()
        assert "项目状态：不可形成项目结论" in status
        assert "审核完成度：当前未发现主要待处理事项" not in status

    def test_export_status_reads_registered_file_by_file_kind(
        self, wb_page, monkeypatch
    ):
        """已有成果文件时应按各自类型读取登记状态，不得引用未定义变量。"""
        from jiadun.core.contracts import run_contract

        export_dir = Path(wb_page.project_dir) / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        excel_path = export_dir / "价盾审核底稿_20260901_120000.xlsx"
        docx_path = export_dir / "价盾管理层摘要_20260901_120000.docx"
        excel_path.write_bytes(b"xlsx")
        docx_path.write_bytes(b"docx")

        def _status(_conn, _project_id, export_kind):
            path = excel_path if export_kind == "excel_workbook" else docx_path
            return [{"status": "current", "path": str(path)}]

        monkeypatch.setattr(run_contract, "export_status", _status)

        wb_page.refresh_export_status()

        assert "可用" in wb_page.export_card_values["excel"]["status"].text()
        assert excel_path.name in wb_page.export_card_values["excel"]["status"].text()
        assert "可用" in wb_page.export_card_values["docx"]["status"].text()
        assert docx_path.name in wb_page.export_card_values["docx"]["status"].text()

    def test_fail_closed_state_is_visible_in_workbench_and_export_cards(self, wb_page):
        from jiadun.core.contracts import run_contract

        run_contract.set_fail_closed_state(
            wb_page.conn,
            wb_page.project.project_id,
            reason="synthetic database is not writable",
        )
        wb_page.refresh_all()

        assert "尚无有效运行结果" in wb_page.status_label.text()
        assert "当前结果不可用" in wb_page.status_label.text()
        assert "尚无有效运行结果" in wb_page.export_status_label.text()
        assert "当前结果不可用" in wb_page.export_status_label.text()
        # fail-closed 的具体原因必须通过 reason 后缀透出，不得只给模糊措辞。
        assert "synthetic database is not writable" in wb_page.export_status_label.text()
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
    def test_startup_keeps_project_list_blank_until_explicit_refresh(
        self, app, tmp_path, monkeypatch
    ):
        """启动页不应扫描旧项目；查看已有项目必须是用户明确动作。"""
        from jiadun.core.models import project as pm
        from jiadun.ui.main_window import MainWindow

        monkeypatch.setattr(pm, "_SETTINGS_FILE", tmp_path / "settings.json")
        monkeypatch.setattr(
            pm.platform_paths, "default_workspace_root", lambda: tmp_path / "ws"
        )
        pm.create_project("启动前已存在", tmp_path / "ws")

        win = MainWindow()
        try:
            win.show()
            app.processEvents()
            assert win.project_list.count() == 0
            assert not win.project_list.isVisible()
            assert win.empty_box.isVisible()
            assert win.empty_drop_zone.isVisible()

            # “刷新”仍可显式查看已有项目，不能把用户的资料入口一并删掉。
            win.refresh_projects(include_known=False, include_legacy=False)
            assert win.project_list.count() == 1
            assert win.project_list.item(0).text() == "启动前已存在"
        finally:
            win.close()

    def test_project_snapshot_does_not_show_historical_risk_or_matches(
        self, app, tmp_path, monkeypatch
    ):
        """项目列表卡片的待办计数必须与共享摘要使用同一 current run。"""
        from jiadun.core.contracts import run_contract
        from jiadun.core.models import project as pm
        from jiadun.ui.main_window import MainWindow

        monkeypatch.setattr(pm, "_SETTINGS_FILE", tmp_path / "settings.json")
        monkeypatch.setattr(
            pm.platform_paths, "default_workspace_root", lambda: tmp_path / "ws"
        )
        info = pm.create_project("历史待办不串入卡片", tmp_path / "ws")
        info, conn = pm.open_project(Path(info.workspace_path))
        try:
            old = run_contract.ensure_run_contract(conn, info.project_id)
            with conn:
                conn.execute(
                    """INSERT INTO anomalies(
                           project_id, rule_id, severity, subject_type, subject_id,
                           message, status, created_at, run_signature, run_id
                       ) VALUES (?, 'historical_probe', 'high', 'project', ?,
                                 '历史高风险', 'open', '2026', ?, ?)""",
                    (info.project_id, info.project_id, old.signature, old.run_id),
                )
                conn.execute(
                    """INSERT INTO matches(
                           project_id, group_key, item_ids_json, level, method,
                           status, run_signature, run_id
                       ) VALUES (?, 'historical-group', '[]', 'suspected', 'none',
                                 'pending', ?, ?)""",
                    (info.project_id, old.signature, old.run_id),
                )
                conn.execute(
                    """INSERT INTO settlement_periods(
                           project_id, period_no, title, direction
                       ) VALUES (?, 1, '第1期', 'downward')""",
                    (info.project_id,),
                )
            new = run_contract.ensure_run_contract(conn, info.project_id)
            assert new.run_id != old.run_id
        finally:
            conn.close()

        snapshot = MainWindow._project_snapshot(info)
        assert snapshot["high"] == 0
        assert snapshot["matches"] == 0

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
