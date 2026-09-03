"""人工确认合同条款（ContractReviewDialog）。

业务规则（阶段 C-1）：
- 抽取产出一律是候选（candidate）；只有人工确认的事实才是已确认合同事实；
- 推翻已确认/已拒绝结论必须填写理由（审计要求）；
- 每次确认/拒绝都写入 contract_fact_review Evidence，前后值与操作者可追溯；
- 被拒绝的事实不再进入运行契约；候选事实在载荷中带状态标记。
"""
from __future__ import annotations

import logging
import sqlite3

from PySide6.QtCore import Qt
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

from jiadun.core.contracts import extract

_LOG = logging.getLogger(__name__)

STATUS_ZH = {
    "candidate": "候选",
    "confirmed": "已确认",
    "rejected": "已拒绝",
    "needs_review": "待复核",
}

FACT_KEY_ZH = {
    "employer_party": "发包人",
    "contractor_party": "承包人",
    "contract_amount": "合同价款",
    "tax_clause": "税务条款",
    "pricing_method": "计价方式",
    "payment_clause": "付款条款",
    "settlement_clause": "结算条款",
    "duration": "工期",
    "breach_clause": "违约责任",
    "claim_clause": "索赔条款",
    "prepayment": "预付款",
    "retention": "质保金",
}


def _fact_key_zh(key: str) -> str:
    return FACT_KEY_ZH.get(key, key)


class ContractReviewDialog(QDialog):
    """按状态列出合同条款，人工确认/拒绝（理由必填于推翻时）。"""

    def __init__(self, conn: sqlite3.Connection, project_id: int, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.project_id = project_id
        self._rows: list[dict] = []
        self.setWindowTitle("人工确认合同条款（候选不作为已确认事实）")
        self.resize(960, 560)

        layout = QVBoxLayout(self)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("状态筛选"))
        self.status_filter = QComboBox()
        for status in ("candidate", "needs_review", "confirmed", "rejected", "all"):
            self.status_filter.addItem(STATUS_ZH.get(status, "全部") if status != "all" else "全部", status)
        self.status_filter.setCurrentIndex(0)
        self.status_filter.currentIndexChanged.connect(self._reload)
        filter_row.addWidget(self.status_filter)
        filter_row.addStretch(1)
        layout.addLayout(filter_row)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["文档", "条款", "内容", "原文引用（节选）", "状态", "确认信息"]
        )
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

        reason_row = QHBoxLayout()
        reason_row.addWidget(QLabel("理由（推翻已确认/已拒绝时必填）"))
        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("确认/拒绝依据，写入审计日志")
        reason_row.addWidget(self.reason_edit, 1)
        layout.addLayout(reason_row)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.confirm_btn = QPushButton("确认选中条款")
        self.confirm_btn.setObjectName("btnPrimary")
        self.confirm_btn.clicked.connect(lambda: self._apply("confirmed"))
        self.reject_btn = QPushButton("拒绝选中条款")
        self.reject_btn.clicked.connect(lambda: self._apply("rejected"))
        self.needs_review_btn = QPushButton("标记待复核")
        self.needs_review_btn.setObjectName("btnTertiary")
        self.needs_review_btn.clicked.connect(lambda: self._apply("needs_review"))
        self.close_btn = QPushButton("关闭")
        self.close_btn.setObjectName("btnTertiary")
        self.close_btn.clicked.connect(self.reject)
        for b in (self.close_btn, self.needs_review_btn, self.reject_btn, self.confirm_btn):
            actions.addWidget(b)
        layout.addLayout(actions)

        self._reload()

    # ---- 数据 ----

    def _current_status_filter(self) -> str | None:
        status = self.status_filter.currentData()
        return None if status == "all" else status

    def _reload(self) -> None:
        try:
            self._rows = extract.list_contract_facts(
                self.conn, self.project_id,
                review_status=self._current_status_filter(),
            )
        except Exception:  # noqa: BLE001 — UI 层兜底：读取失败不清空界面后崩
            _LOG.exception("读取合同事实失败")
            self._rows = []
        self.table.setRowCount(len(self._rows))
        for r, fact in enumerate(self._rows):
            values = [
                fact.get("doc_title") or "",
                _fact_key_zh(fact.get("fact_key") or ""),
                fact.get("fact_value") or "（关键词命中，无具体值）",
                fact.get("quote_text") or "",
                STATUS_ZH.get(fact.get("review_status") or "candidate", fact.get("review_status")),
                self._review_info(fact),
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                if c == 4 and (fact.get("review_status") or "candidate") == "confirmed":
                    item.setForeground(Qt.GlobalColor.darkGreen)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(r, c, item)

    @staticmethod
    def _review_info(fact: dict) -> str:
        if not fact.get("reviewed_at"):
            return ""
        who = fact.get("reviewed_by") or "user"
        reason = fact.get("review_reason") or ""
        info = f"{fact['reviewed_at']} · {who}"
        if reason:
            info += f" · {reason}"
        return info

    def _selected_fact(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._rows):
            return None
        return self._rows[row]

    # ---- 操作 ----

    def _apply(self, decision: str) -> None:
        fact = self._selected_fact()
        if fact is None:
            QMessageBox.information(self, "未选择", "请先在表格中选择一条条款。")
            return
        reason = self.reason_edit.text().strip()
        current = fact.get("review_status") or "candidate"
        if current in ("confirmed", "rejected") and not reason:
            QMessageBox.warning(
                self, "需要理由",
                f"该条款当前状态为「{STATUS_ZH.get(current, current)}」，"
                "推翻既有结论必须填写理由。",
            )
            return
        try:
            extract.set_fact_review(
                self.conn, self.project_id, int(fact["id"]), decision,
                reviewed_by="user", reason=reason,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "无法完成确认", str(exc))
            return
        except Exception:  # noqa: BLE001 — UI 层兜底：数据库错误必须可见，不能静默
            _LOG.exception("写入合同条款确认失败")
            QMessageBox.critical(self, "写入失败", "确认信息未能写入数据库，请重试。")
            return
        self.reason_edit.clear()
        self._reload()
