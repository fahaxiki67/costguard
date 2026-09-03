"""PDF 逐页人工对照复核对话框（阶段 C-2）。

业务规则：
- 只读原件保持只读；用户对照原件逐页核实 OCR 结果；
- 核实一页必须填写对照依据（写入审计）；
- 全部应复核页 verified 后才允许把文档从 needs_review 转为 parsed；
- 条款仍按条款级确认（candidate → confirmed），页级复核只解除文档门控。
"""
from __future__ import annotations

import logging
import sqlite3

from PySide6.QtWidgets import (
    QDialog,
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
)

from jiadun.core.contracts import page_review

_LOG = logging.getLogger(__name__)

STATUS_ZH = {
    "native_text": "原生文本",
    "ocr": "OCR",
    "pending_ocr": "OCR 待处理",
    "ocr_failed": "OCR 失败",
    "needs_review": "待复核",
}
DECISION_ZH = {
    "verified": "已核实",
    "needs_review": "待复核",
}


class PageReviewDialog(QDialog):
    """逐页对照复核：选页 → 对照只读原件 → 核实/退回 → 全部核实后完成。"""

    def __init__(self, conn: sqlite3.Connection, project_id: int, file_id: int,
                 original_name: str, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.project_id = int(project_id)
        self.file_id = int(file_id)
        self._rows: list[dict] = []
        self.setWindowTitle(f"逐页对照复核 — {original_name}")
        self.resize(920, 600)

        layout = QVBoxLayout(self)
        hint = QLabel(
            "请打开只读原件，与该页 OCR 结果逐行对照；核实一页必须填写对照依据。\n"
            "全部应复核页核实后，方可解除文档门控（条款仍需逐条人工确认）。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        split = QSplitter(self)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["页码", "状态", "提取方式", "复核状态", "复核人 / 时间 / 依据"])
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        split.addWidget(self.table)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("选择一页后显示该页提取的候选条款（供与原件对照）")
        split.addWidget(self.detail)
        split.setSizes([420, 480])
        layout.addWidget(split, 1)

        reason_row = QHBoxLayout()
        reason_row.addWidget(QLabel("对照依据（核实必填）"))
        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("例如：与原件第 2 页逐行核对一致；OCR 数字已复核")
        reason_row.addWidget(self.reason_edit, 1)
        layout.addLayout(reason_row)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.verify_btn = QPushButton("核实此页")
        self.verify_btn.setObjectName("btnPrimary")
        self.verify_btn.clicked.connect(self._verify_page)
        self.return_btn = QPushButton("退回待复核")
        self.return_btn.setObjectName("btnTertiary")
        self.return_btn.clicked.connect(self._return_page)
        self.finish_btn = QPushButton("完成复核（解除文档门控）")
        self.finish_btn.clicked.connect(self._finish)
        self.close_btn = QPushButton("关闭")
        self.close_btn.setObjectName("btnTertiary")
        self.close_btn.clicked.connect(self.reject)
        for b in (self.close_btn, self.return_btn, self.finish_btn, self.verify_btn):
            actions.addWidget(b)
        layout.addLayout(actions)

        self.table.itemSelectionChanged.connect(self._show_page_detail)
        self._reload()

    # ---- 数据 ----

    def _reload(self) -> None:
        try:
            info = page_review.list_pdf_pages(self.conn, self.project_id, self.file_id)
        except ValueError as exc:
            QMessageBox.warning(self, "无法复核", str(exc))
            self.reject()
            return
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("读取页级复核状态失败")
            self._rows = []
            return
        self._rows = info["pages"]
        self.table.setRowCount(len(self._rows))
        for r, page in enumerate(self._rows):
            decision = page.get("decision")
            review_text = DECISION_ZH.get(decision or "", "未复核")
            if page.get("reviewed_at"):
                review_text += f" · {page['reviewed_at']} · {page.get('reviewed_by') or 'user'}"
                if page.get("review_reason"):
                    review_text += f" · {page['review_reason']}"
            values = [
                str(page["page_number"]),
                STATUS_ZH.get(page["status"], page["status"]),
                page.get("extraction_method") or "",
                review_text,
                "需复核" if page["requires_review"] else "无需",
            ]
            for c, text in enumerate(values):
                self.table.setItem(r, c, QTableWidgetItem(str(text)))
        self.finish_btn.setEnabled(info["all_review_pages_verified"])
        if info["pages_requiring_review"]:
            self.finish_btn.setToolTip(
                "还有应复核页未核实："
                + ",".join(str(n) for n in info["pages_requiring_review"])
            )
        else:
            self.finish_btn.setToolTip("")

    def _selected_page(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._rows):
            QMessageBox.information(self, "未选择", "请先在表格中选择一页。")
            return None
        return self._rows[row]

    def _show_page_detail(self) -> None:
        page = self._selected_page()
        if page is None:
            return
        facts = page.get("facts") or []
        if not facts:
            self.detail.setPlainText("该页没有提取到候选条款。")
            return
        lines = [f"该页共 {len(facts)} 条候选条款（供与原件对照；均为候选，需按条款确认）：", ""]
        for f in facts:
            value = f.get("fact_value") or "（关键词命中，无具体值）"
            lines.append(f"· {f['fact_key']} = {value}  [{f.get('review_status') or 'candidate'}]")
            if f.get("quote_text"):
                lines.append(f"  原文：{f['quote_text']}")
        self.detail.setPlainText("\n".join(lines))

    # ---- 操作 ----

    def _verify_page(self) -> None:
        page = self._selected_page()
        if page is None:
            return
        reason = self.reason_edit.text().strip()
        try:
            page_review.set_page_review(
                self.conn, self.project_id, self.file_id,
                page["page_number"], "verified", reason=reason,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "无法核实", str(exc))
            return
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("写入页复核失败")
            QMessageBox.critical(self, "写入失败", "复核信息未能写入数据库，请重试。")
            return
        self.reason_edit.clear()
        self._reload()

    def _return_page(self) -> None:
        page = self._selected_page()
        if page is None:
            return
        try:
            page_review.set_page_review(
                self.conn, self.project_id, self.file_id,
                page["page_number"], "needs_review",
            )
        except ValueError as exc:
            QMessageBox.warning(self, "无法退回", str(exc))
            return
        self._reload()

    def _finish(self) -> None:
        try:
            page_review.mark_document_pages_reviewed(self.conn, self.project_id, self.file_id)
        except ValueError as exc:
            QMessageBox.warning(self, "还不能完成复核", str(exc))
            return
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("写入复核完成状态失败")
            QMessageBox.critical(self, "写入失败", "复核完成状态未能写入数据库，请重试。")
            return
        QMessageBox.information(
            self, "复核完成",
            "文档门控已解除；候选条款请继续在“合同条款确认…”中逐条确认。",
        )
        self.accept()
