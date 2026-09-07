"""全工作簿 Sheet 清单对话框 UI 测试（offscreen）。"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from jiadun.core.db import migrations  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def project_db(tmp_path, app):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        pid = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("Sheet清单UI测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
    yield conn, int(pid), tmp_path
    conn.close()


def _add_file_with_sheets(conn, pid, tmp_path, name, sheets):
    """登记文件与批次并写入若干 Sheet（name, status, reason, n_rows）。"""
    from jiadun.core.models.source_file import import_file

    src = tmp_path / name
    src.write_bytes(b"placeholder")
    sf = import_file(conn, pid, tmp_path, src)
    now = "2026-09-07T00:00:00"
    with conn:
        cur = conn.execute(
            """INSERT INTO parse_batches(file_id, parser, parsed_at, status, stats_json)
               VALUES (?,?,?,?,?)""",
            (sf.file_id, "pipeline", now, "ok", "{}"),
        )
        batch_id = cur.lastrowid
        for index, (sheet_name, status, reason, n_rows) in enumerate(sheets, start=1):
            conn.execute(
                """INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name,
                   n_rows, n_cols, sheet_status, sheet_status_reason)
                   VALUES (?,?,?,?,?,?,?)""",
                (batch_id, index, sheet_name, n_rows, 8, status, reason),
            )
    return sf.file_id


def _stub_boxes(monkeypatch) -> list[tuple[str, tuple]]:
    """桩掉全部模态弹窗：offscreen 下任何未打桩的 exec() 都会挂死测试进程。"""
    from PySide6.QtWidgets import QMessageBox

    seen: list[tuple[str, tuple]] = []
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox,
            name,
            staticmethod(
                lambda *a, __kind=name, __seen=seen: (
                    __seen.append((__kind, a)),
                    QMessageBox.StandardButton.Ok,
                )[1]
            ),
        )
    return seen


def _open_dialog(env):
    from jiadun.ui.dialogs.sheet_inventory import SheetInventoryDialog

    conn, pid, _ = env
    return SheetInventoryDialog(conn, pid)


class TestSheetInventoryDialog:
    def test_lists_all_sheets_with_status_and_suggestion(self, project_db):
        conn, pid, tmp_path = project_db
        _add_file_with_sheets(
            conn,
            pid,
            tmp_path,
            "a.xlsx",
            [
                ("分部分项工程量清单计价表", "confirmed", "已进入结算模型", 120),
                ("封面", "non_business", "存证角色确认不进入结算模型", 1),
            ],
        )
        dlg = _open_dialog(project_db)
        assert dlg.table.rowCount() == 2
        names = {dlg.table.item(r, 1).text() for r in range(dlg.table.rowCount())}
        assert names == {"分部分项工程量清单计价表", "封面"}
        # 状态中文标签 + 建议类型候选
        assert dlg.table.item(0, 2).text() == "已确认"
        assert dlg.table.item(0, 4).text() == "分部分项清单"
        assert dlg.table.item(1, 2).text() == "非业务表"
        assert "非业务" in dlg.table.item(1, 4).text()

    def test_keyword_and_status_filters(self, project_db):
        conn, pid, tmp_path = project_db
        _add_file_with_sheets(
            conn,
            pid,
            tmp_path,
            "a.xlsx",
            [
                ("分部分项工程量清单", "pending", "待人工确认", 10),
                ("汇总表", "pending", "待人工确认", 5),
            ],
        )
        dlg = _open_dialog(project_db)
        dlg.keyword_edit.setText("汇总")
        assert dlg.table.rowCount() == 1
        assert dlg.table.item(0, 1).text() == "汇总表"
        dlg.keyword_edit.setText("")
        dlg.status_combo.setCurrentIndex(dlg.status_combo.findData("confirmed"))
        assert dlg.table.rowCount() == 0
        assert "共 0 个工作表" in dlg.summary_label.text()

    def test_annotate_requires_reason_and_persists(self, project_db, monkeypatch):
        conn, pid, tmp_path = project_db
        _add_file_with_sheets(
            conn,
            pid,
            tmp_path,
            "a.xlsx",
            [
                ("安全文明措施费", "pending", "待人工确认", 8),
            ],
        )
        _stub_boxes(monkeypatch)
        from PySide6.QtWidgets import QDialog

        import jiadun.ui.dialogs.sheet_inventory as mod
        from jiadun.ui.dialogs.sheet_inventory import KindSelectDialog

        # 空理由被 KindSelectDialog 拒绝（accept 拦截，不进审计）
        kind_dlg = KindSelectDialog("安全文明措施费", "unknown")
        kind_dlg.kind_combo.setCurrentIndex(kind_dlg.kind_combo.findData("measure_total"))
        kind_dlg.reason_edit.setPlainText("")
        kind_dlg.accept()
        assert kind_dlg.result() != QDialog.DialogCode.Accepted

        # 理由齐全：打桩 exec 后经 _annotate_selected 走完整链路
        class _StubKindDialog:
            def __init__(self, sheet_name, current_kind, parent=None):
                self.init_args = (sheet_name, current_kind)

            def exec(self):
                return QDialog.DialogCode.Accepted

            def annotation(self):
                return ("measure_total", "表名为总价措施特征词，人工核对费率计取")

        monkeypatch.setattr(mod, "KindSelectDialog", _StubKindDialog)
        dlg = _open_dialog(project_db)
        dlg.table.selectRow(0)
        dlg._annotate_selected()
        assert dlg.table.item(0, 6).text() == "总价措施"
        row = conn.execute("SELECT list_kind FROM raw_sheets WHERE sheet_name='安全文明措施费'").fetchone()
        assert row["list_kind"] == "measure_total"
        ev = conn.execute(
            """SELECT COUNT(*) AS c FROM evidence
               WHERE kind='sheet_list_kind'"""
        ).fetchone()
        assert ev["c"] == 1

    def test_annotate_without_selection_prompts(self, project_db, monkeypatch):
        conn, pid, tmp_path = project_db
        _add_file_with_sheets(
            conn,
            pid,
            tmp_path,
            "a.xlsx",
            [
                ("汇总表", "pending", "待人工确认", 5),
            ],
        )
        seen = _stub_boxes(monkeypatch)
        dlg = _open_dialog(project_db)
        dlg.table.clearSelection()
        dlg._annotate_selected()
        assert seen and seen[0][0] == "information"

    def test_human_annotation_survives_reload(self, project_db, monkeypatch):
        """人工标注不被机器建议覆盖（B4）。"""
        conn, pid, tmp_path = project_db
        _add_file_with_sheets(
            conn,
            pid,
            tmp_path,
            "a.xlsx",
            [
                ("封面", "non_business", "存证角色确认不进入结算模型", 1),
            ],
        )
        from jiadun.core.engine import sheet_inventory as inv

        inv.set_sheet_list_kind(conn, pid, 1, "non_business", reason="封面页，登记为存证")
        dlg = _open_dialog(project_db)
        assert dlg.table.item(0, 4).text() == "非业务表"  # 建议列显示已人工标注
        assert dlg.table.item(0, 6).text() == "非业务表"
