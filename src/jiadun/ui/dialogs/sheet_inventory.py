"""全工作簿 Sheet 清单对话框（用户反馈#2：在几十个 Sheet 中定位该用哪张）。

规则：
- 只读展示每个文件最新批次的**全部** Sheet，不只待确认门控页；
- suggest_list_kind 给出的清单类型建议只是候选，不改变 sheet_status 门控语义；
- 人工标注清单类型理由必填（原则 14），经 set_sheet_list_kind 写审计 Evidence。
"""

from __future__ import annotations

import logging
import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from jiadun.core.engine import sheet_inventory as inv
from jiadun.core.reporting.state import SHEET_LABELS

_LOG = logging.getLogger(__name__)

KIND_ZH = {
    inv.LIST_KIND_UNKNOWN: "未知",
    inv.LIST_KIND_BOQ: "分部分项清单",
    inv.LIST_KIND_MEASURE_UNIT: "单价措施",
    inv.LIST_KIND_MEASURE_TOTAL: "总价措施",
    inv.LIST_KIND_SUMMARY: "汇总/台账",
    inv.LIST_KIND_NON_BUSINESS: "非业务表",
}


def _kind_label(code: str) -> str:
    return KIND_ZH.get(code, code)


def _status_label(item: dict) -> str:
    """区分角色已确认但结构/范围仍待复核，避免状态语义混淆。"""
    status = str(item["sheet_status"])
    reason = str(item["sheet_status_reason"] or "")
    if status == "pending" and "人工确认" in reason and "抽取" in reason:
        return "已确认抽取，待结构/范围复核"
    return SHEET_LABELS.get(status, status)


class KindSelectDialog(QDialog):
    """标注清单类型：类型 + 必填理由（写入审计 Evidence）。"""

    def __init__(self, sheet_name: str, current_kind: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"标注清单类型：{sheet_name}")
        self.resize(460, 240)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("清单类型只是内容标注，不改变该页的确认门控状态；建议仅为候选，以人工判断为准。"))
        self.kind_combo = QComboBox()
        for code, label in KIND_ZH.items():
            self.kind_combo.addItem(label, code)
        self.kind_combo.setCurrentIndex(
            list(KIND_ZH).index(current_kind if current_kind in KIND_ZH else inv.LIST_KIND_UNKNOWN)
        )
        v.addWidget(self.kind_combo)
        v.addWidget(QLabel("标注理由（必填，写入审计）："))
        self.reason_edit = QTextEdit()
        self.reason_edit.setPlaceholderText("例如：表头含“分部分项工程量清单计价表”，逐列核对后标注")
        v.addWidget(self.reason_edit, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def annotation(self) -> tuple[str, str]:
        return (
            self.kind_combo.currentData(),
            self.reason_edit.toPlainText().strip(),
        )

    def accept(self) -> None:  # noqa: D102
        if not self.reason_edit.toPlainText().strip():
            QMessageBox.warning(self, "标注清单类型", "标注理由必填（写入审计）。")
            return
        super().accept()


class SheetInventoryDialog(QDialog):
    """全工作簿 Sheet 清单：全部 Sheet 定位 + 清单类型人工标注。"""

    def __init__(self, conn: sqlite3.Connection, project_id: int, parent=None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.project_id = int(project_id)
        self.setWindowTitle("全工作簿 Sheet 清单")
        self.resize(1000, 560)
        v = QVBoxLayout(self)

        filter_row = QHBoxLayout()
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("按 Sheet 名称过滤…")
        self.keyword_edit.setClearButtonEnabled(True)
        self.keyword_edit.textChanged.connect(self.reload)
        self.status_combo = QComboBox()
        self.status_combo.addItem("全部状态", None)
        for code, label in SHEET_LABELS.items():
            self.status_combo.addItem(label, code)
        self.status_combo.currentIndexChanged.connect(self.reload)
        filter_row.addWidget(QLabel("过滤："))
        filter_row.addWidget(self.keyword_edit, 1)
        filter_row.addWidget(self.status_combo)
        v.addLayout(filter_row)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["文件", "工作表", "状态", "行×列", "建议类型", "建议依据", "当前清单类型"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.table, 1)

        self.summary_label = QLabel("")
        v.addWidget(self.summary_label)

        btn_row = QHBoxLayout()
        annotate_btn = QPushButton("标注清单类型…")
        annotate_btn.clicked.connect(self._annotate_selected)
        btn_row.addWidget(annotate_btn)
        btn_row.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        v.addLayout(btn_row)

        self.reload()

    def reload(self) -> None:
        keyword = self.keyword_edit.text().strip() or None
        status = self.status_combo.currentData()
        rows = inv.list_workbook_sheets(self.conn, self.project_id, status=status, keyword=keyword)
        self.table.setRowCount(len(rows))
        for r, item in enumerate(rows):
            values = [
                item["original_name"],
                item["sheet_name"],
                _status_label(item),
                f"{item['n_rows']}×{item['n_cols']}",
                _kind_label(str(item["suggested_kind"])),
                item["suggest_reason"],
                _kind_label(str(item["list_kind"] or inv.LIST_KIND_UNKNOWN)),
            ]
            for c, text in enumerate(values):
                self.table.setItem(r, c, QTableWidgetItem(str(text)))
            payload = self.table.item(r, 0)
            payload.setData(Qt.ItemDataRole.UserRole, dict(item))
            # 状态可能仍有缺口（如确认抽取后存在结构性风险），原因必须可见
            status_reason = str(item["sheet_status_reason"] or "").strip()
            if status_reason:
                self.table.item(r, 2).setToolTip(status_reason)
        self.summary_label.setText(
            f"共 {len(rows)} 个工作表（每文件最新批次）；建议类型仅为候选，以人工标注为准。"
        )

    def _selected_item(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        cell = self.table.item(row, 0)
        data = cell.data(Qt.ItemDataRole.UserRole) if cell else None
        return dict(data) if data else None

    def _annotate_selected(self) -> None:
        item = self._selected_item()
        if item is None:
            QMessageBox.information(self, "标注清单类型", "请先在清单中选择一个工作表。")
            return
        dlg = KindSelectDialog(str(item["sheet_name"]), str(item["list_kind"] or inv.LIST_KIND_UNKNOWN), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        kind, reason = dlg.annotation()
        try:
            inv.set_sheet_list_kind(self.conn, self.project_id, int(item["sheet_id"]), kind, reason=reason)
        except ValueError as exc:
            QMessageBox.warning(self, "标注清单类型", str(exc))
            _LOG.warning("标注清单类型被拒绝：sheet_id=%s：%s", item.get("sheet_id"), exc)
            return
        self.reload()
