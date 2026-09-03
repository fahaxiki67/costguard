"""合同条款确认对话框 UI 测试（offscreen）。"""
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from tests.unit.test_contract_extract import SAMPLE_CONTRACT  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def page_with_contract(tmp_path, app):
    """创建项目并导入一份合成合同，返回 (workbench_page, conn, project_id)。"""
    import docx as docx_lib

    from jiadun.core.contracts import extract
    from jiadun.core.models import project as pm
    from jiadun.ui.workbench import WorkbenchPage

    info = pm.create_project("合同确认测试", tmp_path / "ws")
    info, conn = pm.open_project(Path(info.workspace_path))
    src = tmp_path / "分包合同.docx"
    d = docx_lib.Document()
    for line in SAMPLE_CONTRACT.splitlines():
        d.add_paragraph(line)
    d.save(str(src))
    extract.import_contract(conn, info.project_id, Path(info.workspace_path), src)
    page = WorkbenchPage(conn, info, Path(info.workspace_path), on_back=lambda: None)
    yield page, conn, info.project_id
    conn.close()


class TestContractReviewDialog:
    def _open_dialog(self, page, pid):
        from jiadun.ui.dialogs.contract_review import ContractReviewDialog

        dlg = ContractReviewDialog(page.conn, pid, page)
        return dlg

    def test_candidate_rows_listed(self, page_with_contract):
        page, conn, pid = page_with_contract
        dlg = self._open_dialog(page, pid)
        assert dlg.table.rowCount() >= 6  # 合成合同含多条条款
        status_col = 4
        statuses = {
            dlg.table.item(r, status_col).text() for r in range(dlg.table.rowCount())
        }
        assert statuses == {"候选"}  # 默认只看候选

    def test_confirm_updates_row_and_database(self, page_with_contract):
        page, conn, pid = page_with_contract
        dlg = self._open_dialog(page, pid)
        dlg.table.selectRow(0)
        fact_id = dlg._rows[dlg.table.currentRow()]["id"]
        dlg.reason_edit.setText("与原文核对一致")
        dlg._apply("confirmed")
        # 候选筛选下，已确认行不再出现在列表里
        statuses = {
            dlg.table.item(r, 4).text() for r in range(dlg.table.rowCount())
        }
        assert "已确认" not in statuses
        row = conn.execute(
            "SELECT review_status FROM contract_facts WHERE id=?", (fact_id,)
        ).fetchone()
        assert row["review_status"] == "confirmed"

    def test_overturn_without_reason_blocked(self, page_with_contract, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        page, conn, pid = page_with_contract
        dlg = self._open_dialog(page, pid)
        dlg.status_filter.setCurrentIndex(2)  # 已确认
        assert dlg.table.rowCount() == 0
        dlg.status_filter.setCurrentIndex(3)  # 已拒绝（空）→ 切回全部验证流程
        dlg.status_filter.setCurrentIndex(4)  # 全部
        dlg.table.selectRow(0)
        # 先确认为已确认
        dlg.reason_edit.setText("核对一致")
        dlg._apply("confirmed")
        # 重新选中同一条款（全部筛选下），不填理由尝试拒绝 → 弹窗阻断
        dlg.table.selectRow(0)
        assert dlg.table.item(dlg.table.currentRow(), 4).text() == "已确认"
        warnings: list[tuple] = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            staticmethod(lambda *a, **k: warnings.append(a) or QMessageBox.StandardButton.Ok),
        )
        dlg.reason_edit.clear()
        dlg._apply("rejected")
        assert warnings, "无理由推翻必须触发阻断提示"
