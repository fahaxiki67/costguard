"""全工作簿 Sheet 清单浏览器（任务书任务 B1/B2/B3/B4 的界面侧）。

规则：
- 列出项目全部 Sheet（不只待确认门控页），让用户在几十个 Sheet 里定位
  该用哪张（用户反馈#2/#5）；
- 机器建议（角色/置信度/理由）只是候选；人工标注通过 set_sheet_list_kind
  写入（理由必填，写审计 Evidence），机器建议永不覆盖人工标注；
- 可见状态未知（历史批次）如实显示「未知」，不得默认可见。
"""
from __future__ import annotations

import logging
import sqlite3

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from jiadun.core.engine import sheet_inventory

_LOG = logging.getLogger(__name__)

STATUS_ZH = {
    "pending": "待确认",
    "confirmed": "已确认",
    "non_business": "非业务",
    "parse_failed": "解析失败",
}
VISIBLE_ZH = {
    "visible": "可见",
    "hidden": "隐藏",
    "veryHidden": "深度隐藏",
}
KIND_ZH = {
    sheet_inventory.LIST_KIND_UNKNOWN: "暂不确定",
    sheet_inventory.LIST_KIND_BOQ: "分部分项清单",
    sheet_inventory.LIST_KIND_MEASURE_UNIT: "单价措施（量×价）",
    sheet_inventory.LIST_KIND_MEASURE_TOTAL: "总价措施（费率计取）",
    sheet_inventory.LIST_KIND_UPSTREAM_DETAIL: "对上明细",
    sheet_inventory.LIST_KIND_DOWNSTREAM_DETAIL: "对下明细",
    sheet_inventory.LIST_KIND_OTHER_FEE: "其他费用",
    sheet_inventory.LIST_KIND_SUMMARY: "汇总/控制页",
    sheet_inventory.LIST_KIND_NON_BUSINESS: "非业务",
}
_MODE_ZH = [
    (sheet_inventory.FILTER_ALL, "全部"),
    (sheet_inventory.FILTER_PENDING, "仅待确认"),
    (sheet_inventory.FILTER_SUGGESTED, "仅建议参与分析"),
]


def _kind_label(kind: str | None) -> str:
    return KIND_ZH.get(str(kind or ""), str(kind or ""))


class SheetBrowserDialog(QDialog):
    """全部 Sheet 一览：过滤、建议与人工标注。"""

    def __init__(self, conn: sqlite3.Connection, project_id: int, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.project_id = int(project_id)
        self._rows: list[dict] = []
        self.setWindowTitle("全工作簿 Sheet 清单（选择该用哪张表）")
        self.resize(1120, 720)

        layout = QVBoxLayout(self)

        # ---- 过滤行 ----
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("关键字："))
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("按 Sheet 名过滤（如 分部分项 / 措施）")
        self.keyword_edit.returnPressed.connect(self._reload)
        filter_row.addWidget(self.keyword_edit, 1)
        filter_row.addWidget(QLabel("显示："))
        self.mode_combo = QComboBox()
        for mode, label in _MODE_ZH:
            self.mode_combo.addItem(label, mode)
        self.mode_combo.currentIndexChanged.connect(self._reload)
        filter_row.addWidget(self.mode_combo)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._reload)
        filter_row.addWidget(refresh_btn)
        layout.addLayout(filter_row)

        # ---- 清单表 ----
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["文件", "Sheet 名", "可见", "行×列", "状态",
             "建议角色", "置信度", "建议理由", "人工标注"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(7, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        self.table.setColumnWidth(1, 180)
        self.table.setColumnWidth(0, 150)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

        # ---- 标注区 ----
        note_row = QHBoxLayout()
        note_row.addWidget(QLabel("清单类型："))
        self.kind_combo = QComboBox()
        for kind in sheet_inventory.LIST_KINDS:
            self.kind_combo.addItem(_kind_label(kind), kind)
        note_row.addWidget(self.kind_combo)
        note_row.addWidget(QLabel("标注理由（必填，写入审计）："))
        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText(
            "例如：表头含 编码/名称/工程量/单价/合价，判定为分部分项清单"
        )
        note_row.addWidget(self.reason_edit, 1)
        self.annotate_btn = QPushButton("保存标注")
        self.annotate_btn.setObjectName("btnPrimary")
        self.annotate_btn.clicked.connect(self._annotate)
        note_row.addWidget(self.annotate_btn)
        layout.addLayout(note_row)

        hint = QLabel(
            "说明：机器建议只是候选；人工标注后重解析也不会被机器改写"
            "（除非 Sheet 内容变化）。角色变更会使既有运行结果失效并要求重新校核。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._reload()

    # ---- 数据 ----

    def _reload(self) -> None:
        keyword = self.keyword_edit.text().strip() or None
        mode = self.mode_combo.currentData() or sheet_inventory.FILTER_ALL
        try:
            self._rows = sheet_inventory.list_workbook_sheets(
                self.conn, self.project_id, keyword=keyword, filter_mode=mode
            )
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("读取 Sheet 清单失败")
            self._rows = []
        self.table.setRowCount(len(self._rows))
        for r, item in enumerate(self._rows):
            grid = f"{item['n_rows']}×{item['n_cols']}"
            visible = VISIBLE_ZH.get(item["visible_state"] or "", "未知")
            status = STATUS_ZH.get(str(item["sheet_status"] or ""), str(item["sheet_status"] or ""))
            values = [
                str(item["original_name"] or ""),
                str(item["sheet_name"]),
                visible,
                grid,
                status,
                _kind_label(item["suggested_kind"]),
                str(item["suggest_confidence"] or ""),
                str(item["suggest_reason"] or ""),
                _kind_label(item["list_kind"]) if item["list_kind"] else "",
            ]
            for c, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if c == 2 and visible == "未知":
                    cell.setToolTip("历史批次未捕获可见状态；请重新导入以获得")
                self.table.setItem(r, c, cell)

    # ---- 操作 ----

    def _selected_sheet(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._rows):
            QMessageBox.information(self, "未选择", "请先在清单中选择一个 Sheet。")
            return None
        return self._rows[row]

    def _annotate(self) -> None:
        sheet = self._selected_sheet()
        if sheet is None:
            return
        kind = self.kind_combo.currentData()
        reason = self.reason_edit.text().strip()
        try:
            sheet_inventory.set_sheet_list_kind(
                self.conn, self.project_id, int(sheet["sheet_id"]), kind, reason=reason
            )
        except ValueError as exc:
            QMessageBox.warning(self, "无法标注", str(exc))
            return
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("写入 Sheet 标注失败")
            QMessageBox.critical(self, "写入失败", "标注未能写入数据库，请重试。")
            return
        self.reason_edit.clear()
        self._reload()
