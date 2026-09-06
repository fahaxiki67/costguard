"""全工作簿 Sheet 清单浏览器对话框 UI 测试（offscreen，任务书任务 B 界面侧）。"""
import os

import pytest
from PySide6.QtWidgets import QMessageBox

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from jiadun.core.db import migrations  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def project_with_sheets(tmp_path, app):
    """项目 + 一个文件两个批次：批次1 已确认+标注，批次2 为重导新页。"""
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        pid = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("Sheet清单UI测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
        conn.execute(
            """INSERT INTO source_files(project_id, original_name, original_path,
               stored_path, file_type, size_bytes, sha256, imported_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pid, "结算工作簿.xlsx", str(tmp_path / "结算工作簿.xlsx"),
             str(tmp_path / "结算工作簿.xlsx"), "xlsx", 100,
             "0" * 64, "2026"),
        )
        fid = conn.execute("SELECT MAX(id) FROM source_files").fetchone()[0]
        for batch_no in (1, 2):
            cur = conn.execute(
                """INSERT INTO parse_batches(file_id, parser, parsed_at, status, stats_json)
                   VALUES (?,?,?,?,?)""",
                (fid, "pipeline", f"2026-09-06T0{batch_no}:00:00", "ok", "{}"),
            )
            bid = cur.lastrowid
            if batch_no == 1:
                conn.execute(
                    """INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name,
                       n_rows, n_cols, visible_state, sheet_status,
                       sheet_status_reason, list_kind)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (bid, 1, "分部分项清单", 100, 8, "visible", "confirmed",
                     "已人工确认", "boq_detail"),
                )
                conn.execute(
                    """INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name,
                       n_rows, n_cols, visible_state, sheet_status, sheet_status_reason)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (bid, 2, "封面", 3, 2, "hidden", "non_business", "非业务页"),
                )
            else:
                conn.execute(
                    """INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name,
                       n_rows, n_cols) VALUES (?,?,?,?,?)""",
                    (bid, 1, "分部分项清单", 100, 8),
                )
    yield conn, int(pid)
    conn.close()


def _open_dialog(env):
    from jiadun.ui.dialogs.sheet_browser import SheetBrowserDialog

    conn, pid = env
    return SheetBrowserDialog(conn, pid)


class TestSheetBrowserDialog:
    def test_lists_latest_batch_only_with_visibility_and_suggestion(
        self, project_with_sheets
    ):
        conn, pid = project_with_sheets
        dlg = _open_dialog(project_with_sheets)
        # 每文件只显示最新批次（批次2），所以只有 1 行
        assert dlg.table.rowCount() == 1
        row0 = [dlg.table.item(0, c).text() for c in range(9)]
        assert row0[1] == "分部分项清单"
        assert row0[2] == "未知"  # 新批次未经解析器落可见状态（直接插行无该列值）
        assert row0[5] == "分部分项清单"  # 建议角色
        assert row0[7] != ""  # 建议理由非空

    def test_keyword_filter_and_pending_mode(self, project_with_sheets):
        conn, pid = project_with_sheets
        dlg = _open_dialog(project_with_sheets)
        dlg.keyword_edit.setText("不存在的关键字")
        dlg._reload()
        assert dlg.table.rowCount() == 0
        dlg.keyword_edit.clear()
        dlg.mode_combo.setCurrentIndex(1)  # 仅待确认
        dlg._reload()
        # 最新批次唯一 Sheet 状态为默认 pending
        assert dlg.table.rowCount() == 1

    def test_human_annotation_roundtrip(self, project_with_sheets):
        conn, pid = project_with_sheets
        dlg = _open_dialog(project_with_sheets)
        dlg.table.selectRow(0)
        index = dlg.kind_combo.findData("measure_total")
        dlg.kind_combo.setCurrentIndex(index)
        dlg.reason_edit.setText("表名为总价措施项目清单且无数量列")
        dlg._annotate()
        stored = conn.execute(
            """SELECT rs.list_kind FROM raw_sheets rs
               JOIN parse_batches pb ON pb.id=rs.batch_id
               WHERE pb.id=(SELECT MAX(id) FROM parse_batches)"""
        ).fetchone()["list_kind"]
        assert stored == "measure_total"
        kinds = {
            r["kind"] for r in conn.execute(
                "SELECT DISTINCT kind FROM evidence WHERE project_id=?", (pid,)
            ).fetchall()
        }
        assert "sheet_list_kind" in kinds
        # 重载后人工标注列如实显示
        dlg._reload()
        assert dlg.table.item(0, 8).text() == "总价措施（费率计取）"

    def test_annotation_requires_reason(self, project_with_sheets, monkeypatch):
        conn, pid = project_with_sheets
        dlg = _open_dialog(project_with_sheets)
        dlg.table.selectRow(0)
        dlg.reason_edit.clear()
        # 屏蔽模态弹窗（offscreen 下 QMessageBox 会阻塞）
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
        dlg._annotate()
        stored = conn.execute(
            """SELECT rs.list_kind FROM raw_sheets rs
               JOIN parse_batches pb ON pb.id=rs.batch_id
               WHERE pb.id=(SELECT MAX(id) FROM parse_batches)"""
        ).fetchone()["list_kind"]
        assert stored in (None, "unknown")  # 未写入
