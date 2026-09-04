"""费率规则对话框 UI 测试（offscreen）。"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from jiadun.core import document_intake  # noqa: E402
from jiadun.core.db import migrations  # noqa: E402
from jiadun.core.models.source_file import import_file  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def env_with_framework_doc(tmp_path, app):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        pid = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("费率UI测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
    src = tmp_path / "框架协议.txt"
    src.write_text("协作管理费按对上结算价的3%收取。", encoding="utf-8")
    sf = import_file(conn, pid, tmp_path, src)
    document_intake.record_document(
        conn, pid, sf.file_id, category="upward_framework_management",
        parse_status="evidence_only", detail="", parser="",
    )
    yield conn, pid, sf
    conn.close()


def _open_dialog(env):
    from jiadun.ui.dialogs.rate_rules import RateRulesDialog

    conn, pid, _ = env
    return RateRulesDialog(conn, pid)


def _stub_boxes(monkeypatch) -> list[tuple[str, tuple]]:
    """桩掉全部模态弹窗：offscreen 下任何未打桩的 exec() 都会挂死测试进程。"""
    from PySide6.QtWidgets import QMessageBox

    seen: list[tuple[str, tuple]] = []
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(
                lambda *a, __kind=name, __seen=seen: (
                    __seen.append((__kind, a)) or QMessageBox.StandardButton.Ok
                )
            ),
        )
    return seen


class TestRateRulesDialog:
    def test_scan_creates_candidates(self, env_with_framework_doc, monkeypatch):
        _stub_boxes(monkeypatch)
        dlg = _open_dialog(env_with_framework_doc)
        assert dlg.doc_combo.currentData() is not None
        dlg._scan()
        assert dlg.table.rowCount() == 1
        assert dlg.table.item(0, 1).text() == "3%"
        assert dlg.table.item(0, 2).text() == "候选"

    def test_confirm_requires_base_then_apply(self, env_with_framework_doc, monkeypatch):
        boxes = _stub_boxes(monkeypatch)

        conn, pid, sf = env_with_framework_doc
        dlg = _open_dialog(env_with_framework_doc)
        dlg._scan()
        dlg.table.selectRow(0)
        # 缺基数类型 → 阻断
        dlg.base_def_edit.clear()
        dlg._confirm()
        assert any(kind == "warning" for kind, _ in boxes), "未设定基数类型必须被阻断"
        # 正确设定后确认 + 试算
        dlg.base_type_combo.setCurrentIndex(1)  # 按对上结算价
        dlg.base_def_edit.setText("按对上结算审定金额，含税")
        dlg._confirm()
        assert dlg.table.item(0, 2).text() == "已确认"
        dlg.calc_edit.setText("1000000")
        dlg._apply()
        assert "30000.00" in dlg.calc_label.text()
