"""工作台页面（Phase 8）。

Tab 结构：期次概览 | 清单明细 | 审核问题中心 | 匹配复核 | 成果导出。
纪律落进 UI：
- 修改匹配级别 / 处理异常必须填写原因（原则 14）；
- 所有查询只读，绝不直接改业务数据；
- 期次方向标记（对上/对下）保存前要求确认。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from costguard.core.anomalies import coverage as detection_coverage
from costguard.core.anomalies import engine as anomaly_engine
from costguard.core.contracts import run_contract
from costguard.core.engine import crosscheck, settlement_io
from costguard.core.export import excel_export
from costguard.core.matching import matching
from costguard.core.reporting import build_report_model
from costguard.platform import paths as platform_paths
from costguard.ui import theme
from costguard.ui.labels import (
    DIRECTION_ZH,
    LEVEL_ZH,
    audit_action_zh,
    item_status_zh,
    level_short_zh,
    method_zh,
    normalize_business_text,
    parse_group_key,
    rule_zh,
    subject_type_zh,
)
from costguard.ui.widgets import badge_item, fill_cell, make_data_table

# 展示层与数据层分离（复核项 #6 同源）：内部枚举原样保留在 DB/Tooltip，
# 本表只做 UI 显示转换；未知值使用安全中文兜底，原始枚举仅保留在 Tooltip。
SEVERITY_ZH = {"high": "高", "medium": "中", "low": "低", "info": "提示"}
SEVERITY_KIND = {"high": "danger", "medium": "warning", "low": "neutral", "info": "info"}


def _evidence_entry_text(entry, *, source: bool = False) -> str:
    """把证据 JSON 转为普通用户可读的中文摘要，不泄露内部字段/异常。"""
    if not isinstance(entry, dict):
        return normalize_business_text(str(entry))
    if "technical_error" in entry:
        return "技术详情已隐藏，请在高级信息中查看。"
    labels = {
        "step": "步骤", "rule": "规则", "status": "处理状态", "reason": "原因",
        "actor": "处理人", "confidence": "置信度", "result": "结果", "period": "期次",
        "period_id": "期次标识", "direction": "方向", "location": "位置",
        "quote": "原文摘录", "raw_value": "原始值", "file": "文件", "sheet": "Sheet",
        "n_items": "明细行数", "missing_rows": "缺失行数", "anomaly_id": "问题标识",
        "match_id": "匹配组标识", "subject_id": "对象标识", "items": "涉及明细",
        "role": "工作表角色", "header_range": "表头范围", "data_range": "数据范围",
        "formula": "计算公式", "difference": "差异", "source": "来源",
        "field": "字段", "row": "行", "col": "列", "details": "计算明细",
        "qty": "工程量", "quantity": "工程量", "price": "单价", "unit_price": "单价",
        "amount": "原始合价", "expect": "计算合价", "details_sum": "明细合计",
        "subtotal": "原表小计", "raw": "原始值", "value": "标准值",
        "evidence_id": "Evidence ID", "file_id": "文件标识", "sheet_id": "工作表标识",
        "finding_id": "问题标识", "fingerprint": "问题指纹", "impact": "影响",
        "limitations": "限制", "recommendation": "建议", "raw_values": "原始值",
        "normalized_values": "标准化值", "detection_mode": "检测方式",
    }
    chunks = []
    for key, value in entry.items():
        if key in {"technical_error", "rule_name", "subject_type"}:
            continue
        label = labels.get(key)
        if not label:
            continue
        if key == "rule":
            value = rule_zh(str(value))
        elif key == "status":
            value = item_status_zh(str(value))
        elif key == "direction":
            # 证据写入时可能已经保存过中文标签；避免二次转换把
            # “对上结算/对下结算”误降级成“未标记”。
            value = DIRECTION_ZH.get(
                str(value),
                str(value) if str(value) in DIRECTION_ZH.values() else "未标记",
            )
        elif key == "role":
            value = {"settlement": "结算清单", "contract_control": "合同/控制性内容",
                     "non_settlement": "非结算内容"}.get(str(value), "待人工确认")
        elif isinstance(value, dict):
            value = "；".join(
                f"{labels.get(str(k), '字段')}：{normalize_business_text(str(v))}"
                for k, v in value.items()
                if str(k) not in {"technical_error", "rule_name", "subject_type"}
            ) or "已记录"
        elif isinstance(value, (list, tuple)):
            value = "、".join(map(str, value))
        chunks.append(f"{label}：{normalize_business_text(str(value))}")
    if not chunks:
        return "来源信息已记录，详见证据索引。" if source else "计算过程已记录，详见证据索引。"
    return ("来源：" if source else "") + "；".join(chunks)
# SheetConfirmDialog 已移至 ui.dialogs.sheet_confirm（视觉分组重构版）；
# 业务规则/校验/接口保持不变。此处 re-export 维持既有导入路径兼容。
from costguard.ui.dialogs.sheet_confirm import (  # noqa: E402
    SheetConfirmDialog,
)


class ReasonDialog(QDialog):
    """原因输入对话框（原则 14：人工修正必须记录原因）。"""

    def __init__(self, title: str, prompt: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(420, 180)
        form = QFormLayout(self)
        label = QLabel(prompt)
        label.setWordWrap(True)
        form.addRow(label)
        self.reason_edit = QTextEdit()
        self.reason_edit.setPlaceholderText("必填：请说明修改原因（将写入审计日志）")
        form.addRow("原因：", self.reason_edit)
        bb = QDialogButtonBox()
        ok_btn = bb.addButton("确定", QDialogButtonBox.AcceptRole)
        ok_btn.setObjectName("btnPrimary")
        bb.addButton("取消", QDialogButtonBox.RejectRole)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _accept(self):
        if not self.reason_edit.toPlainText().strip():
            QMessageBox.warning(self, "原因必填", "人工修改必须记录原因（原则 14）")
            return
        self.accept()

    def reason(self) -> str:
        return self.reason_edit.toPlainText().strip()


def _make_table(headers: list[str], **spec) -> QTableWidget:
    """统一表格工厂（theme/widgets）；spec 透传列宽/对齐策略。"""
    return make_data_table(headers, **spec)


def project_status_summary(conn, project_id: int) -> str:
    """工作台顶部状态信息：集中显示当前待处理事项和最近校核级别。"""
    summary = build_report_model(conn, project_id).project_summary
    if not summary.run_availability["available"]:
        # 运行级边界优先于所有历史统计；不要把 current_scope 的空集显示成
        # “尚未校核”，也不要让旧成功结果继续成为工作台的当前状态。
        return "数据库不可写 · 当前结果不可用"
    source_files = summary.source_files
    n = summary.pending["sheets"]
    high_open = summary.pending["high_risk"]
    pending_matches = summary.pending["matches"]
    period_count = sum(summary.directions.values())
    parts = []
    if not source_files:
        parts.append("尚未导入资料")
    elif not period_count:
        parts.append("暂无可审核结算期次")
    if n:
        parts.append(f"待人工确认工作表 {n} 张")
    if high_open:
        parts.append(f"高风险未处理 {high_open} 项")
    if pending_matches:
        parts.append(f"待确认匹配 {pending_matches} 组")
    level_zh = {"sufficient": "校核充分", "findings": "校核有发现", "insufficient": "校核不充分"}
    verification_status = summary.verification["status"]
    if verification_status != "not_started":
        parts.append(f"最近校核：{level_zh.get(verification_status, '待复核')}")
    elif period_count:
        parts.append("最近校核：尚未校核")
    if period_count and summary.detection_coverage["status"] != "complete":
        parts.append("异常检测覆盖率未完整")
    if period_count and summary.aggregate_coverage["status"] != "complete":
        parts.append("聚合验证覆盖率未完整")
    if summary.pending["manifest_status"] in {"incomplete", "mismatch"}:
        parts.append("权威批次清单未闭合")
    return " · ".join(p for p in parts if p)


class WorkbenchPage(QWidget):
    def __init__(self, conn, project, project_dir: str, on_back):
        super().__init__()
        self.conn = conn
        self.project = project
        self.project_dir = project_dir
        self.tabs = QTabWidget()
        self.tabs.setObjectName("workbenchTabs")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SP_L, theme.SP_M, theme.SP_L, theme.SP_M)
        layout.setSpacing(theme.SP_S)
        top = QHBoxLayout()
        back_btn = QPushButton("← 返回项目列表")
        back_btn.setObjectName("btnTertiary")
        back_btn.clicked.connect(on_back)
        title = QLabel(project.name)
        title.setStyleSheet(
            "font-size: 15px; font-weight: 600; background: transparent;")
        pending_count = project_status_summary(conn, project.project_id)
        self.status_label = QLabel(pending_count)
        self.status_label.setToolTip(f"数据结构版本：{project.schema_version}")
        self.status_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent;")
        top.addWidget(back_btn)
        top.addSpacing(theme.SP_S)
        top.addWidget(title)
        top.addSpacing(theme.SP_M)
        top.addWidget(self.status_label, 1)
        layout.addLayout(top)
        self.overview_box = self._project_overview()
        layout.addWidget(self.overview_box)
        layout.addWidget(self.tabs, 1)
        self.tabs.addTab(self._period_tab(), "期次概览")
        self.tabs.addTab(self._items_tab(), "清单明细")
        self.tabs.addTab(self._anomaly_tab(), "审核问题中心")
        self.tabs.addTab(self._match_tab(), "匹配复核")
        self.tabs.addTab(self._export_tab(), "成果导出")
        self.refresh_all()

    def _project_overview(self) -> QWidget:
        """项目审核总览：把当前缺口和唯一下一步动作放在工作台顶部。"""
        box = QWidget()
        box.setObjectName("projectOverview")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(theme.SP_M, theme.SP_S, theme.SP_M, theme.SP_S)
        outer.setSpacing(theme.SP_XS)
        head = QHBoxLayout()
        title = QLabel("项目审核总览")
        title.setStyleSheet("font-weight: 600; background: transparent;")
        head.addWidget(title)
        head.addStretch(1)
        self.next_action_btn = QPushButton("下一步建议")
        self.next_action_btn.setObjectName("btnPrimary")
        self.next_action_btn.clicked.connect(self._follow_next_action)
        head.addWidget(self.next_action_btn)
        outer.addLayout(head)
        cards = QHBoxLayout()
        cards.setSpacing(theme.SP_S)
        self.overview_values: dict[str, QLabel] = {}
        for key, label in (
            ("files", "已导入文件"),
            ("pending_sheets", "待确认工作表"),
            ("upward", "对上结算期次"),
            ("downward", "对下结算期次"),
            ("high", "高风险未处理"),
            ("matches", "待确认匹配"),
            ("latest", "最近校核"),
        ):
            card = QWidget()
            cv = QVBoxLayout(card)
            cv.setContentsMargins(theme.SP_S, theme.SP_XS, theme.SP_S, theme.SP_XS)
            cv.setSpacing(0)
            caption = QLabel(label)
            caption.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent;")
            value = QLabel("—")
            value.setStyleSheet("font-size: 15px; font-weight: 600; background: transparent;")
            cv.addWidget(caption)
            cv.addWidget(value)
            cards.addWidget(card, 1)
            self.overview_values[key] = value
        outer.addLayout(cards)
        self.overview_hint = QLabel("")
        self.overview_hint.setWordWrap(True)
        self.overview_hint.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent;")
        outer.addWidget(self.overview_hint)
        return box

    def refresh_overview(self):
        pid = self.project.project_id
        summary = build_report_model(self.conn, pid).project_summary
        files = summary.source_files
        period_counts = summary.directions
        pending_sheets = summary.pending["sheets"]
        high = summary.pending["high_risk"]
        matches = summary.pending["matches"]
        period_total = sum(period_counts.values())
        level_zh = {
            "sufficient": "校核充分", "findings": "校核有发现", "insufficient": "校核不充分",
        }
        latest_text = level_zh.get(summary.verification["status"], "待复核") \
            if summary.verification["status"] != "not_started" else "尚未校核"
        values = {
            "files": str(files), "pending_sheets": str(pending_sheets),
            "upward": str(period_counts.get("upward", 0)),
            "downward": str(period_counts.get("downward", 0)),
            "high": str(high), "matches": str(matches), "latest": latest_text,
        }
        for key, value in values.items():
            self.overview_values[key].setText(value)
        if not summary.run_availability["available"]:
            self._next_action = "crosscheck"
            suggestion = "数据库不可写，当前结果不可用；请修复写入问题后重新运行校核"
        elif pending_sheets:
            self._next_action = "sheets"
            suggestion = f"还有 {pending_sheets} 张工作表待确认"
        elif high:
            self._next_action = "anomalies"
            suggestion = f"还有 {high} 项高风险问题待处理"
        elif matches:
            self._next_action = "matches"
            suggestion = f"还有 {matches} 组匹配待确认"
        elif latest_text in {"校核不充分", "校核有发现"}:
            self._next_action = "crosscheck"
            suggestion = f"最近校核为“{latest_text}”，请查看校核明细"
        elif period_total and summary.verification["status"] == "not_started":
            self._next_action = "crosscheck"
            suggestion = "尚未执行双向校核，请先运行校核"
        elif period_total and summary.detection_coverage["status"] != "complete":
            self._next_action = "anomalies"
            suggestion = "异常检测覆盖率未完整，请先查看检测状态"
        elif period_total and summary.aggregate_coverage["status"] != "complete":
            self._next_action = "crosscheck"
            suggestion = "聚合验证覆盖率未完整，请先运行或查看双向校核"
        elif not files:
            self._next_action = "import"
            suggestion = "尚未导入文件，请先导入结算资料"
        else:
            self._next_action = "none"
            suggestion = "当前没有未处理的主要事项，可继续检查成果导出"
        self.overview_hint.setText(f"下一步：{suggestion}")
        self.next_action_btn.setText(f"下一步：{suggestion}")

    def _follow_next_action(self):
        action = getattr(self, "_next_action", "none")
        if action == "sheets":
            self._open_sheet_confirm()
        elif action == "anomalies":
            self.tabs.setCurrentIndex(2)
        elif action == "matches":
            self.tabs.setCurrentIndex(3)
        elif action in {"crosscheck", "import"}:
            self.tabs.setCurrentIndex(0)
        else:
            self.tabs.setCurrentIndex(4)

    # ---------- 期次概览 ----------
    def _period_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        btn_row = QHBoxLayout()
        import_btn = QPushButton("导入结算文件…")
        import_btn.setObjectName("btnPrimary")
        import_btn.clicked.connect(self._import_files)
        contract_btn = QPushButton("导入合同/纪要…")
        contract_btn.clicked.connect(self._import_contract)
        detect_btn = QPushButton("运行异常检测")
        detect_btn.clicked.connect(self._run_anomalies)
        check_btn = QPushButton("双向校核")
        check_btn.clicked.connect(self._run_crosscheck)
        confirm_btn = QPushButton("人工确认清单页…")
        confirm_btn.clicked.connect(self._open_sheet_confirm)
        for b, name in ((import_btn, "btnPrimary"), (contract_btn, None),
                        (confirm_btn, None), (detect_btn, None), (check_btn, None)):
            if name:
                b.setObjectName(name)
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        v.addLayout(btn_row)
        self.period_table = _make_table(
            ["期次", "标题", "方向", "明细行数", "小计行", "合同单位"],
            stretch_cols=(1,), center_cols=(2,), right_cols=(3, 4),
            fixed_widths={0: 56, 2: 72, 3: 88, 4: 72})
        v.addWidget(self.period_table, 1)
        self.dir_combo = QComboBox()
        for key, label in (("unknown", "未标记"), ("upward", "对上结算"),
                           ("downward", "对下结算")):
            self.dir_combo.addItem(label, key)  # 显示中文，userData 存内部值
        set_dir_btn = QPushButton("标记选中期次方向")
        set_dir_btn.clicked.connect(self._set_direction)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("方向："))
        row2.addWidget(self.dir_combo)
        row2.addWidget(set_dir_btn)
        row2.addStretch(1)
        v.addLayout(row2)
        return w

    def refresh_periods(self):
        rows = self.conn.execute(
            """SELECT sp.id, sp.period_no, sp.title, sp.direction, sp.contract_party,
               SUM(CASE WHEN li.flags_json NOT LIKE '%"subtotal": true%' THEN 1 ELSE 0 END) AS items,
               SUM(CASE WHEN li.flags_json LIKE '%"subtotal": true%' THEN 1 ELSE 0 END) AS subs
               FROM settlement_periods sp LEFT JOIN line_items li ON li.period_id = sp.id
               WHERE sp.project_id=? GROUP BY sp.id ORDER BY sp.period_no""",
            (self.project.project_id,),
        ).fetchall()
        t = self.period_table
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            direction = DIRECTION_ZH.get(r["direction"], "未标记")
            t.setItem(i, 0, QTableWidgetItem(str(r["period_no"])))
            t.setItem(i, 1, QTableWidgetItem(str(r["title"] or "—")))
            kind = {"upward": "info", "downward": "neutral"}.get(r["direction"], "warning")
            t.setItem(i, 2, badge_item(direction, kind))
            fill_cell(t, i, 3, r["items"], right=True)
            fill_cell(t, i, 4, r["subs"], right=True)
            fill_cell(t, i, 5, r["contract_party"] or "—", secondary=True)
            # 行必须绑定明确 period_id：对上/对下可同期号，按期号更新会双向覆盖
            t.item(i, 0).setData(Qt.UserRole, int(r["id"]))

    def _import_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择结算文件", "", "结算文件 (*.xlsx *.xlsm *.xls *.csv)")
        if not files:
            return
        ok, fail = 0, []
        pending = 0
        for f in files:
            try:
                report = settlement_io.import_settlement_file(
                    self.conn, self.project.project_id, self.project_dir, f)
                ok += 1
                pending += sum(
                    1 for s in report.sheets
                    if s.status in ("needs_role_review", "no_header", "non_settlement_form"))
            except Exception:  # noqa: BLE001 — UI 层兜底提示
                fail.append(f"{Path(f).name}：导入失败，请检查文件格式、权限或数据完整性")
        self._notify_import(ok, fail, pending)
        self.refresh_all()
        if pending:
            ret = QMessageBox.question(
                self, "待人工确认",
                f"有 {pending} 个工作表因表头歧义/无表头/表单结构待人工确认，"
                "未进入结算模型。\n现在打开「人工确认清单页」处理吗？",
                QMessageBox.Yes | QMessageBox.No)
            if ret == QMessageBox.Yes:
                self._open_sheet_confirm()

    def _open_sheet_confirm(self):
        dlg = SheetConfirmDialog(self.conn, self.project.project_id, self)
        dlg.exec()
        self.refresh_all()

    def _import_contract(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择合同/补充协议/纪要", "", "文档 (*.docx *.pdf *.txt)")
        if not files:
            return
        from costguard.core.contracts import extract as contract_extract

        ok, fail = 0, []
        for f in files:
            try:
                contract_extract.import_contract(
                    self.conn, self.project.project_id, self.project_dir, f)
                risks = contract_extract.contract_risks(self.conn, self.project.project_id)
                contract_extract.persist_risks(self.conn, self.project.project_id, risks)
                ok += 1
            except Exception:  # noqa: BLE001
                fail.append(f"{Path(f).name}：导入失败，请检查文件格式、权限或数据完整性")
        self._notify_import(ok, fail)
        self.refresh_all()

    def _run_anomalies(self):
        findings = anomaly_engine.run_anomalies(self.conn, self.project.project_id)
        summary = anomaly_engine.anomaly_summary(findings)
        QMessageBox.information(
            self, "异常检测",
            f"检测完成：高 {summary['high']} / 中 {summary['medium']} / 低 {summary['low']}\n"
            "详见「审核问题中心」页。")
        self.refresh_anomalies()
        self.refresh_overview()
        self.refresh_export_status()

    def _run_crosscheck(self):
        # 方向隔离：按方向分组逐期校核；同方向内期号唯一，无歧义
        from costguard.core.engine.crosscheck import AmbiguousPeriodError

        rows = self.conn.execute(
            "SELECT period_no, direction FROM settlement_periods WHERE project_id=?",
            (self.project.project_id,),
        ).fetchall()
        results = []
        errors = []
        for direction in sorted({r["direction"] for r in rows}):
            pnos = sorted(r["period_no"] for r in rows if r["direction"] == direction)
            try:
                results.extend(crosscheck.run_crosscheck(self.conn, self.project.project_id, pnos, direction=direction))
            except AmbiguousPeriodError:
                errors.append("当前期次存在方向歧义，请先标记方向")
            except Exception as exc:  # noqa: BLE001 — 核心层保留原始异常，UI 负责明确提示
                availability = run_contract.current_results_available(
                    self.conn, self.project.project_id
                )
                if not availability["available"]:
                    errors.append(f"数据库不可写，当前结果不可用：{exc}")
                    # 同一批次前面方向的返回值也不能在运行级边界下继续显示。
                    results = []
                else:
                    raise
        status_zh = {"match": "一致", "diff": "存在差异", "incomplete": "数据不完整"}
        level_zh = {"sufficient": "校核充分", "findings": "校核有发现",
                    "insufficient": "校核不充分"}
        dir_zh = DIRECTION_ZH
        lines = []
        for r in results:
            line = (f"第{r.period_no}期{dir_zh.get(r.direction, '未标记')}："
                    f"{level_zh.get(r.verification_level, '待复核')}"
                    f"（A/B {status_zh.get(r.status, '待复核')}；A={r.path_a_total}，B={r.path_b_total}"
                    f"；参与明细 {r.detail_rows}，排除小计 {r.excluded_subtotal_rows}，"
                    f"排除标题/说明 {r.excluded_title_rows}，"
                    f"待确认工作表 {r.pending_sheets}，"
                    f"取数范围未证明 {r.range_unproven_sheets}）")
            # C 控制值独立于 A/B：一致时若控制差异仍在，必须显著提示，不得被
            # A/B 一致掩盖（独立复核发现 #6）
            if r.control_status == "diff":
                line += f"　⚠C控制差异={r.control_diff}（待复核）"
            elif r.control_status == "not_available":
                line += "　C控制不可用"
            else:
                line += f"　C={r.raw_subtotal}"
            lines.append(line)
        msg = "\n".join(lines)
        if any(r.verification_level == "insufficient" for r in results):
            msg = "⚠ 校核不充分：证据不足或仍有工作表待人工确认，不得视为通过：\n" + msg
        elif any(r.control_status == "diff" for r in results):
            msg = "⚠ 存在 C 控制差异（A/B 一致也不代表全部通过）：\n" + msg
        if errors:
            msg += "\n\n以下期号存在方向歧义，已跳过（请先标记方向）：\n" + "\n".join(errors)
        QMessageBox.information(self, "双向校核", msg or "无期次可校核")
        if any(r.status == "diff" for r in results):
            self.tabs.setCurrentIndex(2)
        self.refresh_anomalies()
        self.refresh_overview()
        self.refresh_export_status()

    def _set_direction(self):
        row = self.period_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "标记方向", "请先在表中选择期次")
            return
        period_id = self.period_table.item(row, 0).data(Qt.UserRole)
        if period_id is None:
            QMessageBox.warning(self, "标记方向", "行缺少期次标识，请刷新后重试")
            return
        pno = int(self.period_table.item(row, 0).text())
        direction = self.dir_combo.currentData() or "unknown"
        dir_zh = DIRECTION_ZH[direction]
        dlg = ReasonDialog("标记期次方向", f"将第 {pno} 期方向标记为「{dir_zh}」", self)
        if dlg.exec() != QDialog.Accepted:
            return
        import sqlite3 as _sq

        from costguard.core.evidence import audit as audit_log

        with self.conn:
            # 按 period_id 精确更新：同 period_no 的另一方向不受影响
            try:
                cur = self.conn.execute(
                    "UPDATE settlement_periods SET direction=? WHERE id=?",
                    (direction, period_id))
            except _sq.IntegrityError:
                # 目标方向同期号已有期次（v3 唯一约束）：友好拒绝，不做部分更新
                QMessageBox.warning(
                    self, "标记方向",
                    f"第 {pno} 期在「{dir_zh}」方向已存在期次，无法重复标记。\n"
                    "如需合并，请先人工核清两期数据。")
                return
            if cur.rowcount != 1:
                QMessageBox.warning(
                    self,
                    "标记方向",
                    "期次方向未更新，请刷新后检查期次记录。",
                )
                return
        audit_log.record_audit(
            self.conn, self.project.project_id, "user", "set_direction", f"period:{period_id}",
            None, {"direction": direction, "period_no": pno}, dlg.reason())
        settlement_io.invalidate_crosscheck_results(self.conn, self.project.project_id)
        self.refresh_all()

    # ---------- 清单明细 ----------
    def _items_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        filter_row = QHBoxLayout()
        self.items_search = QLineEdit()
        self.items_search.setPlaceholderText("搜索编码、名称或单位")
        self.items_search.setClearButtonEnabled(True)
        self.items_search.textChanged.connect(self._items_filter_changed)
        filter_row.addWidget(QLabel("搜索："))
        filter_row.addWidget(self.items_search, 2)
        self.items_direction = QComboBox()
        self.items_direction.addItem("全部方向", "")
        for key, label in (("upward", "对上结算"), ("downward", "对下结算"),
                           ("unknown", "未标记")):
            self.items_direction.addItem(label, key)
        self.items_direction.currentIndexChanged.connect(self._items_filter_changed)
        filter_row.addWidget(QLabel("方向："))
        filter_row.addWidget(self.items_direction)
        self.items_period = QComboBox()
        self.items_period.addItem("全部期次", "")
        self.items_period.currentIndexChanged.connect(self._items_filter_changed)
        filter_row.addWidget(QLabel("期次："))
        filter_row.addWidget(self.items_period)
        self.items_data_status = QComboBox()
        self.items_data_status.addItem("全部数据状态", "")
        self.items_data_status.addItem("资料待补", "pending")
        self.items_data_status.addItem("已有原始合价", "available")
        self.items_data_status.currentIndexChanged.connect(self._items_filter_changed)
        filter_row.addWidget(QLabel("数据状态："))
        filter_row.addWidget(self.items_data_status)
        self.items_row_type = QComboBox()
        self.items_row_type.addItem("明细与小计", "")
        self.items_row_type.addItem("仅明细", "detail")
        self.items_row_type.addItem("仅小计", "subtotal")
        self.items_row_type.currentIndexChanged.connect(self._items_filter_changed)
        filter_row.addWidget(self.items_row_type)
        v.addLayout(filter_row)
        self.items_table = _make_table(
            ["方向", "期次", "编码", "名称", "单位", "数量", "单价", "合价", "税率", "出处(数量)"],
            stretch_cols=(3,), right_cols=(5, 6, 7, 8),
            fixed_widths={0: 60, 1: 52, 2: 118, 4: 56, 9: 96})
        # 分页查询必须在完整结果集上排序，不能只对当前页做 QTableWidget
        # 的本地排序；表头点击因此转为安全的 SQL 字段排序。
        self.items_sort_column = 1
        self.items_sort_desc = False
        self.items_table.horizontalHeader().setSortIndicatorShown(True)
        self.items_table.horizontalHeader().setSortIndicator(1, Qt.AscendingOrder)
        self.items_table.horizontalHeader().sectionClicked.connect(self._items_sort_changed)
        self.items_table.cellDoubleClicked.connect(self._show_item_evidence)
        self.item_evidence_panel = QTextEdit()
        self.item_evidence_panel.setReadOnly(True)
        self.item_evidence_panel.setPlaceholderText("双击清单行查看证据详情")
        self.item_evidence_panel.setMinimumWidth(300)
        item_split = QSplitter(Qt.Horizontal)
        item_split.addWidget(self.items_table)
        item_split.addWidget(self.item_evidence_panel)
        item_split.setSizes([700, 300])
        v.addWidget(item_split, 1)

        # ---- 分页器（P0-1：取消静默 LIMIT 2000，全部数据可翻页到达）----
        self.PAGE_SIZE = 500
        self.items_page = 0
        self.items_total = 0
        pager = QHBoxLayout()
        first_btn = QPushButton("首页")
        prev_btn = QPushButton("上一页")
        next_btn = QPushButton("下一页")
        last_btn = QPushButton("末页")
        for b in (first_btn, prev_btn, next_btn, last_btn):
            b.setObjectName("btnTertiary")
        first_btn.clicked.connect(lambda: self._goto_items_page(0))
        prev_btn.clicked.connect(lambda: self._goto_items_page(self.items_page - 1))
        next_btn.clicked.connect(lambda: self._goto_items_page(self.items_page + 1))
        last_btn.clicked.connect(lambda: self._goto_items_page(10 ** 9))
        pager.addWidget(first_btn)
        pager.addWidget(prev_btn)
        self.items_total_label = QLabel("共 0 条")
        pager.addWidget(self.items_total_label)
        pager.addWidget(next_btn)
        pager.addWidget(last_btn)
        pager.addStretch(1)
        v.addLayout(pager)
        return w

    def _refresh_item_period_options(self):
        current = self.items_period.currentData() if self.items_period.count() else ""
        rows = self.conn.execute(
            """SELECT id, period_no, direction FROM settlement_periods
               WHERE project_id=? ORDER BY period_no, id""", (self.project.project_id,)
        ).fetchall()
        self.items_period.blockSignals(True)
        self.items_period.clear()
        self.items_period.addItem("全部期次", "")
        for row in rows:
            label = f"第 {row['period_no']} 期 · {DIRECTION_ZH.get(row['direction'], '未标记')}"
            self.items_period.addItem(label, str(row["id"]))
        index = self.items_period.findData(current)
        self.items_period.setCurrentIndex(index if index >= 0 else 0)
        self.items_period.blockSignals(False)

    def _items_filter_changed(self, *_args):
        self.items_page = 0
        self.refresh_items()

    def _items_sort_changed(self, column: int):
        if column == self.items_sort_column:
            self.items_sort_desc = not self.items_sort_desc
        else:
            self.items_sort_column = column
            self.items_sort_desc = False
        order = Qt.DescendingOrder if self.items_sort_desc else Qt.AscendingOrder
        self.items_table.horizontalHeader().setSortIndicator(column, order)
        self.items_page = 0
        self.refresh_items()

    def _goto_items_page(self, page: int):
        max_page = max(0, (self.items_total - 1) // self.PAGE_SIZE) if self.items_total else 0
        self.items_page = max(0, min(page, max_page))
        self.refresh_items()

    def refresh_items(self):
        # P0-1：全量计数 + 分页查询，杜绝静默 LIMIT 截断。筛选条件同时作用于
        # COUNT 与明细查询，搜索和人工复核不会只在当前页内误判“没有数据”。
        self._refresh_item_period_options()
        where = ["sp.project_id=?"]
        params: list[object] = [self.project.project_id]
        search = self.items_search.text().strip()
        if search:
            like = f"%{search}%"
            where.append("(COALESCE(li.code, '') LIKE ? OR COALESCE(li.name, '') LIKE ? OR COALESCE(li.unit, '') LIKE ?)")
            params.extend([like, like, like])
        direction = self.items_direction.currentData()
        if direction:
            where.append("COALESCE(sp.direction, 'unknown')=?")
            params.append(direction)
        period_id = self.items_period.currentData()
        if period_id:
            where.append("sp.id=?")
            params.append(int(period_id))
        data_status = self.items_data_status.currentData()
        if data_status == "pending":
            where.append("(li.amount IS NULL OR li.quantity IS NULL OR li.unit_price IS NULL)")
        elif data_status == "available":
            where.append("li.amount IS NOT NULL")
        row_type = self.items_row_type.currentData()
        if row_type == "detail":
            where.append("li.flags_json NOT LIKE '%\"subtotal\": true%'")
        elif row_type == "subtotal":
            where.append("li.flags_json LIKE '%\"subtotal\": true%'")
        where_sql = " AND ".join(where)
        self.items_total = self.conn.execute(
            f"""SELECT COUNT(*) c FROM line_items li
                JOIN settlement_periods sp ON sp.id = li.period_id
                WHERE {where_sql}""", params,
        ).fetchone()["c"]
        max_page = max(0, (self.items_total - 1) // self.PAGE_SIZE) if self.items_total else 0
        self.items_page = max(0, min(self.items_page, max_page))
        offset = self.items_page * self.PAGE_SIZE
        sort_columns = {
            0: "COALESCE(sp.direction, 'unknown')",
            1: "sp.period_no",
            2: "COALESCE(li.code, '')",
            3: "COALESCE(li.name, '')",
            4: "COALESCE(li.unit, '')",
            5: "CAST(li.quantity AS REAL)",
            6: "CAST(li.unit_price AS REAL)",
            7: "CAST(li.amount AS REAL)",
            8: "CAST(li.tax_rate AS REAL)",
            9: "li.id",
        }
        sort_sql = sort_columns.get(self.items_sort_column, "sp.period_no")
        direction_sql = "DESC" if self.items_sort_desc else "ASC"
        rows = self.conn.execute(
            f"""SELECT li.*, sp.period_no AS pno, sp.direction AS direction
                FROM line_items li JOIN settlement_periods sp ON sp.id = li.period_id
                WHERE {where_sql} ORDER BY {sort_sql} {direction_sql}, li.id LIMIT ? OFFSET ?""",
            (*params, self.PAGE_SIZE, offset),
        ).fetchall()
        t = self.items_table
        t.setRowCount(len(rows))
        shown_from = offset + 1 if rows else 0
        shown_to = offset + len(rows)
        self.items_total_label.setText(
            f"共 {self.items_total} 条 / 当前显示 {shown_from}-{shown_to} 条")
        for i, r in enumerate(rows):
            try:
                flags = json.loads(r["flags_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                flags = {}
            try:
                ev = json.loads(r["qty_evid"]) if r["qty_evid"] else None
            except (TypeError, json.JSONDecodeError):
                ev = None
            direction_item = QTableWidgetItem(DIRECTION_ZH.get(r["direction"], "未标记"))
            direction_item.setData(Qt.UserRole, int(r["id"]))
            t.setItem(i, 0, direction_item)
            t.setItem(i, 1, QTableWidgetItem(str(r["pno"])))
            t.setItem(i, 2, QTableWidgetItem(str(r["code"] or "—")))
            prefix = "【小计】" if flags.get("subtotal") else ""
            t.setItem(i, 3, QTableWidgetItem(prefix + (r["name"] or "—")))
            t.setItem(i, 4, QTableWidgetItem(str(r["unit"] or "—")))
            for c, key in ((5, "quantity"), (6, "unit_price"), (7, "amount"), (8, "tax_rate")):
                v = r[key]
                fill_cell(t, i, c, v if v is not None else None, right=True,
                          secondary=v is None)
            ev_txt = f"行{ev['row']}列{ev['col']}" if ev else "—"
            fill_cell(t, i, 9, ev_txt, secondary=(ev is None), mono=bool(ev))

    def _show_item_evidence(self, row: int, _column: int):
        item = self.items_table.item(row, 0)
        item_id = item.data(Qt.UserRole) if item else None
        if item_id is None:
            return
        record = self.conn.execute(
            """SELECT li.*, sp.period_no, sp.direction, rs.sheet_name,
                      sf.original_name
               FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
               LEFT JOIN raw_sheets rs ON rs.id=li.sheet_id
               LEFT JOIN parse_batches pb ON pb.id=rs.batch_id
               LEFT JOIN source_files sf ON sf.id=pb.file_id
               WHERE li.id=? AND sp.project_id=?""",
            (int(item_id), self.project.project_id),
        ).fetchone()
        if not record:
            self.item_evidence_panel.setPlainText("未找到对应清单行，可能已被重新导入。")
            return
        lines = [
            f"清单行 #{record['id']}",
            f"方向：{DIRECTION_ZH.get(record['direction'], '未标记')}",
            f"期次：第 {record['period_no']} 期",
            f"来源文件：{record['original_name'] or '—'}",
            f"Sheet：{record['sheet_name'] or '—'}",
            f"编码：{record['code'] or '—'}　名称：{record['name'] or '—'}",
            f"单位：{record['unit'] or '—'}　数量：{record['quantity'] or '待补资料'}",
            f"单价：{record['unit_price'] or '待补资料'}　合价：{record['amount'] or '待补资料'}",
        ]
        try:
            row_flags = json.loads(record["flags_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            row_flags = {}
        row_evidence_id = row_flags.get("source_evidence_id")
        # qty_evid/price_evid/amount_evid 在历史库中保存的是字段来源 JSON，
        # 并非 evidence 表的整数外键。按来源字段解析后给出稳定的 Evidence ID
        #（清单行 + 字段），同时保留文件、Sheet、行列和原始值，避免双击详情空白。
        field_labels = {
            "qty_evid": "数量", "price_evid": "单价", "amount_evid": "合价",
        }
        provenance_found = False
        lines.append("证据记录：")
        for key, label in field_labels.items():
            raw_provenance = record[key]
            if not raw_provenance:
                continue
            provenance = None
            try:
                provenance = json.loads(raw_provenance) if isinstance(raw_provenance, str) else raw_provenance
            except (TypeError, json.JSONDecodeError):
                provenance = None
            if isinstance(provenance, dict):
                provenance_found = True
                row_no = provenance.get("row", "—")
                col_no = provenance.get("col", "—")
                raw_value = provenance.get("raw", provenance.get("value", "—"))
                evidence_id = provenance.get("evidence_id") or row_evidence_id
                evidence_label = (
                    f"Evidence ID {evidence_id}" if evidence_id not in (None, "")
                    else f"来源定位 ID LI-{record['id']}-{key.replace('_evid', '')}"
                )
                lines.append(
                    f"- {evidence_label}：{label}来源定位"
                )
                lines.append(
                    f"  来源：{record['original_name'] or '—'} / Sheet「{record['sheet_name'] or '—'}」"
                    f" / 行{row_no}列{col_no}；原始值：{normalize_business_text(str(raw_value))}"
                )
                continue
            # 兼容未来/外部导入的整数 evidence_id。
            try:
                evidence_id = int(raw_provenance)
            except (TypeError, ValueError):
                continue
            evidence = self.conn.execute(
                "SELECT summary, sources_json FROM evidence WHERE id=?", (evidence_id,)
            ).fetchone()
            if not evidence:
                continue
            provenance_found = True
            lines.append(f"- Evidence ID {evidence_id}：{normalize_business_text(evidence['summary'])}")
            try:
                sources = json.loads(evidence["sources_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                sources = []
            if isinstance(sources, dict):
                sources = [sources]
            for source in sources[:3]:
                if isinstance(source, dict):
                    location = source.get("location") or source.get("cell") or ""
                    raw = source.get("quote") or source.get("raw_value") or ""
                    if location or raw:
                        lines.append(f"  来源：{location} {raw}".strip())
        if not provenance_found:
            lines.append("- 当前清单行没有独立字段来源记录；可根据上方文件、Sheet、行ID回查保真数据。")
        anomalies = self.conn.execute(
            """SELECT rule_id, message, status, evidence_id FROM anomalies
               WHERE project_id=? AND subject_type='line_item' AND subject_id=? ORDER BY id""",
            (self.project.project_id, int(item_id)),
        ).fetchall()
        if anomalies:
            lines.append("相关异常：")
            for anomaly in anomalies:
                lines.append(
                    f"- {rule_zh(anomaly['rule_id'])}（{item_status_zh(anomaly['status'])}）："
                    f"{normalize_business_text(anomaly['message'])}"
                )
        else:
            lines.append("相关异常：暂无记录")
        self.item_evidence_panel.setPlainText("\n".join(lines))

    # ---------- 异常检测 ----------
    def _anomaly_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.anomaly_summary_label = QLabel("")
        self.anomaly_summary_label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; background: transparent;")
        self.anomaly_summary_label.setWordWrap(True)
        v.addWidget(self.anomaly_summary_label)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh_anomalies)
        process_btn = QPushButton("处理选中问题…")
        process_btn.setObjectName("btnPrimary")
        process_btn.clicked.connect(self._process_anomaly)
        row = QHBoxLayout()
        row.addWidget(refresh_btn)
        row.addWidget(process_btn)
        row.addStretch(1)
        v.addLayout(row)
        self.anomaly_table = _make_table(
            ["编号", "方向", "级别", "规则", "说明", "证据ID", "状态"],
            stretch_cols=(4,), center_cols=(2,),
            fixed_widths={0: 56, 1: 56, 3: 150, 5: 90, 6: 64})
        self.anomaly_table.cellDoubleClicked.connect(self._show_anomaly_detail)
        self.anomaly_detail_panel = QTextEdit()
        self.anomaly_detail_panel.setReadOnly(True)
        self.anomaly_detail_panel.setPlaceholderText("双击问题查看计算过程、来源证据和处理历史")
        anomaly_split = QSplitter(Qt.Horizontal)
        anomaly_split.addWidget(self.anomaly_table)
        anomaly_split.addWidget(self.anomaly_detail_panel)
        anomaly_split.setSizes([700, 300])
        v.addWidget(anomaly_split, 1)
        return w

    def refresh_anomalies(self):
        scope, scope_params = run_contract.current_scope(self.conn, self.project.project_id, "a")
        rows = self.conn.execute(
            f"""SELECT a.id, a.rule_id, a.severity, a.message, a.evidence_id, a.status,
                      COALESCE(sp_item.direction, sp_period.direction, sp_sheet.direction, '')
                        AS direction
               FROM anomalies a
               LEFT JOIN line_items li
                 ON a.subject_type='line_item' AND li.id=a.subject_id
               LEFT JOIN settlement_periods sp_item ON sp_item.id=li.period_id
               LEFT JOIN settlement_periods sp_period
                 ON a.subject_type='period' AND sp_period.id=a.subject_id
               LEFT JOIN raw_sheets rs
                 ON a.subject_type='sheet' AND rs.id=a.subject_id
               LEFT JOIN settlement_periods sp_sheet ON sp_sheet.id=rs.period_id
               WHERE a.project_id=? AND {scope} ORDER BY CASE a.severity
               WHEN 'high' THEN 0 WHEN 'medium' THEN 1
               WHEN 'low' THEN 2 ELSE 3 END, a.id""",
            (self.project.project_id, *scope_params),
        ).fetchall()
        counts = {"high": 0, "medium": 0, "pending": 0, "deferred": 0, "processed": 0}
        for row in rows:
            if row["severity"] == "high":
                counts["high"] += 1
            elif row["severity"] == "medium":
                counts["medium"] += 1
            if row["status"] == "open":
                counts["pending"] += 1
            elif row["status"] == "deferred":
                counts["deferred"] += 1
            elif row["status"] in {"verified_no_issue", "supplemented", "corrected", "resolved"}:
                counts["processed"] += 1
            else:
                # 未知历史状态仍归入待人工确认，避免被误算为已处理。
                counts["pending"] += 1
        coverage = detection_coverage.coverage_summary(self.conn, self.project.project_id)
        coverage_text = (
            f"检测覆盖率 {coverage['status']}（应执行 {coverage['expected_count']}，"
            f"已执行 {coverage['executed_count']}，跳过 {coverage['skipped_count']}，"
            f"失败 {coverage['failed_count']}）"
        )
        self.anomaly_summary_label.setText(
            f"高风险 {counts['high']} 项　中风险 {counts['medium']} 项　"
            f"待处理 {counts['pending']} 项　暂不处理 {counts['deferred']} 项　"
            f"已处理 {counts['processed']} 项　{coverage_text}"
        )
        t = self.anomaly_table
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            sev_text = SEVERITY_ZH.get(r["severity"], "其他")
            sev_kind = SEVERITY_KIND.get(r["severity"], "neutral")
            id_item = QTableWidgetItem(str(r["id"]))
            id_item.setData(Qt.UserRole, int(r["id"]))
            t.setItem(i, 0, id_item)
            t.setItem(i, 1, QTableWidgetItem(DIRECTION_ZH.get(r["direction"], "项目级")))
            t.setItem(i, 2, badge_item(sev_text, sev_kind))
            rule_item = QTableWidgetItem(rule_zh(r["rule_id"]))
            rule_item.setToolTip(r["rule_id"])
            t.setItem(i, 3, rule_item)
            t.setItem(i, 4, QTableWidgetItem(normalize_business_text(r["message"])))
            fill_cell(t, i, 5, r["evidence_id"], secondary=True, mono=True)
            fill_cell(t, i, 6, item_status_zh(r["status"]))
        t.setSortingEnabled(True)

    def _show_anomaly_detail(self, row: int, _column: int):
        item = self.anomaly_table.item(row, 0)
        anomaly_id = item.data(Qt.UserRole) if item else None
        if anomaly_id is None:
            return
        anomaly = self.conn.execute(
            """SELECT id, rule_id, severity, subject_type, subject_id, evidence_id,
                      message, status, resolved_note, created_at, finding_id, fingerprint,
                      confidence, detection_mode, raw_values_json, normalized_values_json,
                      impact, limitations_json, recommendation, suppression_reason
               FROM anomalies WHERE id=? AND project_id=?""",
            (int(anomaly_id), self.project.project_id),
        ).fetchone()
        if not anomaly:
            self.anomaly_detail_panel.setPlainText("未找到对应问题，可能已重新运行检测。")
            return
        lines = [
            f"问题 #{anomaly['id']}：{rule_zh(anomaly['rule_id'])}",
            f"级别：{SEVERITY_ZH.get(anomaly['severity'], '其他')}　状态：{item_status_zh(anomaly['status'])}",
            f"对象：{subject_type_zh(anomaly['subject_type'])} #{anomaly['subject_id']}",
            f"说明：{normalize_business_text(anomaly['message'])}",
            f"发现时间：{anomaly['created_at'] or '—'}",
        ]
        if anomaly["finding_id"]:
            lines.append(f"Finding ID：{anomaly['finding_id']}")
        if anomaly["fingerprint"]:
            lines.append(f"问题指纹：{anomaly['fingerprint']}")
        lines.append(
            f"置信度：{anomaly['confidence'] or '未标注'}　检测方式：{anomaly['detection_mode'] or '未标注'}"
        )
        if anomaly["impact"]:
            lines.append(f"影响：{normalize_business_text(anomaly['impact'])}")
        if anomaly["recommendation"]:
            lines.append(f"建议：{normalize_business_text(anomaly['recommendation'])}")
        try:
            limitations = json.loads(anomaly["limitations_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            limitations = []
        if limitations:
            lines.append(f"限制：{'；'.join(normalize_business_text(str(v)) for v in limitations)}")
        for label, key in (("原始值", "raw_values_json"), ("标准化值", "normalized_values_json")):
            try:
                values = json.loads(anomaly[key] or "{}")
            except (TypeError, json.JSONDecodeError):
                values = {}
            if values:
                lines.append(f"{label}：{_evidence_entry_text(values, source=(label == '原始值'))}")
        if anomaly["suppression_reason"]:
            lines.append(f"抑制原因：{normalize_business_text(anomaly['suppression_reason'])}")
        if anomaly["evidence_id"]:
            evidence = self.conn.execute(
                "SELECT summary, steps_json, sources_json FROM evidence WHERE id=?",
                (anomaly["evidence_id"],),
            ).fetchone()
            if evidence:
                lines.append(f"Evidence ID：{anomaly['evidence_id']}")
                lines.append(f"证据摘要：{normalize_business_text(evidence['summary'])}")
                try:
                    raw_steps = json.loads(evidence["steps_json"] or "[]")
                    raw_sources = json.loads(evidence["sources_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    raw_steps, raw_sources = [], []
                # 旧版跨向校核证据把 steps 写成一个对象，新版通常是列表。
                # 统一成列表后再渲染，避免字典按 key 切片导致页面打开异常。
                steps = ([raw_steps] if isinstance(raw_steps, dict)
                         else raw_steps if isinstance(raw_steps, list) else [])
                sources = ([raw_sources] if isinstance(raw_sources, dict)
                           else raw_sources if isinstance(raw_sources, list) else [])
                if steps:
                    lines.append("计算过程：")
                    lines.extend(f"- {_evidence_entry_text(step)}" for step in steps[:8])
                if sources:
                    lines.append("来源证据：")
                    lines.extend(f"- {_evidence_entry_text(source, source=True)}" for source in sources[:8])
        history = self.conn.execute(
            """SELECT ts, actor, action, reason FROM audit_log
               WHERE project_id=? AND target=? ORDER BY id""",
            (self.project.project_id, f"anomaly:{anomaly_id}"),
        ).fetchall()
        if history:
            lines.append("处理历史：")
            lines.extend(
                f"- {h['ts']} {h['actor']}：{audit_action_zh(h['action'])}（{normalize_business_text(h['reason'])}）"
                for h in history
            )
        elif anomaly["resolved_note"]:
            lines.append(f"处理说明：{anomaly['resolved_note']}")
        else:
            lines.append("处理历史：暂无")
        self.anomaly_detail_panel.setPlainText("\n".join(lines))

    def _process_anomaly(self, forced_status: str | None = None):
        row = self.anomaly_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "处理异常", "请先选择异常")
            return
        aid = int(self.anomaly_table.item(row, 0).text())
        status_map = {
            "verified_no_issue": "已核实无问题",
            "supplemented": "已补资料",
            "corrected": "已修正",
            "deferred": "暂不处理",
            "resolved": "已处理",
        }
        new_status = forced_status
        if new_status is None:
            labels = [status_map[key] for key in ("verified_no_issue", "supplemented", "corrected", "deferred")]
            label, ok = QInputDialog.getItem(self, "处理审核问题", "处理状态", labels, 0, False)
            if not ok:
                return
            new_status = next(key for key, value in status_map.items() if value == label)
        dlg = ReasonDialog("处理异常", f"将异常 #{aid} 标记为“{status_map[new_status]}”", self)
        if dlg.exec() != QDialog.Accepted:
            return
        from costguard.core.evidence import audit as audit_log
        from costguard.core.evidence import evidence as evidence_api

        old = self.conn.execute(
            "SELECT status, run_signature, finding_id FROM anomalies WHERE id=? AND project_id=?",
            (aid, self.project.project_id),
        ).fetchone()
        if not old:
            QMessageBox.warning(self, "处理异常", "未找到对应问题，请刷新后重试。")
            return
        with run_contract._transaction(self.conn, "resolve_anomaly"):
            self.conn.execute(
                "UPDATE anomalies SET status=?, resolved_note=? WHERE id=? AND project_id=?",
                (new_status, dlg.reason(), aid, self.project.project_id))
            evidence_api.add_evidence(
                self.conn, self.project.project_id, "anomaly_resolution",
                f"异常 #{aid} 已标记为{status_map[new_status]}：{dlg.reason()}",
                steps=[{"step": "人工处理", "status": new_status, "reason": dlg.reason()}],
                sources=[{"anomaly_id": aid}],
                commit=False,
                run_signature=old["run_signature"],
                finding_id=old["finding_id"],
            )
            audit_log.record_audit(
                self.conn, self.project.project_id, "user", "resolve_anomaly", f"anomaly:{aid}",
                {"status": old["status"]}, {"status": new_status}, dlg.reason(), commit=False)
        self.refresh_anomalies()
        self.refresh_overview()
        self.refresh_export_status()

    def _resolve_anomaly(self):
        """兼容旧调用：将问题标记为通用“已处理”。"""
        self._process_anomaly("resolved")

    # ---------- 匹配复核 ----------
    def _match_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        run_btn = QPushButton("运行匹配")
        run_btn.clicked.connect(self._run_match)
        confirm_btn = QPushButton("确认选中匹配（需原因）")
        confirm_btn.clicked.connect(self._confirm_match)
        batch_btn = QPushButton("批量确认完全匹配")
        batch_btn.clicked.connect(self._batch_confirm_matches)
        override_btn = QPushButton("修正选中级别（需原因）")
        override_btn.clicked.connect(self._override_match)
        for b in (run_btn, confirm_btn, batch_btn, override_btn):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.match_table = _make_table(
            ["编号", "匹配对象", "级别", "匹配方式", "得分", "行数", "状态"],
            stretch_cols=(1,), center_cols=(2,),
            fixed_widths={0: 56, 3: 110, 4: 72, 5: 56, 6: 64})
        self.match_table.cellDoubleClicked.connect(self._show_match_detail)
        self.match_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.match_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.match_detail_panel = QTextEdit()
        self.match_detail_panel.setReadOnly(True)
        self.match_detail_panel.setPlaceholderText("双击匹配组查看左右对照、置信度和人工判断记录")
        match_split = QSplitter(Qt.Horizontal)
        match_split.addWidget(self.match_table)
        match_split.addWidget(self.match_detail_panel)
        match_split.setSizes([700, 300])
        v.addWidget(match_split, 1)
        return w

    def _run_match(self):
        groups = matching.match_items(self.conn, self.project.project_id)
        n = matching.save_matches(self.conn, self.project.project_id, groups)
        QMessageBox.information(self, "匹配", f"已生成 {n} 个匹配组，疑似/待复核项请人工确认。")
        self.refresh_matches()
        self.refresh_overview()
        self.refresh_export_status()

    def refresh_matches(self):
        scope, scope_params = run_contract.current_scope(self.conn, self.project.project_id, "m")
        rows = self.conn.execute(
            f"""SELECT id, group_key, level, method, score, item_ids_json, status FROM matches m
               WHERE m.project_id=? AND {scope} ORDER BY CASE level
               WHEN 'confirmed' THEN 0 WHEN 'probable' THEN 1 WHEN 'suspected' THEN 2
               WHEN 'incomparable' THEN 3 ELSE 4 END, m.id""",
            (self.project.project_id, *scope_params),
        ).fetchall()
        t = self.match_table
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            n_items = len(json.loads(r["item_ids_json"] or "[]"))
            id_item = QTableWidgetItem(str(r["id"]))
            id_item.setData(Qt.UserRole, int(r["id"]))
            t.setItem(i, 0, id_item)
            obj = QTableWidgetItem(parse_group_key(r["group_key"]))
            obj.setToolTip(r["group_key"])
            t.setItem(i, 1, obj)
            # “confirmed” 是规则候选级别；只有完成了人工确认，才允许使用
            # 成功色。候选本身仍需人工判断，避免把匹配建议误呈为已核实结论。
            kind = ("success" if r["level"] == "confirmed" and r["status"] == "confirmed"
                    else "warning" if r["status"] == "pending" or r["level"] in ("probable", "suspected", "pending_data")
                    else "neutral")
            t.setItem(i, 2, badge_item(level_short_zh(r["level"]), kind))
            t.setItem(i, 3, QTableWidgetItem(method_zh(r["method"])))
            fill_cell(t, i, 4, f"{r['score']:.2f}" if r["score"] is not None else "—",
                      right=True, secondary=r["score"] is None)
            fill_cell(t, i, 5, n_items, right=True)
            fill_cell(t, i, 6, item_status_zh(r["status"]))
        t.setSortingEnabled(True)

    def _show_match_detail(self, row: int, _column: int):
        item = self.match_table.item(row, 0)
        match_id = item.data(Qt.UserRole) if item else None
        if match_id is None:
            return
        match = self.conn.execute(
            "SELECT * FROM matches WHERE id=? AND project_id=?",
            (int(match_id), self.project.project_id),
        ).fetchone()
        if not match:
            self.match_detail_panel.setPlainText("未找到对应匹配组，可能已重新运行匹配。")
            return
        try:
            item_ids = [int(value) for value in json.loads(match["item_ids_json"] or "[]")]
        except (TypeError, ValueError, json.JSONDecodeError):
            item_ids = []
        lines = [
            f"匹配组 #{match['id']}：{parse_group_key(match['group_key'])}",
            f"匹配级别：{level_short_zh(match['level'])}　匹配方式：{method_zh(match['method'])}",
            f"置信度：{float(match['score']):.2f}" if match["score"] is not None else "置信度：待确认",
        ]
        lines[-1] += f"　状态：{item_status_zh(match['status'])}"
        if match["review_note"]:
            lines.append(f"人工判断原因：{match['review_note']}")
        if item_ids:
            placeholders = ",".join("?" for _ in item_ids)
            selected_rows = self.conn.execute(
                f"""SELECT li.id, li.code, li.name, li.unit, li.quantity, li.unit_price,
                           li.amount, sp.period_no, sp.direction
                    FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
                    WHERE li.id IN ({placeholders}) AND sp.project_id=?
                    ORDER BY sp.period_no, li.id""",
                (*item_ids, self.project.project_id),
            ).fetchall()
            # 匹配引擎按方向隔离保存候选组；审核详情仍提供真正的“对上 ↔
            # 对下”对照。以未带方向的编码/归一化名称为桥，重新取两侧行，
            # 不改变匹配结果或自动合并状态。
            key = matching._unscoped_group_key(match["group_key"])
            if key.startswith("code:"):
                key_kind, key_value = "code", key[5:]
                comparison_rows = self.conn.execute(
                    """SELECT li.id, li.code, li.name, li.unit, li.quantity, li.unit_price,
                              li.amount, sp.period_no, sp.direction
                       FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
                       WHERE sp.project_id=? AND li.code=?
                       ORDER BY CASE sp.direction WHEN 'upward' THEN 0 WHEN 'downward' THEN 1 ELSE 2 END,
                                sp.period_no, li.id""",
                    (self.project.project_id, key_value),
                ).fetchall()
            elif key.startswith("name:"):
                key_kind, key_value = "name", key[5:]
                comparison_rows = [
                    candidate for candidate in self.conn.execute(
                        """SELECT li.id, li.code, li.name, li.unit, li.quantity, li.unit_price,
                                  li.amount, sp.period_no, sp.direction
                           FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
                           WHERE sp.project_id=?
                           ORDER BY CASE sp.direction WHEN 'upward' THEN 0 WHEN 'downward' THEN 1 ELSE 2 END,
                                    sp.period_no, li.id""",
                        (self.project.project_id,),
                    ).fetchall()
                    if matching.normalize_name(candidate["name"]) == key_value
                ]
            else:
                key_kind, key_value = "", ""
                comparison_rows = selected_rows
            left_rows = [r for r in comparison_rows if (r["direction"] or "unknown") == "upward"]
            right_rows = [r for r in comparison_rows if (r["direction"] or "unknown") == "downward"]
            if not left_rows and not right_rows:
                # 兼容只有未标记方向的旧项目：不伪造对上/对下数据，显示原组。
                left_rows = [r for r in selected_rows if (r["direction"] or "unknown") != "downward"]
                right_rows = [r for r in selected_rows if (r["direction"] or "unknown") == "downward"]

            def values(rows, field: str) -> list[str]:
                seen: list[str] = []
                for current in rows:
                    value = current[field]
                    text = "待补资料" if value in (None, "") else str(value)
                    if text not in seen:
                        seen.append(text)
                return seen or ["暂无对应项"]

            def mark_name(value: str, other: str) -> str:
                if value in {"暂无对应项", "待补资料"} or other in {"暂无对应项", "待补资料"}:
                    return value
                if value == other:
                    return value
                matcher = SequenceMatcher(a=value, b=other, autojunk=False)
                out: list[str] = []
                for tag, start, end, _other_start, _other_end in matcher.get_opcodes():
                    segment = value[start:end]
                    out.append(segment if tag == "equal" else f"【差异：{segment}】")
                return "".join(out)

            def field_status(left: str, right: str, field: str) -> str:
                if left in {"暂无对应项", "待补资料"} or right in {"暂无对应项", "待补资料"}:
                    return "待补资料/缺一侧"
                if field == "name":
                    return "完全一致" if matching.normalize_name(left) == matching.normalize_name(right) else "名称存在差异"
                if field in {"quantity", "unit_price"}:
                    try:
                        return "完全一致" if abs(float(left) - float(right)) < 1e-9 else "数值存在差异"
                    except (TypeError, ValueError):
                        pass
                return "完全一致" if left == right else "存在差异"

            lines.append("左右对照（左侧对上结算 / 右侧对下结算）：")
            if key_kind:
                lines.append(f"对照依据：{('编码' if key_kind == 'code' else '名称')} {key_value}")
            left_values = {field: "；".join(values(left_rows, field)[:5]) for field in
                           ("code", "name", "unit", "quantity", "unit_price")}
            right_values = {field: "；".join(values(right_rows, field)[:5]) for field in
                            ("code", "name", "unit", "quantity", "unit_price")}
            lines.append("字段 | 对上结算 | 匹配程度 | 对下结算")
            lines.append("---|---|---|---")
            for field, label in (("code", "编码"), ("name", "名称"), ("unit", "单位"),
                                 ("quantity", "数量"), ("unit_price", "单价")):
                left = left_values[field]
                right = right_values[field]
                if field == "name":
                    left = mark_name(left, right)
                    right = mark_name(right, left_values[field])
                lines.append(f"{label} | {left} | {field_status(left_values[field], right_values[field], field)} | {right}")
            lines.append(
                f"置信度原因：{method_zh(match['method'])}；当前级别 {level_short_zh(match['level'])}；"
                f"{('已人工确认' if match['status'] == 'confirmed' else '尚未人工确认')}。"
            )
            if not left_rows or not right_rows:
                lines.append("提示：任一侧没有对应项时，不自动视为匹配通过，需人工逐项确认。")
        else:
            lines.append("该匹配组没有可展示的清单行。")
        self.match_detail_panel.setPlainText("\n".join(lines))

    def _confirm_match(self):
        row = self.match_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "匹配复核", "请先选择匹配组")
            return
        mid = int(self.match_table.item(row, 0).text())
        dlg = ReasonDialog("人工确认匹配", f"将匹配组 #{mid} 标记为人工已确认？", self)
        if dlg.exec() != QDialog.Accepted:
            return
        # 匹配对象的业务展示文本不是用户输入的别名，不能误写入别名库；
        # 如需沉淀别名，应在后续的专门字段中明确填写并记录依据。
        matching.confirm_match(self.conn, self.project.project_id, mid, "user", dlg.reason())
        self.refresh_matches()
        self.refresh_overview()
        self.refresh_export_status()

    def _batch_confirm_matches(self):
        """仅允许把规则完全匹配候选批量转为人工已确认。"""
        selected_rows = sorted({index.row() for index in self.match_table.selectionModel().selectedRows()})
        if not selected_rows:
            QMessageBox.information(self, "批量确认匹配", "请先选择一个或多个匹配组")
            return
        ids = [int(self.match_table.item(row, 0).text()) for row in selected_rows]
        placeholders = ",".join("?" for _ in ids)
        candidates = self.conn.execute(
            f"""SELECT id, level, status FROM matches
                WHERE project_id=? AND id IN ({placeholders})""",
            (self.project.project_id, *ids),
        ).fetchall()
        non_exact = [r["id"] for r in candidates if r["level"] != "confirmed" or r["status"] != "pending"]
        if non_exact:
            QMessageBox.warning(
                self, "批量确认匹配",
                "批量操作仅适用于仍待确认的完全匹配；非完全匹配必须人工逐项确认。",
            )
            return
        dlg = ReasonDialog(
            "批量确认完全匹配",
            f"将 {len(candidates)} 组规则完全匹配标记为人工已确认？请填写统一依据。",
            self,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        for match_row in candidates:
            matching.confirm_match(
                self.conn, self.project.project_id, int(match_row["id"]), "user", dlg.reason()
            )
        self.refresh_matches()
        self.refresh_overview()
        self.refresh_export_status()

    def _override_match(self):
        row = self.match_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "匹配复核", "请先选择匹配组")
            return
        mid = int(self.match_table.item(row, 0).text())
        dlg = ReasonDialog("修正匹配级别", f"修正匹配组 #{mid} 的置信度级别", self)
        if dlg.exec() != QDialog.Accepted:
            return
        levels_zh = list(LEVEL_ZH.values())
        level, ok = QInputDialog.getItem(self, "选择新级别", "级别", levels_zh, 2, False)
        if not ok:
            return
        choices = list(LEVEL_ZH.keys())
        new_level = choices[levels_zh.index(level)]
        matching.override_match(self.conn, self.project.project_id, mid, new_level, "user", dlg.reason())
        self.refresh_matches()
        self.refresh_overview()
        self.refresh_export_status()

    # ---------- 成果导出 ----------
    def _export_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        fm = "Finder" if sys.platform == "darwin" else "资源管理器"
        self.export_status_label = QLabel("")
        self.export_status_label.setWordWrap(True)
        self.export_status_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent;")
        v.addWidget(self.export_status_label)
        self.export_card_values: dict[str, dict[str, QLabel]] = {}
        excel_btn = QPushButton("导出 Excel 审核底稿")
        excel_btn.setObjectName("btnPrimary")
        excel_btn.clicked.connect(self._export_excel)
        doc_btn = QPushButton("导出 Word 摘要")
        doc_btn.clicked.connect(self._export_docx)
        all_btn = QPushButton("全部生成")
        all_btn.setObjectName("btnPrimary")
        all_btn.clicked.connect(self._export_all)
        open_dir_btn = QPushButton(f"在{fm}中显示导出目录")
        open_dir_btn.setObjectName("btnTertiary")
        open_dir_btn.clicked.connect(self._open_export_dir)

        cards = QHBoxLayout()
        cards.setSpacing(theme.SP_M)
        for key, title, purpose, content, button in (
            (
                "excel", "Excel 审核底稿", "适用：逐行复核、筛选、公式与证据回溯",
                "包含：封面、对上/对下累计、差异、异常、审核底稿、证据索引",
                excel_btn,
            ),
            (
                "docx", "Word 管理层摘要", "适用：管理层汇报与决策前阅读",
                "包含：审核范围、关键指标、Top 风险、待决策事项与限制",
                doc_btn,
            ),
        ):
            card = QGroupBox(title)
            cv = QVBoxLayout(card)
            purpose_label = QLabel(purpose)
            purpose_label.setWordWrap(True)
            purpose_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent;")
            content_label = QLabel(content)
            content_label.setWordWrap(True)
            content_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent;")
            generated_label = QLabel("最近生成：—")
            status_label = QLabel("文件状态：尚未生成")
            for label in (generated_label, status_label):
                label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent;")
            cv.addWidget(purpose_label)
            cv.addWidget(content_label)
            cv.addStretch(1)
            cv.addWidget(generated_label)
            cv.addWidget(status_label)
            cv.addWidget(button)
            cards.addWidget(card, 1)
            self.export_card_values[key] = {
                "generated": generated_label, "status": status_label,
            }

        block = QVBoxLayout()
        block.setSpacing(theme.SP_XS)
        block.addLayout(cards)
        block.addSpacing(theme.SP_M)
        drow2 = QHBoxLayout()
        drow2.addWidget(all_btn)
        drow2.addWidget(open_dir_btn)
        drow2.addStretch(1)
        block.addLayout(drow2)
        tip = QLabel(
            "缺失数据不自动补 0，标注「待补资料」；不可比数据不强行比较；"
            "全部结论附证据索引，可追溯至原始单元格；自动结果不等于已批准业务结论。")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent;")
        block.addWidget(tip)
        block.addStretch(1)
        v.addLayout(block)
        return w

    def refresh_export_status(self):
        pid = self.project.project_id
        availability = run_contract.current_results_available(self.conn, pid)
        if not availability["available"]:
            reason = availability.get("reason")
            suffix = f"（{reason}）" if reason else ""
            self.export_status_label.setText(
                f"数据库不可写，当前结果不可用{suffix}；不能登记或生成当前 Excel/Word 成果。"
            )
            export_dir = Path(self.project_dir) / "exports"
            for key, pattern in (
                ("excel", "CostGuard审核底稿_*.xlsx"),
                ("docx", "CostGuard管理层摘要_*.docx"),
            ):
                card = getattr(self, "export_card_values", {}).get(key)
                if not card:
                    continue
                files = sorted(export_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
                if files:
                    latest_path = files[-1]
                    stamp = datetime.fromtimestamp(
                        latest_path.stat().st_mtime
                    ).strftime("%Y-%m-%d %H:%M")
                    card["generated"].setText(f"最近生成：{stamp}")
                else:
                    card["generated"].setText("最近生成：—")
                card["status"].setText(
                    "文件状态：当前结果不可用（数据库不可写，旧文件不得视为 current）"
                )
            return
        anomaly_scope, anomaly_params = run_contract.current_scope(self.conn, pid, "a")
        match_scope, match_params = run_contract.current_scope(self.conn, pid, "m")
        check_scope, check_params = run_contract.current_scope(self.conn, pid, "cr")
        pending_sheets = settlement_io.pending_sheet_count(self.conn, pid)
        high = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM anomalies a WHERE a.project_id=? AND {anomaly_scope} "
            "AND a.severity='high' AND a.status IN ('open', 'deferred')",
            (pid, *anomaly_params),
        ).fetchone()["c"]
        pending_matches = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM matches m WHERE m.project_id=? AND {match_scope} "
            "AND m.status='pending'", (pid, *match_params),
        ).fetchone()["c"]
        insufficient = self.conn.execute(
            f"""SELECT COUNT(*) AS c FROM crosscheck_results cr
               WHERE cr.project_id=? AND {check_scope} AND cr.verification_level='insufficient'""",
            (pid, *check_params),
        ).fetchone()["c"]
        findings = self.conn.execute(
            f"""SELECT COUNT(*) AS c FROM crosscheck_results cr
               WHERE cr.project_id=? AND {check_scope} AND cr.verification_level='findings'""",
            (pid, *check_params),
        ).fetchone()["c"]
        period_count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM settlement_periods WHERE project_id=?", (pid,)
        ).fetchone()["c"]
        checked_count = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM crosscheck_results cr WHERE cr.project_id=? AND {check_scope}",
            (pid, *check_params),
        ).fetchone()["c"]
        range_unproven = self.conn.execute(
            f"""SELECT COALESCE(SUM(cr.range_unproven_sheets), 0) AS c
               FROM crosscheck_results cr WHERE cr.project_id=? AND {check_scope}""",
            (pid, *check_params),
        ).fetchone()["c"]
        deferred = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM anomalies a WHERE a.project_id=? AND {anomaly_scope} "
            "AND a.status='deferred'", (pid, *anomaly_params),
        ).fetchone()["c"]
        source_files = self.conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE project_id=?", (pid,)
        ).fetchone()["c"]
        issues = []
        if not source_files:
            issues.append("尚未导入资料")
        elif not period_count:
            issues.append("暂无可审核结算期次")
        if pending_sheets:
            issues.append(f"待确认工作表 {pending_sheets} 张")
        if high:
            issues.append(f"高风险未处理 {high} 项")
        if deferred:
            issues.append(f"暂不处理异常 {deferred} 项")
        if pending_matches:
            issues.append(f"待确认匹配 {pending_matches} 组")
        if insufficient:
            issues.append(f"校核不充分 {insufficient} 期")
        if findings:
            issues.append(f"校核有发现 {findings} 期")
        if period_count and checked_count < period_count:
            issues.append(f"尚未校核 {period_count - checked_count} 期")
        if range_unproven:
            issues.append(f"取数范围未证明 {range_unproven} 张工作表")
        if issues:
            self.export_status_label.setText(
                "审核尚未完成：" + "、".join(issues) + "。仍可生成成果，但成果中会保留待补资料和未完成标记。"
            )
        else:
            self.export_status_label.setText("审核完成度：当前未发现主要待处理事项；导出内容仍需按证据索引复核。")
        # 成果卡片显示最近生成时间和文件状态，避免用户只看到“导出”按钮而
        # 不知道是否已有可用成果。文件名沿用导出器前缀，不读取文件内容。
        export_dir = Path(self.project_dir) / "exports"
        registry_by_kind = {
            "excel": run_contract.export_status(self.conn, pid, "excel_workbook"),
            "docx": run_contract.export_status(self.conn, pid, "management_summary_docx"),
        }
        for key, pattern, kind in (("excel", "CostGuard审核底稿_*.xlsx", "excel_workbook"),
                                   ("docx", "CostGuard管理层摘要_*.docx", "management_summary_docx")):
            card = getattr(self, "export_card_values", {}).get(key)
            if not card:
                continue
            files = sorted(export_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
            if not files:
                card["generated"].setText("最近生成：—")
                card["status"].setText("文件状态：尚未生成")
                continue
            latest_path = files[-1]
            stamp = datetime.fromtimestamp(latest_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            registered = registry_by_kind[kind]
            if registered:
                item = registered[0]
                status_zh = {
                    "current": "可用", "stale": "已失效，请重新生成",
                    "missing": "文件缺失，请重新生成", "changed": "文件已变化，请重新生成",
                }.get(item["status"], "需复核")
                card["generated"].setText(f"最近生成：{stamp}")
                card["status"].setText(f"文件状态：{status_zh}（{Path(item['path']).name}）")
            else:
                card["generated"].setText(f"最近生成：{stamp}")
                card["status"].setText(f"文件状态：未登记，需重新生成（{latest_path.name}）")

    def _export_excel(self):
        try:
            path = excel_export.export_workbook(
                self.conn, self.project.project_id, Path(self.project_dir) / "exports")
        except run_contract.CurrentResultsUnavailableError as exc:
            QMessageBox.warning(self, "导出不可用", str(exc))
            return
        except Exception:  # noqa: BLE001 — 普通界面不显示技术异常
            QMessageBox.warning(self, "导出失败", "Excel 审核底稿生成失败，请检查导出目录权限后重试。")
            return
        self.refresh_export_status()
        platform_paths.reveal_in_file_manager(path)
        QMessageBox.information(self, "导出完成", f"已导出：\n{path}")

    def _export_docx(self):
        try:
            path = excel_export.export_management_summary_docx(
                self.conn, self.project.project_id, Path(self.project_dir) / "exports")
        except run_contract.CurrentResultsUnavailableError as exc:
            QMessageBox.warning(self, "导出不可用", str(exc))
            return
        except Exception:  # noqa: BLE001 — 普通界面不显示技术异常
            QMessageBox.warning(self, "导出失败", "Word 管理层摘要生成失败，请检查导出目录权限后重试。")
            return
        self.refresh_export_status()
        platform_paths.reveal_in_file_manager(path)
        QMessageBox.information(self, "导出完成", f"已导出：\n{path}")

    def _export_all(self):
        """按固定顺序生成两类成果；任一失败都保留另一类已生成文件。"""
        try:
            run_contract.require_current_results_available(
                self.conn, self.project.project_id, operation="全部成果导出"
            )
        except run_contract.CurrentResultsUnavailableError as exc:
            QMessageBox.warning(self, "导出不可用", str(exc))
            return
        excel_path = None
        docx_path = None
        failures = []
        try:
            excel_path = excel_export.export_workbook(
                self.conn, self.project.project_id, Path(self.project_dir) / "exports")
        except run_contract.CurrentResultsUnavailableError as exc:
            failures.append(f"Excel：{exc}")
        except Exception as exc:  # noqa: BLE001 — 保留失败信息并继续另一类导出
            failures.append(f"Excel：{exc}")
        try:
            docx_path = excel_export.export_management_summary_docx(
                self.conn, self.project.project_id, Path(self.project_dir) / "exports")
        except run_contract.CurrentResultsUnavailableError as exc:
            failures.append(f"Word：{exc}")
        except Exception as exc:  # noqa: BLE001 — 保留失败信息并继续另一类导出
            failures.append(f"Word：{exc}")
        self.refresh_export_status()
        created = [str(path) for path in (excel_path, docx_path) if path]
        if created:
            platform_paths.reveal_in_file_manager(Path(created[0]))
            suffix = "审核尚未完成" if "审核尚未完成" in self.export_status_label.text() else ""
            failure_text = "\n\n失败：\n" + "\n".join(failures) if failures else ""
            QMessageBox.information(
                self, "成果生成完成",
                "已生成：\n" + "\n".join(created)
                + (f"\n\n{suffix}" if suffix else "")
                + failure_text,
            )
        else:
            message = "暂未生成成果，请检查导出目录权限后重试。"
            if failures:
                message += "\n\n" + "\n".join(failures)
            QMessageBox.warning(self, "成果生成失败", message)

    def _open_export_dir(self):
        platform_paths.reveal_in_file_manager(Path(self.project_dir) / "exports")

    # ---------- 公共 ----------
    def refresh_all(self):
        self.status_label.setText(project_status_summary(self.conn, self.project.project_id))
        self.refresh_overview()
        self.refresh_periods()
        self.refresh_items()
        self.refresh_anomalies()
        self.refresh_matches()
        self.refresh_export_status()

    def _notify_import(self, ok: int, fail: list[str], pending: int = 0):
        msg = f"成功导入 {ok} 个文件（原文件未改动）。"
        if pending:
            msg += f"\n其中 {pending} 个工作表待人工确认（表头歧义/无表头/表单），可用「人工确认清单页…」处理。"
        if fail:
            msg += "\n失败：\n" + "\n".join(fail)
        QMessageBox.information(self, "导入", msg)
