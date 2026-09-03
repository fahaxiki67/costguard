"""资料中心、扫描 PDF 边界与后台导入的回归测试。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_intake_categories_keep_business_direction_user_controlled():
    from jiadun.core import document_intake

    categories = {item.code: item for item in document_intake.DOCUMENT_CATEGORIES}
    assert categories["unclassified"].parse_strategy == "evidence_only"
    assert categories["upward_contract"].parse_strategy == "contract"
    assert categories["upward_framework_management"].direction == "upward"
    assert categories["upward_audit_report"].parse_strategy == "control_candidate"
    assert categories["downward_material_settlement"].direction == "downward"
    assert categories["downward_subcontract_settlement"].parse_strategy == "settlement"
    # 任何未知值都必须保守落到人工分类，不得根据字符串/文件名推断业务方向。
    assert document_intake.category_for("对下第七期结算").code == "unclassified"


def test_scanned_contract_pdf_is_persisted_as_pending_ocr(tmp_path: Path):
    pypdf = pytest.importorskip("pypdf")
    from jiadun.core import document_intake
    from jiadun.core.contracts import extract
    from jiadun.core.models import project as project_model

    info = project_model.create_project("扫描合同", tmp_path / "workspace")
    info, conn = project_model.open_project(Path(info.workspace_path))
    scanned = tmp_path / "扫描合同.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with scanned.open("wb") as handle:
        writer.write(handle)
    try:
        with pytest.raises(NotImplementedError, match="OCR"):
            extract.import_contract(conn, info.project_id, Path(info.workspace_path), scanned)
        rows = document_intake.list_documents(conn, info.project_id)
        assert len(rows) == 1
        assert rows[0]["parse_status"] == "pending_ocr"
        assert "扫描件" in rows[0]["detail"]
        assert conn.execute(
            "SELECT COUNT(*) FROM contract_docs WHERE project_id=?", (info.project_id,)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_import_worker_returns_for_scanned_pdf_and_keeps_document_visible(tmp_path: Path):
    pytest.importorskip("PySide6")
    pypdf = pytest.importorskip("pypdf")
    from jiadun.core import document_intake
    from jiadun.core.models import project as project_model
    from jiadun.ui.workbench import ImportWorker

    info = project_model.create_project("后台扫描件", tmp_path / "workspace")
    info, conn = project_model.open_project(Path(info.workspace_path))
    scanned = tmp_path / "无文本层.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with scanned.open("wb") as handle:
        writer.write(handle)
    completed: list[dict] = []
    worker = ImportWorker(info.workspace_path, info.project_id, [scanned], "upward_contract")
    worker.finished.connect(completed.append)
    try:
        # 直接执行 worker 的业务函数用于确定性回归；真实 UI 中它运行在 QThread，
        # 因此 PDF 解析不会占用 Qt 主事件循环。
        worker.run()
        assert completed and completed[0]["partial"]
        rows = document_intake.list_documents(conn, info.project_id)
        assert rows[0]["parse_status"] == "pending_ocr"
    finally:
        conn.close()


def test_import_worker_emits_completion_when_selection_has_no_supported_files(tmp_path: Path):
    pytest.importorskip("PySide6")
    from jiadun.core.models import project as project_model
    from jiadun.ui.workbench import ImportWorker

    info = project_model.create_project("后台空目录", tmp_path / "workspace")
    info, conn = project_model.open_project(Path(info.workspace_path))
    unsupported = tmp_path / "说明图片.jpg"
    unsupported.write_bytes(b"not an importable document")
    completed: list[dict] = []
    worker = ImportWorker(info.workspace_path, info.project_id, [unsupported], "unclassified")
    worker.finished.connect(completed.append)
    try:
        worker.run()
        assert completed == [
            {
                "ok": 0,
                "partial": [],
                "fail": [],
                "pending": 0,
                "category": "unclassified",
                "skipped": (unsupported,),
                "skipped_details": ((unsupported, "不支持的文件类型"),),
            }
        ]
    finally:
        conn.close()


def test_workbench_documents_tab_exposes_registered_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from jiadun.core.models import project as project_model
    from jiadun.ui.workbench import WorkbenchPage

    QApplication.instance() or QApplication([])
    info = project_model.create_project("资料中心", tmp_path / "workspace")
    info, conn = project_model.open_project(Path(info.workspace_path))
    source = tmp_path / "财务台账.txt"
    source.write_text("仅用于归档", encoding="utf-8")
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    page = WorkbenchPage(conn, info, info.workspace_path, on_back=lambda: None)
    try:
        # 兼容入口仍按旧策略解析 TXT；资料台账必须独立可见，且没有利用文件名
        # 给它擅自标记“对下”。
        page.import_paths([source])
        page.refresh_documents()
        assert page.document_table.rowCount() == 1
        assert page.document_table.item(0, 1).text() == "待人工分类"
        page.document_table.selectRow(0)
        page._show_source_file_detail()
        assert "SHA-256" in page.document_detail.toPlainText()
    finally:
        conn.close()
        page.deleteLater()
