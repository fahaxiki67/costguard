"""框架/管理性协议费率规则对话框（宪章 §五）。

- 扫描已登记的框架/管理性协议 → 费率候选（每句每个百分比各一条）；
- 确认必须人工设定计取基数类型与说明（"按结算价3%"≠"按不含税价3%"）；
- 试算为 Decimal 确定性计算，支持上限/下限；结论写入审计。
"""
from __future__ import annotations

import logging
import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from jiadun.core.contracts import rate_rules
from jiadun.core.document_intake import list_documents

_LOG = logging.getLogger(__name__)

STATUS_ZH = {"candidate": "候选", "confirmed": "已确认", "rejected": "已拒绝"}
BASE_TYPE_ZH = {
    "unset": "（未设定）",
    "upward_settlement_amount": "按对上结算价",
    "upward_settlement_excl_tax": "按对上结算价（不含税）",
    "contract_amount": "按合同价款",
    "downward_settlement_amount": "按对下结算价",
    "custom": "自定义基数",
}
TAX_OPTIONS = [("unknown", "未确认"), ("included", "含税"), ("excluded", "不含税")]


class RateRulesDialog(QDialog):
    """框架协议费率：扫描 → 确认（结构化基数）→ 确定性试算。"""

    def __init__(self, conn: sqlite3.Connection, project_id: int, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.project_id = int(project_id)
        self._rules: list[dict] = []
        self.setWindowTitle("费率规则（框架/管理性协议）")
        self.resize(1000, 640)

        layout = QVBoxLayout(self)

        # 扫描区
        scan_row = QHBoxLayout()
        scan_row.addWidget(QLabel("框架/管理性协议："))
        self.doc_combo = QComboBox()
        self._load_framework_docs()
        scan_row.addWidget(self.doc_combo, 1)
        scan_btn = QPushButton("扫描费率候选")
        scan_btn.setObjectName("btnPrimary")
        scan_btn.clicked.connect(self._scan)
        scan_row.addWidget(scan_btn)
        layout.addLayout(scan_row)

        # 规则列表
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["ID", "比例", "状态", "基数类型", "基数说明", "原文引用（节选）"]
        )
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 2)

        # 确认表单（针对选中候选）
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setHorizontalSpacing(12)
        self.base_type_combo = QComboBox()
        for code, label in BASE_TYPE_ZH.items():
            self.base_type_combo.addItem(label, code)
        form.addRow("计取基数类型：", self.base_type_combo)
        self.base_def_edit = QLineEdit()
        self.base_def_edit.setPlaceholderText("例：按对上结算审定金额（含税）计取")
        form.addRow("基数说明（必填）：", self.base_def_edit)
        tax_row = QHBoxLayout()
        self.tax_combo = QComboBox()
        for code, label in TAX_OPTIONS:
            self.tax_combo.addItem(label, code)
        tax_row.addWidget(self.tax_combo)
        tax_row.addWidget(QLabel("上限（元，可空）"))
        self.cap_edit = QLineEdit()
        tax_row.addWidget(self.cap_edit)
        tax_row.addWidget(QLabel("下限（元，可空）"))
        self.floor_edit = QLineEdit()
        tax_row.addWidget(self.floor_edit)
        form.addRow("税口径 / 上限 / 下限：", tax_row)
        review_row = QHBoxLayout()
        review_row.addStretch(1)
        self.confirm_btn = QPushButton("确认为费率规则")
        self.confirm_btn.setObjectName("btnPrimary")
        self.confirm_btn.clicked.connect(self._confirm)
        self.reject_btn = QPushButton("拒绝候选")
        self.reject_btn.setObjectName("btnTertiary")
        self.reject_btn.clicked.connect(self._reject)
        for b in (self.reject_btn, self.confirm_btn):
            review_row.addWidget(b)
        form.addRow(review_row)
        layout.addWidget(form_w)

        # 试算区
        calc_row = QHBoxLayout()
        calc_row.addWidget(QLabel("确定性试算：基数（元）"))
        self.calc_edit = QLineEdit()
        calc_row.addWidget(self.calc_edit, 1)
        self.calc_btn = QPushButton("试算")
        self.calc_btn.clicked.connect(self._apply)
        calc_row.addWidget(self.calc_btn)
        self.calc_label = QLabel("")
        calc_row.addWidget(self.calc_label, 1)
        layout.addLayout(calc_row)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("btnTertiary")
        close_btn.clicked.connect(self.reject)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

        self.table.itemSelectionChanged.connect(self._fill_form_from_selection)
        self._reload()

    # ---- 数据 ----

    def _load_framework_docs(self) -> None:
        self.doc_combo.clear()
        docs = [
            d for d in list_documents(self.conn, self.project_id)
            if d["category"] == "upward_framework_management"
        ]
        if not docs:
            self.doc_combo.addItem("（暂无框架/管理性协议；请在资料中心分类后导入）", None)
            return
        for d in docs:
            self.doc_combo.addItem(
                f"{d['original_name']}（{STATUS_ZH.get(d['parse_status'], d['parse_status'])}）",
                int(d["file_id"]),
            )

    def _reload(self) -> None:
        self._rules = rate_rules.list_rate_rules(self.conn, self.project_id)
        self.table.setRowCount(len(self._rules))
        for r, rule in enumerate(self._rules):
            values = [
                str(rule["id"]),
                f"{rule['rate_percent']}%" if rule["rate_percent"] else "—",
                STATUS_ZH.get(str(rule["status"]), str(rule["status"])),
                BASE_TYPE_ZH.get(str(rule["base_type"]), str(rule["base_type"])),
                rule["base_definition"],
                rule["quote_text"][:80],
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                if c == 2 and rule["status"] == "confirmed":
                    item.setForeground(Qt.GlobalColor.darkGreen)
                self.table.setItem(r, c, item)

    def _selected_rule(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._rules):
            QMessageBox.information(self, "未选择", "请先在表格中选择一条费率候选/规则。")
            return None
        return self._rules[row]

    def _fill_form_from_selection(self) -> None:
        rule = self._selected_rule_quiet()
        if rule is None:
            return
        index = self.base_type_combo.findData(str(rule["base_type"] or "unset"))
        self.base_type_combo.setCurrentIndex(index if index >= 0 else 0)

    def _selected_rule_quiet(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._rules):
            return None
        return self._rules[row]

    # ---- 操作 ----

    def _scan(self) -> None:
        file_id = self.doc_combo.currentData()
        if file_id is None:
            QMessageBox.information(self, "无法扫描", "暂无框架/管理性协议资料。")
            return
        try:
            n = rate_rules.import_rate_candidates(self.conn, self.project_id, int(file_id))
        except ValueError as exc:
            QMessageBox.warning(self, "无法扫描", str(exc))
            return
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("费率扫描失败")
            QMessageBox.critical(self, "扫描失败", "扫描未能完成，请重试。")
            return
        QMessageBox.information(self, "扫描完成", f"登记 {n} 条费率候选；请逐条确认基数后使用。")
        self._reload()

    def _confirm(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        try:
            rate_rules.confirm_rate_rule(
                self.conn, self.project_id, int(rule["id"]),
                base_type=str(self.base_type_combo.currentData()),
                base_definition=self.base_def_edit.text().strip(),
                tax_basis=str(self.tax_combo.currentData()),
                cap=self.cap_edit.text().strip() or None,
                floor=self.floor_edit.text().strip() or None,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "无法确认", str(exc))
            return
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("费率规则确认失败")
            QMessageBox.critical(self, "写入失败", "确认信息未能写入数据库，请重试。")
            return
        self._reload()

    def _reject(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        reason = self.base_def_edit.text().strip()
        if not reason:
            QMessageBox.warning(self, "需要理由", "拒绝候选必须说明理由（可填写在基数说明框）。")
            return
        try:
            rate_rules.reject_rate_rule(self.conn, self.project_id, int(rule["id"]), reason=reason)
        except ValueError as exc:
            QMessageBox.warning(self, "无法拒绝", str(exc))
            return
        self._reload()

    def _apply(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        amount = self.calc_edit.text().strip()
        if not amount:
            QMessageBox.information(self, "缺少基数", "请先填写试算基数金额。")
            return
        try:
            result = rate_rules.apply_rate_rule(
                self.conn, self.project_id, int(rule["id"]), amount
            )
        except ValueError as exc:
            QMessageBox.warning(self, "无法试算", str(exc))
            return
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("费率试算失败")
            QMessageBox.critical(self, "试算失败", "试算未能完成，请重试。")
            return
        extra = ""
        if result["detail"].get("cap_applied"):
            extra += f"（已按上限 {result['detail']['cap_applied']} 封顶）"
        if result["detail"].get("floor_applied"):
            extra += f"（已按下限 {result['detail']['floor_applied']} 兜底）"
        self.calc_label.setText(f"费用 = {result['fee']} 元{extra}")
