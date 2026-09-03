"""对上控制基准候选对话框（宪章 §六）。

规则：
- 候选只能来自已确认合同事实或人工显式登记（出处必填）；
- 只有经人工确认（附核对依据）的基准才参与上限比较；
- 不自动挑选基准；两个并存有效基准比较时返回 CONTROL_CONFLICT；
- 比较结论只报告差额，不认定违规或责任。
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
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from jiadun.core.engine import control_baseline as cb
from jiadun.core.engine.money import to_decimal

_LOG = logging.getLogger(__name__)

STATUS_ZH = {
    "candidate": "候选",
    "confirmed": "已确认",
    "rejected": "已拒绝",
}
RESULT_ZH = {
    "PASS": "未超上限（PASS）",
    "FAIL": "超上限（FAIL）",
    "PENDING": "待确认（PENDING）",
    "INCOMPARABLE": "不可比较（INCOMPARABLE）",
    "CONTROL_CONFLICT": "基准冲突（CONTROL_CONFLICT）",
}
TAX_OPTIONS = [("unknown", "未确认"), ("included", "含税"), ("excluded", "不含税")]


def _tax_label(code: str) -> str:
    return dict(TAX_OPTIONS).get(code, code)


class ControlBaselineDialog(QDialog):
    """对上控制基准候选管理 + 与对上结算期次的五态上限比较。"""

    def __init__(self, conn: sqlite3.Connection, project_id: int, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.project_id = int(project_id)
        self._baseline_rows: list[dict] = []
        self.setWindowTitle("对上控制基准候选（终审/审计报告 = 上限候选）")
        self.resize(1000, 640)

        layout = QVBoxLayout(self)

        # ---- 基准列表 ----
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["ID", "金额（元）", "税口径", "状态", "来源", "取代", "确认依据"]
        )
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 2)

        # ---- 登记新候选 ----
        form_w = QWidget()
        form = QFormLayout(form_w)
        form.setHorizontalSpacing(12)
        self.fact_combo = QComboBox()
        self._load_confirmed_facts()
        form.addRow("从已确认条款登记：", self.fact_combo)
        self.supersedes_combo = QComboBox()
        form.addRow("声明取代现有候选：", self.supersedes_combo)
        self.tax_combo = QComboBox()
        for code, label in TAX_OPTIONS:
            self.tax_combo.addItem(label, code)
        form.addRow("税口径：", self.tax_combo)
        self.scope_edit = QLineEdit()
        self.scope_edit.setPlaceholderText("范围说明（可空；双方都填写且不同 → INCOMPARABLE）")
        form.addRow("范围说明：", self.scope_edit)
        add_row = QHBoxLayout()
        add_btn = QPushButton("登记为候选")
        add_btn.clicked.connect(self._add_from_fact)
        add_row.addWidget(add_btn)
        add_row.addStretch(1)
        form.addRow(add_row)
        layout.addWidget(form_w)

        # ---- 确认/拒绝 ----
        review_row = QHBoxLayout()
        review_row.addWidget(QLabel("核对依据（确认必填）"))
        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("范围/税口径/版本的核对结论，写入审计")
        review_row.addWidget(self.reason_edit, 1)
        self.confirm_btn = QPushButton("确认选中基准")
        self.confirm_btn.setObjectName("btnPrimary")
        self.confirm_btn.clicked.connect(lambda: self._review("confirmed"))
        self.reject_btn = QPushButton("拒绝选中基准")
        self.reject_btn.clicked.connect(lambda: self._review("rejected"))
        for b in (self.reject_btn, self.confirm_btn):
            review_row.addWidget(b)
        layout.addLayout(review_row)

        # ---- 比较 ----
        split = QSplitter(self)
        compare_w = QWidget()
        cv = QVBoxLayout(compare_w)
        period_row = QHBoxLayout()
        period_row.addWidget(QLabel("对上结算期次："))
        self.period_combo = QComboBox()
        self.period_combo.currentIndexChanged.connect(self._show_period_total)
        period_row.addWidget(self.period_combo, 1)
        self.period_total_label = QLabel("明细合计：—")
        period_row.addWidget(self.period_total_label)
        cv.addLayout(period_row)
        self.compare_btn = QPushButton("与选中基准比较")
        self.compare_btn.clicked.connect(self._compare)
        cv.addWidget(self.compare_btn, 0)
        self.result_view = QTextEdit()
        self.result_view.setReadOnly(True)
        self.result_view.setPlaceholderText("比较结论（PASS/FAIL/PENDING/INCOMPARABLE/CONTROL_CONFLICT）")
        cv.addWidget(self.result_view, 1)
        split.addWidget(compare_w)
        layout.addWidget(split, 2)

        self._reload()

    # ---- 数据 ----

    def _load_confirmed_facts(self) -> None:
        from jiadun.core.contracts import extract

        self.fact_combo.clear()
        count = 0
        try:
            facts = extract.list_contract_facts(self.conn, self.project_id, review_status="confirmed")
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("读取已确认条款失败")
            facts = []
        for fact in facts:
            try:
                to_decimal(fact.get("fact_value"))
            except Exception:  # noqa: BLE001 — 非金额条款不进入候选来源
                continue
            label = f"《{fact.get('doc_title')}》{fact.get('fact_key')} = {fact.get('fact_value')}"
            self.fact_combo.addItem(label, int(fact["id"]))
            count += 1
        if count == 0:
            self.fact_combo.addItem("（暂无已确认金额条款；请先在条款确认中确认）", None)

    def _reload(self) -> None:
        self._baseline_rows = cb.list_baselines(self.conn, self.project_id)
        self.table.setRowCount(len(self._baseline_rows))
        superseded_ids = set()
        for b in self._baseline_rows:
            if b.get("supersedes_id"):
                superseded_ids.add(int(b["supersedes_id"]))
        for r, b in enumerate(self._baseline_rows):
            values = [
                str(b["id"]),
                b["amount"],
                _tax_label(str(b["tax_basis"] or "unknown")),
                STATUS_ZH.get(str(b["status"]), str(b["status"])),
                b.get("source_note") or b.get("doc_title") or "",
                f"#{b['supersedes_id']}" if b.get("supersedes_id") else "",
                b.get("confirmed_reason") or "",
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(str(text))
                if c == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(r, c, item)
        self._load_supersedes_options()
        self._load_periods()

    def _load_supersedes_options(self) -> None:
        self.supersedes_combo.clear()
        self.supersedes_combo.addItem("（无）", None)
        for b in self._baseline_rows:
            if b["status"] != "rejected":
                self.supersedes_combo.addItem(f"#{b['id']}（{b['amount']} 元）", int(b["id"]))

    def _load_periods(self) -> None:
        self.period_combo.clear()
        try:
            self._periods = cb.list_upward_periods(self.conn, self.project_id)
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("读取对上期次失败")
            self._periods = []
        if not self._periods:
            self.period_combo.addItem("（暂无对上结算期次）", None)
            self.period_total_label.setText("明细合计：—")
            return
        for p in self._periods:
            self.period_combo.addItem(
                f"第 {p['period_no']} 期 · {p['title']}（{p['detail_rows']} 行明细）",
                p["period_id"],
            )
        self._show_period_total()

    def _show_period_total(self) -> None:
        period_id = self.period_combo.currentData()
        for p in getattr(self, "_periods", []):
            if p["period_id"] == period_id:
                self.period_total_label.setText(f"明细合计：{p['amount_total']} 元")
                return
        self.period_total_label.setText("明细合计：—")

    def _selected_baseline(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._baseline_rows):
            QMessageBox.information(self, "未选择", "请先在表格中选择一条控制基准。")
            return None
        return self._baseline_rows[row]

    # ---- 操作 ----

    def _add_from_fact(self) -> None:
        fact_id = self.fact_combo.currentData()
        if fact_id is None:
            QMessageBox.information(self, "无法登记", "没有可用的已确认金额条款。")
            return
        supersedes = self.supersedes_combo.currentData()
        try:
            cb.create_candidate_from_fact(
                self.conn, self.project_id, int(fact_id),
                tax_basis=self.tax_combo.currentData(),
                scope_note=self.scope_edit.text().strip(),
                supersedes_id=int(supersedes) if supersedes else None,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "无法登记", str(exc))
            return
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("登记控制基准候选失败")
            QMessageBox.critical(self, "写入失败", "候选未能写入数据库，请重试。")
            return
        self._reload()

    def _review(self, decision: str) -> None:
        baseline = self._selected_baseline()
        if baseline is None:
            return
        reason = self.reason_edit.text().strip()
        try:
            cb.set_baseline_review(
                self.conn, self.project_id, int(baseline["id"]), decision, reason=reason
            )
        except ValueError as exc:
            QMessageBox.warning(self, "无法完成", str(exc))
            return
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("写入基准确认失败")
            QMessageBox.critical(self, "写入失败", "确认信息未能写入数据库，请重试。")
            return
        self.reason_edit.clear()
        self._reload()

    def _compare(self) -> None:
        baseline = self._selected_baseline()
        if baseline is None:
            return
        period_id = self.period_combo.currentData()
        period = next(
            (p for p in getattr(self, "_periods", []) if p["period_id"] == period_id), None
        )
        if period is None:
            QMessageBox.information(self, "无法比较", "请先导入对上结算期次资料。")
            return
        try:
            result = cb.compare_upward_result(
                self.conn, self.project_id, int(baseline["id"]),
                period["amount_total"],
                settlement_tax_basis=str(period.get("tax_mode") or "unknown"),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "无法比较", str(exc))
            return
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("控制基准比较失败")
            QMessageBox.critical(self, "比较失败", "比较未能完成，请重试。")
            return
        lines = [
            f"结论：{RESULT_ZH.get(result['status'], result['status'])}",
            f"基准 #{result['baseline_id']}：{result['baseline_amount']} 元",
            f"对上结算合计（第 {period['period_no']} 期）：{result['settlement_amount']} 元",
            f"差额：{result['delta'] if result['delta'] is not None else '—'} 元",
            f"说明：{result['reason']}",
            "（比较结论不构成违规或责任认定；全部依据已写入审计 Evidence）",
        ]
        self.result_view.setPlainText("\n".join(lines))
