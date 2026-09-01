"""人工确认工作表 GUI 验收测试（issue #5，共享 UI）。

覆盖：
1. header_detect：小计词补"本页小计/本页合计"（支付报表按页小计行）；
2. workbench.SheetConfirmDialog：被门控工作表的人工确认闭环（候选预填、
   校验规则、按清单抽取、仅存证），全程原因必填并写审计。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "demo"


class TestSubtotalPageWords:
    def test_page_subtotal_words_flagged(self):
        from jiadun.core.parsing.header_detect import is_subtotal_row

        for text in ("本页小计", "本页合计", "小计", "分部小计"):
            assert is_subtotal_row(text, ""), f"「{text}」应被识别为小计行"
        # 词内含"部分/合计"的普通清单名不受影响
        assert not is_subtotal_row("钢筋合计用量表", "")
        assert not is_subtotal_row("部分项工程说明", "")


@pytest.fixture()
def quiet_boxes(monkeypatch):
    """打桩模态弹窗：offscreen 下 QMessageBox 会嵌套事件循环永久阻塞。"""
    from PySide6.QtWidgets import QMessageBox

    calls = {"information": [], "warning": [], "question": []}
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda *a, **k: calls["information"].append(a) or QMessageBox.Ok))
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: calls["warning"].append(a) or QMessageBox.Ok))
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: calls["question"].append(a) or QMessageBox.No))
    return calls


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def gated_project(tmp_path):
    """导入附表文件，制造一个被角色门控的「人材机汇总」工作表。"""
    from jiadun.core.engine import settlement_io
    from jiadun.core.models import project as pm

    info = pm.create_project("确认对话框测试", tmp_path / "ws")
    info, conn = pm.open_project(Path(info.workspace_path))
    settlement_io.import_settlement_file(
        conn, info.project_id, Path(info.workspace_path),
        DEMO_DIR / "演示-对下结算-附表.xlsx", direction="downward")
    return conn, info.project_id


class TestSheetConfirmDialog:
    def _dialog(self, conn, pid):
        from jiadun.ui.workbench import SheetConfirmDialog

        return SheetConfirmDialog(conn, pid)

    def test_pending_sheet_listed_states_and_prefill(self, qapp, quiet_boxes, gated_project):
        conn, pid = gated_project
        dlg = self._dialog(conn, pid)
        assert dlg.sheet_list.count() == 1
        item_text = dlg.sheet_list.item(0).text()
        assert "人材机汇总" in item_text
        # 语义门控页没有 table_headers 行：显示"无表头"，不预填（程序不猜测）
        dlg._on_select(0)
        assert "无表头" in dlg.sheet_list.item(0).text()
        assert all(sp.value() == 0 for sp in dlg._col_spins.values())
        assert dlg.preview_table.rowCount() > 0 and dlg.preview_table.columnCount() > 0
        dlg.preview_field_combo.setCurrentIndex(1)  # 名称
        dlg._preview_column_clicked(1)  # 点击预览第 2 列
        assert dlg._col_spins["name"].value() == 2

        # needs_review（有自动识别候选）类页面：预填候选列映射与表头行
        sheet_id = conn.execute(
            "SELECT id FROM raw_sheets WHERE sheet_name='人材机汇总'").fetchone()["id"]
        import json as _json

        with conn:
            conn.execute(
                """INSERT INTO table_headers(sheet_id, header_row_lo, header_row_hi,
                   col_map_json, confidence, needs_review) VALUES (?,?,?,?,?,?)""",
                (sheet_id, 2, 2,
                 _json.dumps({"code": 1, "name": 2, "unit": 3, "quantity": 4,
                              "unit_price": 5, "amount": 6}),
                 0.9, 1))
        dlg.reload()
        dlg._on_select(0)
        assert "表头歧义" in dlg.sheet_list.item(0).text()
        assert dlg._col_spins["name"].value() == 2
        assert dlg._col_spins["quantity"].value() == 4
        assert dlg._col_spins["amount"].value() == 6
        assert dlg.hdr_lo.value() == 2 and dlg.hdr_hi.value() == 2
        assert "示例：" in dlg._col_hints["name"].text()
        conn.close()

    def test_validation_rules(self, qapp, quiet_boxes, gated_project):
        conn, pid = gated_project
        dlg = self._dialog(conn, pid)
        dlg._on_select(0)
        dlg.reason_edit.setPlainText("核对依据")
        # 缺名称列
        for spin in dlg._col_spins.values():
            spin.setValue(0)
        with pytest.raises(ValueError, match="名称"):
            dlg._validated()
        # 缺金额口径
        dlg._col_spins["name"].setValue(2)
        with pytest.raises(ValueError, match="金额口径"):
            dlg._validated()
        # 同列冲突
        dlg._col_spins["quantity"].setValue(2)
        dlg._col_spins["unit_price"].setValue(3)
        with pytest.raises(ValueError, match="同一列"):
            dlg._validated()
        # 无原因
        dlg.reason_edit.setPlainText("")
        dlg._col_spins["amount"].setValue(6)
        with pytest.raises(ValueError, match="原因必填"):
            dlg._validated()
        conn.close()

    def test_extract_writes_items_and_audit(self, qapp, quiet_boxes, gated_project):
        conn, pid = gated_project
        dlg = self._dialog(conn, pid)
        dlg._on_select(0)
        # 模拟人工选列（人材机汇总表：序号1/名称2/单位3/数量4/单价5/金额6）
        dlg._col_spins["name"].setValue(2)
        dlg._col_spins["unit"].setValue(3)
        dlg._col_spins["quantity"].setValue(4)
        dlg._col_spins["unit_price"].setValue(5)
        dlg._col_spins["amount"].setValue(6)
        dlg.hdr_lo.setValue(2)
        dlg.hdr_hi.setValue(2)
        dlg.reason_edit.setPlainText("人工核对该表为结算清单")
        dlg.dir_combo.setCurrentIndex(0)  # upward
        n = dlg.conn.execute(
            "SELECT COUNT(*) c FROM line_items").fetchone()["c"]
        dlg._do_extract()
        n_after = dlg.conn.execute(
            "SELECT COUNT(*) c FROM line_items").fetchone()["c"]
        assert n_after > n, "按清单抽取必须写入明细"
        audit = dlg.conn.execute(
            """SELECT after_json FROM audit_log WHERE action='confirm_sheet_role'
               ORDER BY id DESC LIMIT 1""").fetchone()
        assert audit is not None, "抽取必须写审计"
        assert dlg.sheet_list.count() == 0, "抽取后该页不再待确认"
        conn.close()

    def test_evidence_only_records_role(self, qapp, quiet_boxes, gated_project):
        conn, pid = gated_project
        dlg = self._dialog(conn, pid)
        dlg._on_select(0)
        dlg.reason_edit.setPlainText("该表为人材机汇总，仅作证据")
        dlg.role_combo.setCurrentIndex(1)  # settlement_summary
        dlg._do_evidence_only()
        audit = dlg.conn.execute(
            """SELECT after_json FROM audit_log
               WHERE action='confirm_sheet_non_settlement_role'
               ORDER BY id DESC LIMIT 1""").fetchone()
        assert audit is not None, "仅存证必须写审计"
        assert "settlement_summary" in (audit["after_json"] or "")
        assert dlg.sheet_list.count() == 0
        conn.close()

    def test_save_mapping_template_is_explicit_candidate(self, qapp, quiet_boxes, gated_project):
        """人工映射可另存模板，但保存动作不直接写入当前 Sheet 映射。"""
        conn, pid = gated_project
        dlg = self._dialog(conn, pid)
        dlg._on_select(0)
        # 演示附表的人工核对范围与测试前置数据一致；不触发抽取。
        for field, col in {
            "name": 2, "unit": 3, "quantity": 4,
            "unit_price": 5, "amount": 6,
        }.items():
            dlg._col_spins[field].setValue(col)
        dlg.hdr_lo.setValue(2)
        dlg.hdr_hi.setValue(2)
        dlg.reason_edit.setPlainText("已核对附表列位，保存为本项目候选模板")
        dlg.template_name_edit.setText("附表人工映射")
        dlg.template_scope_combo.setCurrentIndex(1)  # 本项目
        before = conn.execute(
            "SELECT COUNT(*) FROM line_items WHERE sheet_id=?",
            (conn.execute("SELECT id FROM raw_sheets WHERE sheet_name='人材机汇总'").fetchone()[0],),
        ).fetchone()[0]
        dlg._save_template()
        row = conn.execute(
            "SELECT template_name, scope, created_by, evidence_id FROM mapping_templates "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert tuple(row[:3]) == ("附表人工映射", "project", "user")
        assert row["evidence_id"] is not None
        after = conn.execute(
            "SELECT COUNT(*) FROM line_items WHERE sheet_id=?",
            (conn.execute("SELECT id FROM raw_sheets WHERE sheet_name='人材机汇总'").fetchone()[0],),
        ).fetchone()[0]
        assert after == before
        conn.close()
