"""逐页对照复核对话框 UI 测试（offscreen）。"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from tests.unit.test_pdf_page_pipeline import (  # noqa: E402
    FakeOcrProvider,
    FakeRenderer,
    _mixed_pages,
    _ocr_result,
    _pdf_copy,
)

from jiadun.core.contracts import extract  # noqa: E402
from jiadun.core.db import migrations  # noqa: E402
from jiadun.core.document_intake import list_documents  # noqa: E402


@pytest.fixture()
def project_db(tmp_path):
    """与页级管线测试相同的库与工程夹具（自包含，避免跨文件调用夹具）。"""
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        project_id = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("页级复核UI测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
    yield conn, int(project_id), tmp_path
    conn.close()


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def needs_review_doc(project_db, tmp_path):
    conn, pid, project_dir = project_db
    source = _pdf_copy(tmp_path)
    provider = FakeOcrProvider({2: _ocr_result("付款比例为 80%")})
    extract.import_contract(
        conn, pid, project_dir, source,
        document_category="upward_contract",
        pdf_renderer=FakeRenderer(_mixed_pages()),
        ocr_provider=provider,
    )
    fid = int(conn.execute(
        "SELECT file_id FROM contract_docs WHERE project_id=? ORDER BY id DESC LIMIT 1",
        (pid,),
    ).fetchone()["file_id"])
    return conn, pid, fid


def _open_dialog(needs_review_doc, app):
    from jiadun.ui.dialogs.page_review import PageReviewDialog

    conn, pid, fid = needs_review_doc
    return PageReviewDialog(conn, pid, fid, "混合合同 副本.pdf")


class TestPageReviewDialog:
    def test_pages_listed_and_finish_disabled(self, needs_review_doc, app):
        dlg = _open_dialog(needs_review_doc, app)
        assert dlg.table.rowCount() == 3
        assert dlg.finish_btn.isEnabled() is False
        assert dlg.table.item(1, 2).text() == "ocr"

    def test_verify_without_reason_blocked(self, needs_review_doc, app, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        dlg = _open_dialog(needs_review_doc, app)
        dlg.table.selectRow(1)  # 第 2 页 OCR
        warnings: list[tuple] = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            staticmethod(lambda *a, **k: warnings.append(a) or QMessageBox.StandardButton.Ok),
        )
        dlg.reason_edit.clear()
        dlg._verify_page()
        assert warnings, "核实一页不填对照依据必须被阻断"

    def test_verify_then_finish_unblocks_document(self, needs_review_doc, app, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        conn, pid, fid = needs_review_doc
        dlg = _open_dialog(needs_review_doc, app)
        dlg.table.selectRow(1)
        dlg.reason_edit.setText("与原件第 2 页逐行核对一致")
        dlg._verify_page()
        assert dlg.finish_btn.isEnabled() is True
        infos: list[tuple] = []
        monkeypatch.setattr(
            QMessageBox, "information",
            staticmethod(lambda *a, **k: infos.append(a) or QMessageBox.StandardButton.Ok),
        )
        dlg._finish()
        assert infos, "完成复核后应给出成功提示"
        assert dlg.result() == dlg.DialogCode.Accepted
        intake = {d["file_id"]: d for d in list_documents(conn, pid)}[fid]
        assert intake["parse_status"] == "parsed"
