"""工作台页面（Phase 8）。

Tab 结构：期次概览 | 清单明细 | 审核问题中心 | 匹配复核 | 成果导出 | 版本与历史资产。
纪律落进 UI：
- 修改匹配级别 / 处理异常必须填写原因（原则 14）；
- 所有查询只读，绝不直接改业务数据；
- 期次方向标记（对上/对下）保存前要求确认。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
    QMenu,
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

from jiadun import branding
from jiadun.core import analysis, document_intake
from jiadun.core.anomalies import catalog as rule_catalog
from jiadun.core.anomalies import coverage as detection_coverage
from jiadun.core.anomalies import engine as anomaly_engine
from jiadun.core.contracts import run_contract
from jiadun.core.db import migrations
from jiadun.core.engine import settlement_io
from jiadun.core.evidence import audit as audit_log
from jiadun.core.evidence import finding_lifecycle
from jiadun.core.export import excel_export
from jiadun.core.matching import matching
from jiadun.core.matching import mirror as matching_mirror
from jiadun.core.models.source_file import import_file
from jiadun.core.pricing import history as pricing_history
from jiadun.core.reporting import build_report_model
from jiadun.core.versions import project as project_versions
from jiadun.platform import paths as platform_paths
from jiadun.ui import theme
from jiadun.ui.file_selection import (
    FileDropZone,
    classify_import_file,
    scan_import_paths,
)
from jiadun.ui.labels import (
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
from jiadun.ui.widgets import badge_item, fill_cell, make_data_table

# 展示层与数据层分离（复核项 #6 同源）：内部枚举原样保留在 DB/Tooltip，
# 本表只做 UI 显示转换；未知值使用安全中文兜底，原始枚举仅保留在 Tooltip。
SEVERITY_ZH = {"high": "高", "medium": "中", "low": "低", "info": "提示"}
SEVERITY_KIND = {"high": "danger", "medium": "warning", "low": "neutral", "info": "info"}

_LOG = logging.getLogger(__name__)


def _load_local_ocr_provider():
    """在工作线程中加载本机 OCR；不可用时保守返回 None。"""
    try:
        from jiadun.platform.ocr import get_default_ocr_provider

        return get_default_ocr_provider()
    except Exception as exc:  # noqa: BLE001 - OCR 不可用必须回到待处理
        _LOG.warning("本地 OCR 不可用，扫描 PDF 将保持待处理：%s", type(exc).__name__)
        return None


def _contract_document_status(conn, project_id: int, doc_id: int) -> str:
    """读取合同资料的最终状态，避免把 OCR 候选显示成普通成功。"""
    row = conn.execute(
        """SELECT COALESCE(di.parse_status, 'needs_review') AS parse_status
           FROM contract_docs cd
           LEFT JOIN document_intake di
             ON di.file_id=cd.file_id AND di.project_id=cd.project_id
           WHERE cd.id=? AND cd.project_id=?""",
        (int(doc_id), int(project_id)),
    ).fetchone()
    return str(row["parse_status"]) if row else "failed"


def _export_files(export_dir: Path, kind: str) -> list[Path]:
    """返回当前和历史品牌前缀下的导出文件，按修改时间排序。

    新生成成果统一使用「价盾」前缀；旧 v0.x 项目中的 ``CostGuard`` 成果
    仍是历史证据，不能因品牌迁移在界面上消失。这里仅扫描文件名，不改名、
    不覆盖，也不改变 Run Contract 对 current/stale 的判定。
    """
    suffix = "审核底稿_*.xlsx" if kind == "excel" else "管理层摘要_*.docx"
    prefixes = (
        branding.PRODUCT_DISPLAY_NAME,
        branding.PRODUCT_NAME,
        branding.LEGACY_PRODUCT_NAME,
    )
    files = {
        path
        for prefix in prefixes
        for path in export_dir.glob(f"{prefix}{suffix}")
        if path.is_file()
    }
    return sorted(files, key=lambda path: path.stat().st_mtime)


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
from jiadun.ui.dialogs.sheet_confirm import (  # noqa: E402
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


class ImportCategoryDialog(QDialog):
    """资料导入前的人工分类，不根据文件名猜测对上/对下。"""

    def __init__(self, selection_count: int, parent=None, *, category: str = "unclassified"):
        super().__init__(parent)
        self.setWindowTitle("选择资料类别")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        prompt = QLabel(
            f"已选择 {selection_count} 项资料（文件夹会在后台扫描）。请先确认资料类别；"
            "未能确认时请选择“待人工分类”。\n"
            "资料只会复制到项目的只读原件库；未分类、台账和报告不会自动进入金额计算。"
        )
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        form = QFormLayout()
        self.category_combo = QComboBox()
        for item in document_intake.DOCUMENT_CATEGORIES:
            self.category_combo.addItem(item.label, item.code)
        index = self.category_combo.findData(category)
        self.category_combo.setCurrentIndex(index if index >= 0 else 0)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        form.addRow("资料类别：", self.category_combo)
        form.addRow("处理方式：", self.detail)
        layout.addLayout(form)
        self.category_combo.currentIndexChanged.connect(self._refresh_detail)
        self._refresh_detail()
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        confirm = buttons.addButton("开始后台导入", QDialogButtonBox.AcceptRole)
        confirm.setObjectName("btnPrimary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _refresh_detail(self) -> None:
        item = document_intake.category_for(self.category_combo.currentData())
        strategy = {
            "contract": "提取合同文本；扫描 PDF 会标为 OCR 待处理，不能据此作业务结论。",
            "settlement": f"按{DIRECTION_ZH.get(item.direction, '未标记')}结算资料解析；异常结构进入人工确认。",
            "control_candidate": "登记为对上控制基准候选；不混入当前结算期次，须先人工确认范围、版本、税口径及变更关系。",
            "evidence_only": "仅登记、只读保存并保留 SHA-256，不自动解析为结算金额。",
        }[item.parse_strategy]
        self.detail.setText(f"{item.description or '待补充说明'}\n{strategy}")

    def category(self) -> str:
        return str(self.category_combo.currentData() or "unclassified")


class ImportWorker(QObject):
    """在独立 SQLite 连接中导入，避免 PDF/OCR 能力异常阻塞 Qt 主事件循环。"""

    progress = Signal(str)
    finished = Signal(object)

    def __init__(self, project_dir: str, project_id: int, files, category: str):
        super().__init__()
        self.project_dir = Path(project_dir)
        self.project_id = int(project_id)
        self.paths = tuple(Path(path) for path in files)
        self.category = document_intake.category_for(category).code

    def run(self) -> None:
        result = {
            "ok": 0, "partial": [], "fail": [], "pending": 0, "category": self.category,
            "skipped": (), "skipped_details": (),
        }
        conn = None
        try:
            conn = migrations.connect(self.project_dir / "project.db")
            spec = document_intake.category_for(self.category)
            self.progress.emit("正在后台扫描资料目录（不跟随符号链接）…")
            selection = scan_import_paths(self.paths)
            result["skipped"] = selection.skipped
            result["skipped_details"] = selection.skipped_reasons
            if not selection.files:
                self.progress.emit("未发现可导入资料；已结束后台任务。")
                self.finished.emit(result)
                return
            ocr_provider = None
            if spec.parse_strategy == "contract":
                self.progress.emit("正在准备本机 OCR；文件不会上传网络…")
                ocr_provider = _load_local_ocr_provider()
            total = len(selection.files)
            for ordinal, path in enumerate(selection.files, start=1):
                self.progress.emit(f"正在后台导入 {ordinal}/{total}：{path.name}")
                try:
                    if spec.parse_strategy == "settlement":
                        report = settlement_io.import_settlement_file(
                            conn, self.project_id, self.project_dir, path,
                            direction=spec.direction, document_category=spec.code,
                        )
                        result["pending"] += sum(
                            1 for sheet in report.sheets
                            if sheet.status in ("needs_role_review", "no_header", "non_settlement_form")
                            or sheet.state_code == "pending"
                        )
                        if report.status == "ok":
                            result["ok"] += 1
                        elif report.status == "partial":
                            result["partial"].append(
                                f"{path.name}：部分工作表已导入，其余待人工确认"
                                + (f"（{report.message}）" if report.message else "")
                            )
                        else:
                            result["fail"].append(
                                f"{path.name}：结算文件导入失败"
                                + (f"（{report.message}）" if report.message else "")
                            )
                    elif spec.parse_strategy == "contract":
                        from jiadun.core.contracts import extract as contract_extract

                        doc_id = contract_extract.import_contract(
                            conn, self.project_id, self.project_dir, path,
                            document_category=spec.code,
                            ocr_provider=ocr_provider,
                        )
                        risks = contract_extract.contract_risks(conn, self.project_id)
                        contract_extract.persist_risks(conn, self.project_id, risks)
                        status = _contract_document_status(conn, self.project_id, doc_id)
                        if status == "parsed":
                            result["ok"] += 1
                        elif status == "needs_review":
                            result["partial"].append(
                                f"{path.name}：OCR 已提取，需人工复核；候选条款未进入运行契约"
                            )
                        elif status == "pending_ocr":
                            result["partial"].append(
                                f"{path.name}：扫描页面待 OCR，尚未进入运行契约"
                            )
                        else:
                            result["fail"].append(
                                f"{path.name}：合同资料状态为 {status}，未作为成功导入"
                            )
                    else:
                        source = import_file(conn, self.project_id, self.project_dir, path)
                        intake_status = (
                            "registered" if spec.code == "unclassified" else
                            "control_candidate" if spec.parse_strategy == "control_candidate" else
                            "evidence_only"
                        )
                        document_intake.record_document(
                            conn, self.project_id, source.file_id, category=spec.code,
                            parse_status=intake_status,
                            detail=(
                                "等待人工选择资料类别；尚未进入结算或合同计算"
                                if spec.code == "unclassified" else
                                "已登记为对上控制基准候选；须人工确认范围、版本、税口径和变更关系后才能用于上限预警"
                                if intake_status == "control_candidate" else
                                "资料已登记，等待人工核阅"
                            ),
                            parser="",
                        )
                        result["ok"] += 1
                except NotImplementedError as exc:
                    # OCR/未实现能力不是解析成功，也不是“资料缺失”；资料中心会保留
                    # 可重试的明确状态，且主窗口继续响应。
                    result["partial"].append(f"{path.name}：OCR/解析待处理（{str(exc)[:180]}）")
                except Exception as exc:  # noqa: BLE001 - 返回可行动的文件级结果
                    _LOG.exception("后台资料导入失败 project_id=%s file=%s", self.project_id, path.name)
                    result["fail"].append(f"{path.name}：{str(exc).strip()[:240] or type(exc).__name__}；请检查后重试")
        except Exception as exc:  # noqa: BLE001 - 连接/项目级失败同样不能令 UI 无响应
            result["fail"].append(f"后台导入任务未启动：{type(exc).__name__}: {exc}")
        finally:
            if conn is not None:
                conn.close()
        self.finished.emit(result)


def _make_table(headers: list[str], **spec) -> QTableWidget:
    """统一表格工厂（theme/widgets）；spec 透传列宽/对齐策略。"""
    return make_data_table(headers, **spec)


def project_status_summary(conn, project_id: int) -> str:
    """工作台顶部状态信息：集中显示当前待处理事项和最近校核级别。"""
    summary = build_report_model(conn, project_id, read_only=True).project_summary
    if not summary.run_availability["available"]:
        # 运行级边界优先于所有历史统计；不要把 current_scope 的空集显示成
        # “尚未校核”，也不要让旧成功结果继续成为工作台的当前状态。
        return f"项目状态：{summary.statuses['project_status']} · 数据库不可写 · 当前结果不可用"
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
    verification_status = summary.verification["status"]
    if verification_status != "not_started":
        parts.append(f"最近校核：{summary.verification['period_status']}")
    elif period_count:
        parts.append("最近校核：尚未校核")
    if period_count and summary.detection_coverage["status"] != "complete":
        parts.append("异常检测覆盖率未完整")
    if period_count and summary.aggregate_coverage["status"] != "complete":
        parts.append("聚合验证覆盖率未完整")
    if summary.pending["manifest_status"] in {"incomplete", "mismatch"}:
        parts.append("权威批次清单未闭合")
    parts.insert(0, f"项目状态：{summary.statuses['project_status']}")
    return " · ".join(p for p in parts if p)


def _export_review_status_text(summary) -> str:
    """从共享项目摘要生成成果页状态，不在 UI 重新查询或推导结论。"""
    project_status = summary.statuses.get("project_status", "不可形成项目结论")
    project_status_code = summary.statuses.get(
        "project_status_code", "cannot_conclude"
    )
    period_count = sum(summary.directions.values())
    checked_count = int(summary.verification.get("periods_checked", 0) or 0)
    levels = summary.verification.get("levels", {})
    issues: list[str] = []
    if not summary.source_files:
        issues.append("尚未导入资料")
    elif not period_count:
        issues.append("暂无可审核结算期次")
    pending_sheets = int(summary.pending.get("sheets", 0) or 0)
    high = int(summary.pending.get("high_risk", 0) or 0)
    deferred = int(summary.risk.get("status", {}).get("deferred", 0) or 0)
    pending_matches = int(summary.pending.get("matches", 0) or 0)
    insufficient = int(levels.get("insufficient", 0) or 0)
    findings = int(levels.get("findings", 0) or 0)
    range_unproven = int(
        summary.verification.get("range_unproven_sheets", 0) or 0
    )
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
    if period_count and summary.detection_coverage.get("status") != "complete":
        issues.append("异常检测覆盖率未完整")
    if period_count and summary.aggregate_coverage.get("status") != "complete":
        issues.append("聚合验证覆盖率未完整")
    if summary.pending.get("manifest_status") in {"incomplete", "mismatch"}:
        issues.append("权威批次清单未闭合")
    if not summary.verification.get("evidence_complete", False):
        issues.append("当前 Evidence 不完整")
    if project_status_code != "can_conclude" and not issues:
        issues.append("共享证据闸门尚未闭合")
    if issues:
        return (
            f"项目状态：{project_status}；审核尚未完成："
            + "、".join(issues)
            + "。仍可生成成果，但成果中会保留待补资料和未完成标记。"
        )
    return (
        f"项目状态：{project_status}；审核完成度：当前未发现主要待处理事项；"
        "导出内容仍需按证据索引复核。"
    )


def _crosscheck_independence_text(result) -> str:
    """把 A/B 来源与物理行集事实合并成不易误读的业务文案。"""
    path_labels = {
        "source_independent_raw_scan": "已完成原始网格独立扫描",
        "unknown": "无法证明",
    }
    text = path_labels.get(
        getattr(result, "path_independence_level", None), "需复核"
    )
    warning = analysis.same_row_set_warning(
        getattr(result, "ab_row_set_status", None)
    )
    return f"{text}（{warning}）" if warning else text


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
        self.tabs.addTab(self._version_history_tab(), "版本与历史资产")
        self.source_files_tab_index = self.tabs.addTab(self._source_files_tab(), "资料中心")
        self.refresh_all()
        self._install_windows_shortcuts()

    # ---- Windows 键盘习惯 -------------------------------------------------
    def _install_windows_shortcuts(self) -> None:
        """Ctrl+F 搜索、Ctrl+A 全选、Ctrl+C 复制选中、Esc 清空/取消选择。

        表格是只读审计数据：不绑定 Delete 等删除类快捷键（防误删证据）；
        粘贴仅在搜索框等可编辑控件内使用 Qt 内置行为。焦点在可编辑控件上时
        复制/全选显式委托给该控件，保持编辑器默认习惯。
        """
        from PySide6.QtGui import QKeySequence, QShortcut
        from PySide6.QtWidgets import QApplication, QPlainTextEdit

        tab_tables: dict[int, QTableWidget | None] = {
            0: self.period_table,
            1: self.items_table,
            2: self.anomaly_table,
            3: self.match_table,
            4: None,
            5: self.version_chain_table,
        }

        def _focused_table() -> QTableWidget | None:
            w = QApplication.focusWidget()
            if isinstance(w, QTableWidget):
                return w
            return tab_tables.get(self.tabs.currentIndex())

        def _copy_selection() -> None:
            w = QApplication.focusWidget()
            if isinstance(w, (QLineEdit, QPlainTextEdit)):
                w.copy()
                return
            table = _focused_table()
            if table is None or not table.selectionModel().hasSelection():
                return
            rows = sorted({i.row() for i in table.selectedIndexes()})
            cols = sorted({i.column() for i in table.selectedIndexes()})
            lines = ["\t".join(
                table.item(r, c).text() if table.item(r, c) else ""
                for c in cols) for r in rows]
            QGuiApplication.clipboard().setText("\n".join(lines))

        def _select_all() -> None:
            w = QApplication.focusWidget()
            if isinstance(w, (QLineEdit, QPlainTextEdit)):
                w.selectAll()
                return
            table = _focused_table()
            if table is not None:
                table.selectAll()

        def _focus_search() -> None:
            w = QApplication.focusWidget()
            if isinstance(w, QLineEdit):
                w.setFocus()
                w.selectAll()
                return
            search = {1: self.items_search, 3: self.match_search}.get(
                self.tabs.currentIndex())
            if search is not None:
                search.setFocus()
                search.selectAll()

        def _escape() -> None:
            w = QApplication.focusWidget()
            if isinstance(w, QLineEdit) and w.text():
                w.clear()
                return
            table = _focused_table()
            if table is not None:
                table.clearSelection()

        def _bind(seq, slot) -> None:
            sc = QShortcut(seq, self)
            sc.activated.connect(slot)

        _bind(QKeySequence.Copy, _copy_selection)
        _bind(QKeySequence.SelectAll, _select_all)
        _bind(QKeySequence.Find, _focus_search)
        _bind(QKeySequence(Qt.Key_Escape), _escape)

    def _ensure_current_results_for_ui(
        self, operation: str, *, allow_unbound_read_only: bool = False
    ) -> bool:
        """UI 读写入口统一执行运行级不可用门控。"""
        try:
            run_contract.require_current_results_available(
                self.conn, self.project.project_id, operation=operation
            )
        except run_contract.CurrentResultsUnavailableError as exc:
            if allow_unbound_read_only:
                availability = run_contract.current_results_available(
                    self.conn, self.project.project_id, allow_state_clear=False
                )
                # 只放行未形成运行身份的人工 Finding/源证据只读查看。
                # 运行级侧车存在时仍保持完全 fail-closed，不能借详情页
                # 绕过恢复边界。
                if availability.get("state") is None:
                    return True
            QMessageBox.warning(self, operation, str(exc))
            return False
        return True

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
        self.overview_values: dict[str, QWidget] = {}
        for key, label in (
            ("files", "已导入文件"),
            ("pending_sheets", "待确认工作表"),
            ("upward", "对上结算期次"),
            ("downward", "对下结算期次"),
            ("high", "高风险未处理"),
            ("matches", "待确认匹配"),
            ("latest", "最近校核"),
            ("version", "当前版本"),
            ("historical_prices", "历史单价资产"),
        ):
            card = QWidget()
            cv = QVBoxLayout(card)
            cv.setContentsMargins(theme.SP_S, theme.SP_XS, theme.SP_S, theme.SP_XS)
            cv.setSpacing(0)
            caption = QLabel(label)
            caption.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent;")
            if key == "files":
                value = QPushButton("—")
                value.setFlat(True)
                value.setCursor(Qt.PointingHandCursor)
                value.setToolTip("查看已登记资料、类别、处理状态和只读副本")
                value.clicked.connect(self._show_source_files_tab)
            else:
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

    def _show_source_files_tab(self) -> None:
        self.tabs.setCurrentIndex(self.source_files_tab_index)
        self.refresh_documents()

    def refresh_overview(self):
        pid = self.project.project_id
        summary = build_report_model(self.conn, pid, read_only=True).project_summary
        files = summary.source_files
        period_counts = summary.directions
        pending_sheets = summary.pending["sheets"]
        high = summary.pending["high_risk"]
        matches = summary.pending["matches"]
        period_total = sum(period_counts.values())
        latest_text = summary.verification["period_status"] \
            if summary.verification["status"] != "not_started" else "尚未校核"
        latest_version = summary.version_chain.get("latest") or {}
        version_text = (
            f"v{latest_version['version_no']}"
            if latest_version else "未建立"
        )
        values = {
            "files": str(files), "pending_sheets": str(pending_sheets),
            "upward": str(period_counts.get("upward", 0)),
            "downward": str(period_counts.get("downward", 0)),
            "high": str(high), "matches": str(matches), "latest": latest_text,
            "version": version_text,
            "historical_prices": str(summary.historical_price_assets.get("active", 0)),
        }
        for key, value in values.items():
            # 统计卡的“已导入文件”是可点击的按钮，其他卡为 QLabel；二者都
            # 使用 Qt 的 setText 接口，避免只显示不可操作的数字。
            self.overview_values[key].setText(value)
        if not summary.run_availability["available"]:
            self._next_action = "crosscheck"
            suggestion = "数据库不可写，当前结果不可用；请修复写入问题后重新运行校核"
        elif summary.statuses["project_status_code"] == "cannot_conclude":
            self._next_action = "crosscheck" if period_total else "import"
            suggestion = (
                "当前不可形成项目结论：请先补齐资料并完成当前运行校核"
                if period_total else "当前不可形成项目结论：请先导入结算资料"
            )
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
        import_btn = QPushButton("选择结算文件…")
        import_btn.setObjectName("btnPrimary")
        import_btn.clicked.connect(self._import_files)
        folder_btn = QPushButton("导入资料文件夹…")
        folder_btn.clicked.connect(self._import_folder)
        contract_btn = QPushButton("导入合同/纪要…")
        contract_btn.clicked.connect(self._import_contract)
        files_btn = QPushButton("查看已导入资料…")
        files_btn.clicked.connect(self._show_source_files_tab)
        detect_btn = QPushButton("运行异常检测")
        detect_btn.clicked.connect(self._run_anomalies)
        check_btn = QPushButton("双向校核")
        check_btn.clicked.connect(self._run_crosscheck)
        confirm_btn = QPushButton("人工确认清单页…")
        confirm_btn.clicked.connect(self._open_sheet_confirm)
        review_btn = QPushButton("合同条款确认…")
        review_btn.clicked.connect(self._open_contract_review)
        baseline_btn = QPushButton("对上控制基准…")
        baseline_btn.clicked.connect(self._open_control_baseline)
        for b, name in ((import_btn, "btnPrimary"), (folder_btn, None), (contract_btn, None),
                        (files_btn, None), (confirm_btn, None), (review_btn, None),
                        (baseline_btn, None), (detect_btn, None), (check_btn, None)):
            if name:
                b.setObjectName(name)
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        v.addLayout(btn_row)
        self.import_progress_label = QLabel("")
        self.import_progress_label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; background: transparent;")
        self.import_progress_label.setWordWrap(True)
        v.addWidget(self.import_progress_label)
        self._import_action_buttons = (import_btn, folder_btn, contract_btn)
        self._import_thread: QThread | None = None
        self._import_worker: ImportWorker | None = None
        self.import_drop_zone = FileDropZone(
            "将资料文件、资料文件夹或打包资料拖到这里（支持递归导入与人工分类）"
        )
        self.import_drop_zone.paths_dropped.connect(self._choose_category_and_import)
        v.addWidget(self.import_drop_zone)
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
            self, "选择资料文件", "",
            "价盾可导入资料 (*.xlsx *.xlsm *.xls *.csv *.docx *.pdf *.txt)")
        if not files:
            return
        self._choose_category_and_import(files)

    def _import_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "选择要导入的资料文件夹", str(self.project_dir)
        )
        if folder:
            self._choose_category_and_import([folder])

    def _choose_category_and_import(self, paths, *, default_category: str = "unclassified") -> None:
        """扫描后由用户确认类别，再交给后台线程；不以文件名替用户定性。"""
        if self._import_thread is not None:
            QMessageBox.information(self, "资料导入", "已有资料正在后台导入，请等待当前任务完成。")
            return
        raw_paths = tuple(Path(path) for path in paths)
        if not raw_paths:
            return
        dialog = ImportCategoryDialog(len(raw_paths), self, category=default_category)
        if dialog.exec() != QDialog.Accepted:
            return
        self._start_background_import(raw_paths, dialog.category())

    def _start_background_import(self, files, category: str) -> None:
        """启动独立连接的导入线程。主线程只负责进度和完成后的只读刷新。"""
        self._import_thread = QThread(self)
        self._import_worker = ImportWorker(
            self.project_dir, self.project.project_id, files, category
        )
        self._import_worker.moveToThread(self._import_thread)
        self._import_thread.started.connect(self._import_worker.run)
        self._import_worker.progress.connect(self._on_import_progress)
        self._import_worker.finished.connect(self._on_background_import_finished)
        self._import_worker.finished.connect(self._import_thread.quit)
        self._import_thread.finished.connect(self._import_worker.deleteLater)
        self._import_thread.finished.connect(self._clear_import_worker)
        for button in self._import_action_buttons:
            button.setEnabled(False)
        self.import_progress_label.setText("正在后台扫描并导入资料；窗口仍可查看已有资料。")
        self._import_thread.start()

    def _on_import_progress(self, text: str) -> None:
        self.import_progress_label.setText(text)

    def _clear_import_worker(self) -> None:
        for button in self._import_action_buttons:
            button.setEnabled(True)
        self._import_worker = None
        self._import_thread = None

    def _on_background_import_finished(self, result: dict) -> None:
        self.import_progress_label.setText("后台导入已完成；已刷新资料中心。")
        self._notify_import(
            int(result["ok"]), list(result["fail"]), int(result["pending"]),
            skipped=len(result["skipped"]), partial=list(result["partial"]),
            skipped_details=result["skipped_details"],
        )
        self.refresh_all()
        if result["pending"]:
            ret = QMessageBox.question(
                self, "待人工确认",
                f"有 {result['pending']} 个工作表需要人工确认；相关结论不会自动显示为校核充分。\n"
                "现在打开「人工确认清单页」处理吗？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ret == QMessageBox.Yes:
                self._open_sheet_confirm()

    def import_paths(self, paths, *, skipped=(), skipped_reasons=()):
        """导入一组文件或文件夹中的资料，并按扩展名路由到对应解析器。"""
        selection = scan_import_paths(paths)
        skipped_paths = tuple(skipped) + selection.skipped
        skipped_details = tuple(skipped_reasons) + selection.skipped_reasons
        if not selection.files:
            detail = ""
            if skipped_details:
                detail = "\n未导入：" + "、".join(
                    f"{path.name}（{reason}）"
                    for path, reason in skipped_details[:8]
                )
            elif skipped_paths:
                detail = "\n未导入：" + "、".join(
                    path.name for path in skipped_paths[:8]
                )
            QMessageBox.warning(
                self,
                "没有可导入的资料",
                "所选内容中没有价盾支持的结算表或合同/纪要文件。"
                "\n支持：XLSX、XLSM、XLS、CSV、DOCX、PDF、TXT。" + detail,
            )
            return

        ok, partial, fail, pending = 0, [], [], 0
        for path in selection.files:
            kind = classify_import_file(path)
            try:
                if kind == "settlement":
                    report = settlement_io.import_settlement_file(
                        self.conn, self.project.project_id, self.project_dir, path,
                        document_category="unclassified",
                    )
                    pending += sum(
                        1
                        for sheet in report.sheets
                        if sheet.status
                        in ("needs_role_review", "no_header", "non_settlement_form")
                        or sheet.state_code == "pending"
                    )
                    if report.status == "ok":
                        ok += 1
                    elif report.status == "partial":
                        partial.append(
                            f"{path.name}：部分工作表已导入，其余待人工确认"
                            + (f"（{report.message}）" if report.message else "")
                        )
                    else:
                        fail.append(
                            f"{path.name}：结算文件导入失败"
                            + (f"（{report.message}）" if report.message else "")
                        )
                elif kind == "contract":
                    # 旧同步兼容入口没有资料类别上下文；合同/纪要只能先登记，
                    # 禁止把“未分类”误当成对上合同进入 Run Contract。
                    source = import_file(
                        self.conn, self.project.project_id, self.project_dir, path
                    )
                    document_intake.record_document(
                        self.conn,
                        self.project.project_id,
                        source.file_id,
                        category="unclassified",
                        parse_status="registered",
                        detail="合同/纪要资料已登记，待人工分类；未进入合同事实或运行契约。",
                        parser="",
                    )
                    partial.append(
                        f"{path.name}：合同/纪要已登记，待人工分类；未进入合同解析或运行契约"
                    )
                else:
                    # scan_import_paths 已过滤；保留显式分支防止未来扩展时静默成功。
                    raise ValueError("unsupported file type")
            except Exception as exc:  # noqa: BLE001 — UI 层兜底提示
                _LOG.exception(
                    "资料导入失败 project_id=%s path=%s",
                    self.project.project_id,
                    path,
                )
                detail = str(exc).strip()
                if detail:
                    # 保留核心层已经给出的可执行原因；截断长度避免异常文本
                    # 把导入结果对话框撑满。完整堆栈只写入日志，不阻塞重试。
                    detail = detail[:240]
                    fail.append(f"{path.name}：{detail}；请检查后重试")
                else:
                    fail.append(
                        f"{path.name}：导入失败，请检查文件格式、权限或数据完整性后重试"
                    )
        self._notify_import(
            ok,
            fail,
            pending,
            skipped=len(skipped_paths),
            partial=partial,
            skipped_details=skipped_details,
        )
        self.refresh_all()
        if pending:
            ret = QMessageBox.question(
                self,
                "待人工确认",
                f"有 {pending} 个工作表需要人工确认（可能涉及表头、取数范围、隐藏行列、"
                "公式/合并结构或表单角色）；相关结论不会自动显示为校核充分。\n"
                "现在打开「人工确认清单页」处理吗？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ret == QMessageBox.Yes:
                self._open_sheet_confirm()

    def _open_sheet_confirm(self):
        dlg = SheetConfirmDialog(self.conn, self.project.project_id, self)
        dlg.exec()
        self.refresh_all()

    def _open_contract_review(self):
        from jiadun.ui.dialogs.contract_review import ContractReviewDialog

        dlg = ContractReviewDialog(self.conn, self.project.project_id, self)
        dlg.exec()
        self.refresh_all()

    def _open_control_baseline(self) -> None:
        from jiadun.ui.dialogs.control_baseline import ControlBaselineDialog

        dlg = ControlBaselineDialog(self.conn, self.project.project_id, self)
        dlg.exec()
        self.refresh_all()

    def _open_page_review(self) -> None:
        from jiadun.ui.dialogs.page_review import PageReviewDialog

        record = self._selected_source_file()
        if record is None:
            return
        if str(record["parse_status"]) != "needs_review":
            QMessageBox.information(
                self, "逐页对照复核",
                "只有状态为“解析不完整，待人工确认”的资料需要逐页对照复核。"
            )
            return
        dlg = PageReviewDialog(
            self.conn, self.project.project_id, int(record["file_id"]),
            str(record["original_name"]), self,
        )
        dlg.exec()
        self.refresh_all()

    def _import_contract(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择合同/补充协议/纪要", "", "文档 (*.docx *.pdf *.txt)")
        if not files:
            return
        self._choose_category_and_import(files, default_category="upward_contract")

    def _run_anomalies(self):
        # 异常检测按钮也经过正式分析入口，避免从 UI 直接调用异常引擎而
        # 绕过 A/B/C 校核。入口会固定先校核、后检测，并以 fail-closed 结果
        # 回传阶段失败；这里仅展示检测数量和重试提示。
        analysis_result = analysis.run_analysis(
            self.conn, self.project.project_id
        )
        findings = analysis_result.anomaly_findings
        summary = anomaly_engine.anomaly_summary(findings)
        message = (
            f"检测完成：高 {summary['high']} / 中 {summary['medium']} / 低 {summary['low']}\n"
            "详见「审核问题中心」页。"
        )
        if analysis_result.errors:
            QMessageBox.warning(
                self,
                "正式分析未完成",
                message + "\n\n" + "\n".join(analysis_result.errors)
                + "\n请修复问题后重试。",
            )
        else:
            QMessageBox.information(self, "异常检测", message)
        self.refresh_anomalies()
        self.refresh_overview()
        self.refresh_export_status()

    def _run_crosscheck(self):
        # 项目级一次性协调所有方向，避免各方向分别形成 3/3 coverage 后
        # 永远无法满足项目级完整证明；正式分析入口固定先跑 A/B/C，再跑
        # 异常规则，两个阶段的失败统一回传为 fail-closed 结果。
        analysis_result = analysis.run_analysis(
            self.conn, self.project.project_id
        )
        results = analysis_result.crosscheck_results
        errors = list(analysis_result.errors)
        status_zh = {"match": "一致", "diff": "存在差异", "incomplete": "数据不完整"}
        level_zh = {"sufficient": "校核充分", "findings": "校核有发现",
                    "insufficient": "校核不充分"}
        proof_independence_zh = {
            "shared_extractor": "覆盖证明共用导入抽取器（覆盖证明本身非独立）",
            "unknown": "覆盖证明独立性无法证明",
        }
        dir_zh = DIRECTION_ZH
        lines = []
        for r in results:
            independence_text = _crosscheck_independence_text(r)
            line = (f"第{r.period_no}期{dir_zh.get(r.direction, '未标记')}："
                    f"{level_zh.get(r.verification_level, '待复核')}"
                    f"（A/B {status_zh.get(r.status, '待复核')}；A={r.path_a_total}，B={r.path_b_total}"
                    f"；参与明细 {r.detail_rows}，排除小计 {r.excluded_subtotal_rows}，"
                    f"排除标题/说明 {r.excluded_title_rows}，"
                    f"待确认工作表 {r.pending_sheets}，"
                    f"取数范围未证明 {r.range_unproven_sheets}；"
                    f"A/B路径 {independence_text}；"
                    f"{proof_independence_zh.get(r.ab_independence_level, '覆盖证明独立性需复核')}；"
                    f"一致性问题 {len(r.consistency_findings)}）")
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
        # 期次级结果不能压过项目级证据闸门。即使某一期显示“校核充分”，
        # 只要项目还有待确认 Sheet、未完成覆盖或其他阻断，弹窗必须明确
        # 这是局部结果，不构成项目结论，也不使用成功色语义。
        try:
            project_summary = build_report_model(
                self.conn, self.project.project_id, read_only=True
            ).project_summary
        except Exception as exc:  # noqa: BLE001 — UI 层保留失败原因并允许重试
            errors.append(f"report: 项目状态读取失败（{type(exc).__name__}），当前结果不可用")
            project_summary = None
        project_status_code = (
            project_summary.statuses.get("project_status_code", "cannot_conclude")
            if project_summary is not None else "cannot_conclude"
        )
        if project_status_code != "can_conclude":
            project_status = (
                project_summary.statuses.get("project_status", "不可形成项目结论")
                if project_summary is not None else "不可形成项目结论"
            )
            msg = (
                f"项目级状态：{project_status}。以下仅为期次/局部校核结果，"
                "不构成项目结论：\n" + msg
            )
        if any(r.verification_level == "insufficient" for r in results):
            msg = "⚠ 校核不充分：证据不足或仍有工作表待人工确认，不得视为通过：\n" + msg
        elif any(r.control_status == "diff" for r in results):
            msg = "⚠ 存在 C 控制差异（A/B 一致也不代表全部通过）：\n" + msg
        if errors:
            msg += "\n\n本次正式分析未能完整完成，以下阶段需重试：\n" + "\n".join(errors)
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

        # 方向更新、运行契约切换、旧结果失效、证据和审计由核心入口在同一
        # 事务内完成；UI 不再先提交方向再补写审计。
        try:
            settlement_io.set_project_direction(
                self.conn,
                self.project.project_id,
                int(period_id),
                direction,
                actor="user",
                reason=dlg.reason(),
            )
        except _sq.IntegrityError:
            # 目标方向同期号已有期次（v3 唯一约束）：友好拒绝，不做部分更新
            QMessageBox.warning(
                self, "标记方向",
                f"第 {pno} 期在「{dir_zh}」方向已存在期次，无法重复标记。\n"
                "如需合并，请先人工核清两期数据。")
            return
        except (ValueError, RuntimeError, run_contract.CurrentResultsUnavailableError) as exc:
            QMessageBox.warning(self, "标记方向", str(exc))
            return
        self.refresh_all()

    # ---------- 资料中心 ----------
    def _source_files_tab(self) -> QWidget:
        """显示所有登记资料，而不是把“已导入文件 N 个”做成不可点击计数。"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        notice = QLabel(
            "资料中心只显示已登记的原件身份与处理边界。类别由人工选择；系统不会仅凭"
            "文件名把资料断定为对上、对下或某一期结算。双击一行查看 SHA-256、路径和处理原因。"
        )
        notice.setWordWrap(True)
        notice.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent;")
        layout.addWidget(notice)
        self.document_table = _make_table(
            ["文件名", "资料类别", "处理状态", "方向", "类型", "大小", "SHA-256", "导入时间"],
            stretch_cols=(0, 1), fixed_widths={2: 100, 3: 64, 4: 56, 5: 82, 6: 142, 7: 142},
        )
        self.document_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.document_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.document_table.cellClicked.connect(self._show_source_file_detail)
        self.document_table.cellDoubleClicked.connect(self._show_source_file_detail)
        layout.addWidget(self.document_table, 1)
        row = QHBoxLayout()
        reclassify = QPushButton("重新分类…")
        reclassify.clicked.connect(self._reclassify_source_file)
        process = QPushButton("按类别重新处理")
        process.setObjectName("btnPrimary")
        process.clicked.connect(self._process_selected_source_file)
        page_review_btn = QPushButton("逐页对照复核…")
        page_review_btn.clicked.connect(self._open_page_review)
        original_folder = QPushButton("查看原件位置")
        original_folder.clicked.connect(lambda: self._reveal_selected_source_file("original_path"))
        copy_folder = QPushButton("查看只读副本")
        copy_folder.clicked.connect(lambda: self._reveal_selected_source_file("stored_path"))
        for button in (reclassify, process, page_review_btn, original_folder, copy_folder):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        self.document_detail = QTextEdit()
        self.document_detail.setReadOnly(True)
        self.document_detail.setPlaceholderText("选择资料后显示来源、SHA-256、类别、处理状态和可行动原因")
        self.document_detail.setMinimumHeight(155)
        layout.addWidget(self.document_detail)
        self._document_rows: dict[int, sqlite3.Row] = {}
        return widget

    @staticmethod
    def _document_status_text(status: str) -> str:
        return {
            "registered": "已登记，待分类/处理",
            "processing": "正在处理",
            "parsed": "已解析（仍需人工复核）",
            "needs_review": "解析不完整，待人工确认",
            "pending_ocr": "OCR 待处理（不能作结论）",
            "control_candidate": "控制基准候选，待确认",
            "evidence_only": "仅归档，待人工核阅",
            "failed": "处理失败，可查看原因",
        }.get(status, "状态待确认")

    def refresh_documents(self) -> None:
        if not hasattr(self, "document_table"):
            return
        rows = document_intake.list_documents(self.conn, self.project.project_id)
        self._document_rows = {int(row["file_id"]): row for row in rows}
        table = self.document_table
        table.setRowCount(len(rows))
        for index, record in enumerate(rows):
            category = document_intake.category_for(record["category"])
            status = str(record["parse_status"])
            table.setItem(index, 0, QTableWidgetItem(str(record["original_name"])))
            table.setItem(index, 1, QTableWidgetItem(category.label))
            status_kind = "danger" if status == "failed" else (
                "warning" if status in {"registered", "needs_review", "pending_ocr", "control_candidate"} else "info"
            )
            table.setItem(index, 2, badge_item(self._document_status_text(status), status_kind))
            table.setItem(index, 3, QTableWidgetItem(DIRECTION_ZH.get(record["direction"], "未标记")))
            table.setItem(index, 4, QTableWidgetItem(str(record["file_type"]).upper()))
            fill_cell(table, index, 5, f"{int(record['size_bytes']):,}", right=True)
            table.setItem(index, 6, QTableWidgetItem(str(record["sha256"])[:16] + "…"))
            table.setItem(index, 7, QTableWidgetItem(str(record["imported_at"])))
            table.item(index, 0).setData(Qt.UserRole, int(record["file_id"]))
        if not rows:
            self.document_detail.setPlainText("尚未登记资料。导入时请先选择资料类别；无法确认时选择“待人工分类”。")

    def _selected_source_file(self) -> sqlite3.Row | None:
        row = self.document_table.currentRow()
        if row < 0 or not self.document_table.item(row, 0):
            QMessageBox.information(self, "资料中心", "请先选择一份资料。")
            return None
        file_id = self.document_table.item(row, 0).data(Qt.UserRole)
        record = self._document_rows.get(int(file_id)) if file_id is not None else None
        if record is None:
            QMessageBox.warning(self, "资料中心", "资料记录已变化，请刷新后重试。")
        return record

    def _show_source_file_detail(self, *_args) -> None:
        record = self._selected_source_file()
        if record is None:
            return
        category = document_intake.category_for(record["category"])
        detail = str(record["detail"] or "无额外处理说明")
        page_detail: list[str] = []
        try:
            batch_stats = json.loads(record["parser_batch_stats_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            batch_stats = {}
        if isinstance(batch_stats, dict) and batch_stats.get("parser_version", "").startswith("pdf-"):
            page_count = batch_stats.get("page_count")
            counts = batch_stats.get("page_status_counts")
            if isinstance(page_count, int):
                page_detail.append(f"PDF 页数：{page_count}")
            if isinstance(counts, dict):
                labels = {
                    "native_text": "原生文本",
                    "ocr": "OCR",
                    "pending_ocr": "待 OCR",
                    "ocr_failed": "OCR 失败",
                    "needs_review": "待复核",
                }
                page_detail.append(
                    "页面状态："
                    + "、".join(
                        f"{labels.get(str(key), str(key))} {int(value)}"
                        for key, value in counts.items()
                        if isinstance(value, int) and value
                    )
                )
            ocr_meta = batch_stats.get("ocr_provider")
            if isinstance(ocr_meta, dict):
                model_id = ocr_meta.get("model_id")
                model_version = ocr_meta.get("model_version")
                if model_id or model_version:
                    page_detail.append(
                        f"OCR 模型：{model_id or '未记录'} / {model_version or '未记录'}"
                    )
            if batch_stats.get("error"):
                page_detail.append(f"页面处理边界：{batch_stats['error']}")
        if page_detail:
            detail += "\n" + "\n".join(page_detail)
        self.document_detail.setPlainText(
            f"文件：{record['original_name']}\n"
            f"资料类别：{category.label}（{record['classification_status']}）\n"
            f"处理状态：{self._document_status_text(str(record['parse_status']))}\n"
            f"方向：{DIRECTION_ZH.get(record['direction'], '未标记')}\n"
            f"原文件：{record['original_path']}\n"
            f"项目只读副本：{record['stored_path']}\n"
            f"SHA-256：{record['sha256']}\n"
            f"大小：{record['size_bytes']} bytes\n"
            f"处理器：{record['parser'] or '未使用'}\n"
            f"原因/边界：{detail}"
        )

    def _reveal_selected_source_file(self, field: str) -> None:
        record = self._selected_source_file()
        if record is None:
            return
        path = Path(str(record[field]))
        if not path.exists():
            QMessageBox.warning(self, "资料中心", "该文件路径当前不可访问；不会创建替代文件。")
            return
        platform_paths.reveal_in_file_manager(path)

    def _reclassify_source_file(self) -> None:
        record = self._selected_source_file()
        if record is None:
            return
        dialog = ImportCategoryDialog(1, self, category=str(record["category"]))
        dialog.setWindowTitle("重新分类资料")
        if dialog.exec() != QDialog.Accepted:
            return
        spec = document_intake.category_for(dialog.category())
        status = {
            "evidence_only": "evidence_only",
            "control_candidate": "control_candidate",
        }.get(spec.parse_strategy, "registered")
        previous_contract = run_contract.get_current_contract(
            self.conn, self.project.project_id
        )
        old_category = str(record["category"] or "unclassified")
        old_status = str(record["parse_status"] or "registered")
        reason = "资料重新分类；类别变化需重新确认解析范围和运行输入"
        with self.conn:
            document_intake.record_document(
                self.conn, self.project.project_id, int(record["file_id"]), category=spec.code,
                parse_status=status,
                detail=("分类已更新；请点击“按类别重新处理”后再进入对应解析。"
                        if status == "registered" else
                        "已重新分类为对上控制基准候选，尚未进入金额计算。" if status == "control_candidate" else
                        "资料已重新分类为仅归档资料，未进入金额计算。"),
                parser="", commit=False,
            )
            audit_id = audit_log.record_audit(
                self.conn,
                self.project.project_id,
                "user",
                "reclassify_source_file",
                f"source_file:{int(record['file_id'])}",
                {
                    "category": old_category,
                    "parse_status": old_status,
                    "run_id": previous_contract.run_id if previous_contract else None,
                },
                {"category": spec.code, "parse_status": status},
                reason,
                commit=False,
                run_id=previous_contract.run_id if previous_contract else None,
                run_signature=previous_contract.signature if previous_contract else None,
            )
            current_contract = run_contract.ensure_if_materialized(
                self.conn, self.project.project_id
            )
            if (
                previous_contract is not None
                and current_contract is not None
                and previous_contract.run_id != current_contract.run_id
            ):
                settlement_io._rebind_coverage_proofs_for_run(
                    self.conn,
                    self.project.project_id,
                    previous_contract,
                    current_contract,
                    reason=reason,
                )
            if current_contract is not None:
                self.conn.execute(
                    "UPDATE audit_log SET run_id=?, run_signature=? WHERE id=?",
                    (current_contract.run_id, current_contract.signature, audit_id),
                )
        self.refresh_all()
        QMessageBox.information(self, "资料中心", "资料类别已更新；原文件和只读副本未被修改。")

    def _process_selected_source_file(self) -> None:
        record = self._selected_source_file()
        if record is None:
            return
        spec = document_intake.category_for(record["category"])
        if spec.code == "unclassified":
            QMessageBox.information(self, "资料中心", "请先重新分类；待人工分类资料不会被自动解析或进入计算。")
            return
        copy_path = Path(str(record["stored_path"]))
        if not copy_path.is_file():
            QMessageBox.warning(self, "资料中心", "项目只读副本不可访问，已拒绝处理，原文件不会被替代。")
            return
        self._start_background_import([copy_path], spec.code)

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
        # 只读明细允许多选（Ctrl/Shift 点选、Ctrl+A 全选），便于整段复制核查
        self.items_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
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
            evidence_scope, evidence_params = run_contract.current_scope(
                self.conn, self.project.project_id, "e"
            )
            evidence = self.conn.execute(
                f"""SELECT summary, sources_json, scope, historical_reason
                    FROM evidence e
                    WHERE e.id=? AND e.project_id=? AND {evidence_scope}""",
                (evidence_id, self.project.project_id, *evidence_params),
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
        anomaly_scope, anomaly_params = run_contract.current_scope(
            self.conn, self.project.project_id, "a"
        )
        anomalies = self.conn.execute(
            f"""SELECT rule_id, message, status, evidence_id FROM anomalies a
               WHERE a.project_id=? AND a.subject_type='line_item' AND a.subject_id=?
                 AND {anomaly_scope} ORDER BY a.id""",
            (self.project.project_id, int(item_id), *anomaly_params),
        ).fetchall()
        if anomalies:
            lines.append("相关异常：")
            for anomaly in anomalies:
                lines.append(
                    f"- {rule_zh(anomaly['rule_id'])}（{item_status_zh(finding_lifecycle.lifecycle_status(anomaly))}）："
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
        rule_btn = QPushButton("规则目录…")
        rule_btn.setToolTip("查看规则适用范围与版本；启停变更会进入下一次 Run Contract")
        rule_btn.clicked.connect(self._edit_rule_configuration)
        process_btn = QPushButton("处理选中问题…")
        process_btn.setObjectName("btnPrimary")
        process_btn.clicked.connect(self._process_anomaly)
        row = QHBoxLayout()
        row.addWidget(refresh_btn)
        row.addWidget(rule_btn)
        row.addWidget(process_btn)
        row.addStretch(1)
        v.addLayout(row)
        self.anomaly_table = _make_table(
            ["编号", "方向", "级别", "规则", "说明", "证据ID", "状态"],
            stretch_cols=(4,), center_cols=(2,),
            fixed_widths={0: 56, 1: 56, 3: 150, 5: 90, 6: 64})
        self.anomaly_table.cellDoubleClicked.connect(self._show_anomaly_detail)
        self.anomaly_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.anomaly_table.customContextMenuRequested.connect(
            self._show_anomaly_context_menu
        )
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
                      a.lifecycle_status, a.repeat_history_json,
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
        pending_lifecycle = {"new", "pending_review", "confirmed_issue", "pending_data"}
        deferred_lifecycle = {"pending_review", "pending_data"}
        processed_lifecycle = {"legitimate_business", "rectified", "closed"}
        for row in rows:
            status_code = finding_lifecycle.lifecycle_status(row)
            if row["severity"] == "high":
                counts["high"] += 1
            elif row["severity"] == "medium":
                counts["medium"] += 1
            if status_code in pending_lifecycle:
                counts["pending"] += 1
            if status_code in deferred_lifecycle:
                counts["deferred"] += 1
            elif status_code in processed_lifecycle:
                counts["processed"] += 1
            elif status_code not in pending_lifecycle and status_code != "historical":
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
            fill_cell(t, i, 6, item_status_zh(finding_lifecycle.lifecycle_status(r)))
        t.setSortingEnabled(True)
        self._apply_match_filters()

    def _show_anomaly_context_menu(self, pos):
        item = self.anomaly_table.itemAt(pos)
        if item is None:
            return
        id_item = self.anomaly_table.item(item.row(), 0)
        anomaly_id = id_item.data(Qt.UserRole) if id_item else None
        if anomaly_id is None:
            return
        from jiadun.platform.spreadsheet_jump import (
            cell_ref,
            jump_target_for_anomaly,
            open_in_spreadsheet,
        )

        target = jump_target_for_anomaly(self.conn, int(anomaly_id))
        menu = QMenu(self)
        act_jump = menu.addAction("打开源文件并定位单元格")
        act_folder = menu.addAction("打开所在文件夹")
        act_copy = menu.addAction("复制单元格引用")
        if target is None:
            for action in (act_jump, act_folder, act_copy):
                action.setEnabled(False)
        chosen = menu.exec(self.anomaly_table.viewport().mapToGlobal(pos))
        if chosen is None or target is None:
            return
        if chosen is act_copy:
            QGuiApplication.clipboard().setText(cell_ref(target))
            return
        if chosen is act_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.file_path.parent)))
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            # TODO 异步化：COM 打开约 1-3 秒，暂同步执行
            outcome = open_in_spreadsheet(target)
        except FileNotFoundError:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "异常定位", "原始文件已移动或删除")
            return
        finally:
            QApplication.restoreOverrideCursor()
        text = {
            "located": f"已在表格程序中定位：{cell_ref(target)}",
            "opened_only": "已打开文件（未检测到可编程表格接口，无法自动定位单元格）",
            "hash_mismatch": "原始文件内容已变更，仅打开所在文件夹，请人工核对",
            "jump_failed": "自动定位失败，已尝试打开文件，请手动查找",
            "unsupported_platform": "当前平台不支持自动定位",
        }.get(outcome, "定位失败")
        QMessageBox.information(self, "异常定位", text)

    def _show_anomaly_detail(self, row: int, _column: int):
        item = self.anomaly_table.item(row, 0)
        anomaly_id = item.data(Qt.UserRole) if item else None
        if anomaly_id is None:
            return
        if not self._ensure_current_results_for_ui(
            "异常明细", allow_unbound_read_only=True
        ):
            self.anomaly_detail_panel.setPlainText("当前结果不可用，不能查看异常明细。")
            return
        scope, scope_params = run_contract.current_scope(
            self.conn, self.project.project_id, "a"
        )
        anomaly = self.conn.execute(
            f"""SELECT a.id, a.rule_id, a.severity, a.subject_type, a.subject_id, a.evidence_id,
                      message, status, lifecycle_status, resolved_note, created_at, finding_id, fingerprint,
                      confidence, detection_mode, raw_values_json, normalized_values_json,
                      impact, limitations_json, recommendation, suppression_reason, repeat_history_json
               FROM anomalies a WHERE a.id=? AND a.project_id=? AND {scope}""",
            (int(anomaly_id), self.project.project_id, *scope_params),
        ).fetchone()
        if not anomaly:
            self.anomaly_detail_panel.setPlainText("未找到对应问题，可能已重新运行检测。")
            return
        lines = [
            f"问题 #{anomaly['id']}：{rule_zh(anomaly['rule_id'])}",
            f"级别：{SEVERITY_ZH.get(anomaly['severity'], '其他')}　状态：{item_status_zh(finding_lifecycle.lifecycle_status(anomaly))}",
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
        repeated = finding_lifecycle.repeat_history(anomaly["repeat_history_json"])
        if repeated:
            lines.append("相同问题指纹历史处理（仅供参考，不自动关闭当前 Finding）：")
            for previous in repeated[-8:]:
                previous_status = finding_lifecycle.lifecycle_status(
                    previous.get("lifecycle_status") or previous.get("legacy_status")
                )
                lines.append(
                    f"- Finding #{previous.get('anomaly_id', '—')}："
                    f"{item_status_zh(previous_status)}；处理说明："
                    f"{normalize_business_text(previous.get('reason') or '暂无')}"
                )
        if anomaly["evidence_id"]:
            evidence_scope, evidence_params = run_contract.current_scope(
                self.conn, self.project.project_id, "e"
            )
            evidence = self.conn.execute(
                f"""SELECT e.summary, e.steps_json, e.sources_json FROM evidence e
                    WHERE e.id=? AND e.project_id=? AND {evidence_scope}""",
                (anomaly["evidence_id"], self.project.project_id, *evidence_params),
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

    def _edit_rule_configuration(self):
        """通过规则目录启停一条项目规则；每次变更均要求人工原因。"""
        configurations = rule_catalog.current_configurations(
            self.conn, self.project.project_id
        )
        definitions = rule_catalog.catalog_entries()
        labels = [
            f"{definition.name_zh}（{definition.rule_id}）· "
            f"{'启用' if configurations[definition.rule_id]['enabled'] else '已停用'}"
            for definition in definitions
        ]
        label, ok = QInputDialog.getItem(
            self, "规则目录", "选择要切换的规则（目录详情见提示）", labels, 0, False
        )
        if not ok:
            return
        selected = definitions[labels.index(label)]
        current = configurations[selected.rule_id]
        if not selected.allow_disable and current["enabled"]:
            QMessageBox.warning(self, "规则目录", "该规则是安全闸门，不允许停用。")
            return
        target_enabled = not current["enabled"]
        detail = (
            f"规则：{selected.name_zh}\n"
            f"场景：{selected.as_dict()['scenario_zh']}\n"
            f"严重度：{selected.severity}\n"
            f"触发条件：{selected.trigger_condition}\n"
            f"证据要求：{selected.evidence_requirements}\n"
            f"限制：{selected.limitations}\n"
            f"启停后将形成新的 Run Contract；旧结果不参与当前结论。"
        )
        QMessageBox.information(self, "规则目录", detail)
        reason_dlg = ReasonDialog(
            "调整规则配置",
            f"将「{selected.name_zh}」设置为{'启用' if target_enabled else '停用'}",
            self,
        )
        if reason_dlg.exec() != QDialog.Accepted:
            return
        try:
            rule_catalog.set_rule_enabled(
                self.conn,
                self.project.project_id,
                selected.rule_id,
                target_enabled,
                actor="user",
                reason=reason_dlg.reason(),
            )
        except (ValueError, rule_catalog.audit_log.AuditReasonRequiredError) as exc:
            QMessageBox.warning(self, "规则目录", str(exc))
            return
        QMessageBox.information(
            self,
            "规则目录",
            f"已将「{selected.name_zh}」设置为{'启用' if target_enabled else '停用'}。\n"
            "当前已有校核/检测结果需要按新运行契约重新执行。",
        )
        self.refresh_all()

    def _process_anomaly(self, forced_status: str | None = None):
        row = self.anomaly_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "处理异常", "请先选择异常")
            return
        aid = int(self.anomaly_table.item(row, 0).text())
        if not self._ensure_current_results_for_ui("处理异常"):
            return
        status_map = {
            "pending_review": "待复核",
            "confirmed_issue": "已确认问题",
            "legitimate_business": "合理业务情形",
            "pending_data": "待补资料",
            "rectified": "已整改",
            "closed": "已关闭",
        }
        new_status = finding_lifecycle.LEGACY_TO_LIFECYCLE.get(
            forced_status, forced_status
        ) if forced_status else None
        if new_status is None:
            labels = [status_map[key] for key in (
                "pending_review", "confirmed_issue", "legitimate_business",
                "pending_data", "rectified", "closed",
            )]
            label, ok = QInputDialog.getItem(self, "处理审核问题", "处理状态", labels, 0, False)
            if not ok:
                return
            new_status = next(key for key, value in status_map.items() if value == label)
        if new_status not in status_map:
            QMessageBox.warning(self, "处理异常", "不支持的 Finding 状态，请刷新后重试。")
            return
        dlg = ReasonDialog("处理异常", f"将异常 #{aid} 标记为“{status_map[new_status]}”", self)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            finding_lifecycle.update_finding_status(
                self.conn,
                self.project.project_id,
                aid,
                new_status,
                actor="user",
                reason=dlg.reason(),
            )
        except (run_contract.CurrentResultsUnavailableError,
                finding_lifecycle.FindingLifecycleError, ValueError) as exc:
            QMessageBox.warning(self, "处理异常", str(exc))
            return
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
        self.match_search = QLineEdit()
        self.match_search.setPlaceholderText("搜索编码、名称或匹配理由")
        self.match_search.setClearButtonEnabled(True)
        self.match_search.textChanged.connect(self._apply_match_filters)
        row.addWidget(self.match_search, 1)
        self.match_filter = QComboBox()
        self.match_filter.addItem("全部匹配", "all")
        self.match_filter.addItem("只看未匹配", "unmatched")
        self.match_filter.addItem("只看高金额差", "high_amount_diff")
        self.match_filter.addItem("只看单价差", "unit_price_diff")
        self.match_filter.addItem("只看数量差", "quantity_diff")
        self.match_filter.addItem("只看不可比", "incomparable")
        self.match_filter.addItem("只看待人工确认", "pending")
        self.match_filter.currentIndexChanged.connect(self._apply_match_filters)
        row.addWidget(self.match_filter)
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

    def _apply_match_filters(self):
        """只隐藏表格行，不删除当前运行的匹配候选。"""
        if not hasattr(self, "match_table"):
            return
        keyword = self.match_search.text().strip().casefold()
        filter_code = self.match_filter.currentData() or "all"
        for row in range(self.match_table.rowCount()):
            id_item = self.match_table.item(row, 0)
            if id_item is None:
                self.match_table.setRowHidden(row, True)
                continue
            match_id = id_item.data(Qt.UserRole)
            display_text = " ".join(
                (self.match_table.item(row, column).text() if self.match_table.item(row, column) else "")
                for column in (1, 2, 3, 6)
            ).casefold()
            visible = not keyword or keyword in display_text
            level_text = self.match_table.item(row, 2).text() if self.match_table.item(row, 2) else ""
            status_text = self.match_table.item(row, 6).text() if self.match_table.item(row, 6) else ""
            if filter_code in {"unmatched", "pending"}:
                visible = visible and status_text not in {"已确认", "已处理"}
            if filter_code == "unmatched":
                visible = visible and status_text != "已确认"
            elif filter_code == "pending":
                visible = visible and status_text == "待确认"
            elif filter_code == "incomparable":
                visible = visible and "不可比" in (level_text or status_text)
            elif filter_code in {"high_amount_diff", "unit_price_diff", "quantity_diff"}:
                try:
                    mirror = matching_mirror.build_mirror_comparison(
                        self.conn, self.project.project_id, int(match_id)
                    )
                except (ValueError, sqlite3.Error):
                    visible = False
                else:
                    if filter_code == "high_amount_diff":
                        visible = visible and mirror.amount_difference is not None and abs(mirror.amount_difference) >= 1000
                    elif filter_code == "unit_price_diff":
                        visible = visible and mirror.unit_price_difference not in (None, 0)
                    else:
                        visible = visible and mirror.quantity_difference not in (None, 0)
            self.match_table.setRowHidden(row, not visible)

    def _run_match(self):
        if not self._ensure_current_results_for_ui("运行匹配"):
            return
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
        if not self._ensure_current_results_for_ui("匹配明细"):
            self.match_detail_panel.setPlainText("当前结果不可用，不能查看匹配明细。")
            return
        scope, scope_params = run_contract.current_scope(
            self.conn, self.project.project_id, "m"
        )
        match = self.conn.execute(
            f"SELECT m.* FROM matches m WHERE m.id=? AND m.project_id=? AND {scope}",
            (int(match_id), self.project.project_id, *scope_params),
        ).fetchone()
        if not match:
            self.match_detail_panel.setPlainText("未找到对应匹配组，可能已重新运行匹配。")
            return
        mirror = None
        try:
            mirror = matching_mirror.build_mirror_comparison(
                self.conn, self.project.project_id, int(match_id)
            )
        except (ValueError, sqlite3.Error):
            # 旧项目/兼容候选缺少左右字段时保留下方旧详情路径；一旦核心
            # 镜像模型可用，数量、单价、金额差异均来自 Decimal 计算。
            mirror = None
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
        if mirror is not None:
            lines.append(
                "镜像复核范围：对上结算 ↔ 对下结算；"
                f"数量差 {mirror.quantity_difference if mirror.quantity_difference is not None else '待补资料'}；"
                f"单价差 {mirror.unit_price_difference if mirror.unit_price_difference is not None else '待补资料'}；"
                f"金额差 {mirror.amount_difference if mirror.amount_difference is not None else '待补资料'}；"
                f"差异率 {mirror.amount_difference_rate if mirror.amount_difference_rate is not None else '不可比'}"
            )
            lines.append(
                "镜像字段状态：" + "；".join(
                    f"{field.label}={field.status}" for field in mirror.fields
                )
            )
            lines.append(
                "相关 Evidence ID：" + (
                    "、".join(map(str, mirror.evidence_ids))
                    if mirror.evidence_ids else "待生成（来源行已在下方保留）"
                )
            )
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
                if field in {"quantity", "unit_price", "amount"}:
                    try:
                        from decimal import Decimal

                        return "完全一致" if Decimal(str(left)) == Decimal(str(right)) else "数值存在差异"
                    except (TypeError, ValueError, ArithmeticError):
                        pass
                return "完全一致" if left == right else "存在差异"

            lines.append("左右对照（左侧对上结算 / 右侧对下结算）：")
            if key_kind:
                lines.append(f"对照依据：{('编码' if key_kind == 'code' else '名称')} {key_value}")
            left_values = {field: "；".join(values(left_rows, field)[:5]) for field in
                           ("code", "name", "feature", "unit", "quantity", "unit_price", "amount")}
            right_values = {field: "；".join(values(right_rows, field)[:5]) for field in
                            ("code", "name", "feature", "unit", "quantity", "unit_price", "amount")}
            lines.append("字段 | 对上结算 | 匹配程度 | 对下结算")
            lines.append("---|---|---|---")
            for field, label in (("code", "编码"), ("name", "名称"), ("feature", "项目特征"),
                                 ("unit", "单位"), ("quantity", "数量"),
                                 ("unit_price", "单价"), ("amount", "金额")):
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
        if not self._ensure_current_results_for_ui("确认匹配"):
            return
        scope, scope_params = run_contract.current_scope(
            self.conn, self.project.project_id, "m"
        )
        current = self.conn.execute(
            f"SELECT id, status, run_signature FROM matches m WHERE m.id=? AND m.project_id=? AND {scope}",
            (mid, self.project.project_id, *scope_params),
        ).fetchone()
        if not current:
            QMessageBox.warning(self, "匹配复核", "匹配组已不在当前运行范围，请刷新后重试。")
            return
        dlg = ReasonDialog("人工确认匹配", f"将匹配组 #{mid} 标记为人工已确认？", self)
        if dlg.exec() != QDialog.Accepted:
            return
        if not self._ensure_current_results_for_ui("确认匹配"):
            return
        # 匹配对象的业务展示文本不是用户输入的别名，不能误写入别名库；
        # 如需沉淀别名，应在后续的专门字段中明确填写并记录依据。
        try:
            matching.confirm_match(self.conn, self.project.project_id, mid, "user", dlg.reason())
        except run_contract.CurrentResultsUnavailableError as exc:
            QMessageBox.warning(self, "确认匹配", str(exc))
            return
        self.refresh_matches()
        self.refresh_overview()
        self.refresh_export_status()

    def _batch_confirm_matches(self):
        """仅允许把规则完全匹配候选批量转为人工已确认。"""
        selected_rows = sorted({index.row() for index in self.match_table.selectionModel().selectedRows()})
        if not selected_rows:
            QMessageBox.information(self, "批量确认匹配", "请先选择一个或多个匹配组")
            return
        if not self._ensure_current_results_for_ui("批量确认匹配"):
            return
        ids = [int(self.match_table.item(row, 0).text()) for row in selected_rows]
        placeholders = ",".join("?" for _ in ids)
        scope, scope_params = run_contract.current_scope(
            self.conn, self.project.project_id, "m"
        )
        candidates = self.conn.execute(
            f"""SELECT m.id, m.level, m.status FROM matches m
                WHERE m.project_id=? AND m.id IN ({placeholders}) AND {scope}""",
            (self.project.project_id, *ids, *scope_params),
        ).fetchall()
        if len(candidates) != len(ids):
            QMessageBox.warning(self, "批量确认匹配", "所选匹配组已不在当前运行范围，请刷新后重试。")
            return
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
        if not self._ensure_current_results_for_ui("批量确认匹配"):
            return
        try:
            # 核心批量 API 会在对话框返回后重新读取每个 ID 的 level/status；
            # 因此此处的候选列表只用于对话框展示，不具备写入授权。
            matching.confirm_matches(
                self.conn,
                self.project.project_id,
                [int(match_row["id"]) for match_row in candidates],
                "user",
                dlg.reason(),
            )
        except (run_contract.CurrentResultsUnavailableError, ValueError) as exc:
            QMessageBox.warning(self, "批量确认匹配", str(exc))
            return
        self.refresh_matches()
        self.refresh_overview()
        self.refresh_export_status()

    def _override_match(self):
        row = self.match_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "匹配复核", "请先选择匹配组")
            return
        mid = int(self.match_table.item(row, 0).text())
        if not self._ensure_current_results_for_ui("修正匹配"):
            return
        scope, scope_params = run_contract.current_scope(
            self.conn, self.project.project_id, "m"
        )
        if not self.conn.execute(
            f"SELECT 1 FROM matches m WHERE m.id=? AND m.project_id=? AND {scope}",
            (mid, self.project.project_id, *scope_params),
        ).fetchone():
            QMessageBox.warning(self, "匹配复核", "匹配组已不在当前运行范围，请刷新后重试。")
            return
        dlg = ReasonDialog("修正匹配级别", f"修正匹配组 #{mid} 的置信度级别", self)
        if dlg.exec() != QDialog.Accepted:
            return
        levels_zh = list(LEVEL_ZH.values())
        level, ok = QInputDialog.getItem(self, "选择新级别", "级别", levels_zh, 2, False)
        if not ok:
            return
        choices = list(LEVEL_ZH.keys())
        new_level = choices[levels_zh.index(level)]
        if not self._ensure_current_results_for_ui("修正匹配"):
            return
        try:
            matching.override_match(
                self.conn, self.project.project_id, mid, new_level, "user", dlg.reason()
            )
        except run_contract.CurrentResultsUnavailableError as exc:
            QMessageBox.warning(self, "修正匹配", str(exc))
            return
        self.refresh_matches()
        self.refresh_overview()
        self.refresh_export_status()

    # ---------- 版本与历史资产 ----------
    def _version_history_tab(self) -> QWidget:
        """展示不可覆盖版本链与历史单价资产；所有内容均为只读回读。"""
        w = QWidget()
        v = QVBoxLayout(w)
        toolbar = QHBoxLayout()
        refresh_btn = QPushButton("刷新版本与资产")
        refresh_btn.clicked.connect(self.refresh_version_assets)
        create_btn = QPushButton("创建项目版本…")
        create_btn.setObjectName("btnPrimary")
        create_btn.clicked.connect(self._create_project_version_ui)
        collect_btn = QPushButton("关闭/沉淀历史单价…")
        collect_btn.clicked.connect(self._collect_historical_prices_ui)
        toolbar.addWidget(refresh_btn)
        toolbar.addWidget(create_btn)
        toolbar.addWidget(collect_btn)
        toolbar.addStretch(1)
        v.addLayout(toolbar)

        self.version_asset_status_label = QLabel("")
        self.version_asset_status_label.setWordWrap(True)
        self.version_asset_status_label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; background: transparent;"
        )
        v.addWidget(self.version_asset_status_label)
        self.version_compare_label = QLabel("")
        self.version_compare_label.setWordWrap(True)
        self.version_compare_label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; background: transparent;"
        )
        v.addWidget(self.version_compare_label)

        v.addWidget(QLabel("项目版本链（历史版本不覆盖，责任链无效时只作待复核线索）"))
        self.version_chain_table = _make_table(
            [
                "版本", "类型", "标题", "清单行数", "创建人", "创建时间",
                "当前运行", "Evidence ID", "Audit ID", "责任链",
            ],
            stretch_cols=(2,), right_cols=(0, 3, 7, 8),
            fixed_widths={0: 58, 1: 100, 3: 75, 6: 75, 7: 85, 8: 70},
        )
        v.addWidget(self.version_chain_table, 2)

        v.addWidget(QLabel("历史综合单价库（仅提示复核，不直接认定当前单价错误）"))
        self.historical_price_table = _make_table(
            [
                "状态", "规范项目名", "项目特征", "单位", "地区", "时间",
                "项目类型", "方向", "历史单价", "来源版本", "来源行", "Evidence ID", "Audit ID",
            ],
            stretch_cols=(1, 2), right_cols=(8, 9, 10, 11, 12),
            fixed_widths={0: 70, 3: 58, 7: 75, 8: 90, 9: 75, 10: 65, 11: 85, 12: 70},
        )
        v.addWidget(self.historical_price_table, 2)
        return w

    @staticmethod
    def _asset_status_label(status: str) -> str:
        return {
            "not_started": "未建立",
            "in_progress": "进行中",
            "final_approval_recorded": "已记录最终审定版本",
            "closed": "已关闭，可沉淀历史资产",
            "available": "有可用资产",
            "not_available": "暂无可用资产",
            "conditional": "有条件/待复核",
            "not_comparable": "不可直接比较",
        }.get(str(status), "待复核")

    def refresh_version_assets(self):
        """刷新版本链和历史单价，保留失效/撤销记录并显示证据状态。"""
        pid = self.project.project_id
        try:
            summary = build_report_model(self.conn, pid, read_only=True).project_summary
            versions = project_versions.list_project_versions(self.conn, pid)
            prices = pricing_history.list_historical_unit_prices(
                self.conn, source_project_id=pid, include_revoked=True
            )
        except Exception:  # noqa: BLE001 — UI 层只给可行动提示
            self.version_asset_status_label.setText(
                "版本或历史资产读取失败，请检查数据库状态后重试。"
            )
            self.version_chain_table.setRowCount(0)
            self.historical_price_table.setRowCount(0)
            self.version_compare_label.setText("")
            return

        chain = summary.version_chain or {}
        history = summary.historical_price_assets or {}
        closure = chain.get("closure") or {}
        closure_text = (
            f"已关闭（最终审定 v{closure.get('final_version_id')}）"
            if closure and closure.get("chain_valid") else "尚未形成有效关闭链"
        )
        self.version_asset_status_label.setText(
            f"版本链：{self._asset_status_label(chain.get('status', 'not_started'))}；"
            f"共 {chain.get('version_count', 0)} 个版本；{closure_text}。"
            f"历史单价：{self._asset_status_label(history.get('status', 'not_available'))}，"
            f"可用 {history.get('active', 0)} 条，已撤销 {history.get('revoked', 0)} 条；"
            "历史价格只用于提示复核，维度不齐时不可直接比较。"
        )
        self.version_chain_table.setRowCount(len(versions))
        current = run_contract.get_current_contract(self.conn, pid)
        for row_no, version in enumerate(versions):
            valid, reasons = project_versions._version_integrity(self.conn, version)
            current_run = bool(
                current
                and version.run_id == current.run_id
                and version.run_signature == current.signature
            )
            values = (
                f"v{version.version_no}",
                project_versions.VERSION_KINDS.get(version.version_kind, version.version_kind),
                version.title,
                str(version.item_count),
                version.created_by,
                version.created_at,
                "是" if current_run else "否",
                str(version.evidence_id) if version.evidence_id is not None else "待生成",
                str(version.audit_id) if version.audit_id is not None else "待生成",
                "有效" if valid else "待复核：" + ", ".join(reasons[:2]),
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col in {7, 8} and str(value).isdigit():
                    item.setData(Qt.UserRole, int(value))
                self.version_chain_table.setItem(row_no, col, item)

        if len(versions) >= 2:
            try:
                comparison = project_versions.compare_project_versions(
                    self.conn, pid, versions[-2].version_id, versions[-1].version_id
                )
                impact = (
                    str(comparison.confirmed_net_amount_impact)
                    if comparison.confirmed_net_amount_impact is not None else "无法确认"
                )
                self.version_compare_label.setText(
                    f"相邻版本 v{comparison.baseline.version_no} → v{comparison.current.version_no}："
                    f"{self._asset_status_label(comparison.status)}；"
                    f"差异 {len(comparison.items)} 项；已确认净金额影响 {impact} 元；"
                    f"Evidence ID {', '.join(map(str, comparison.evidence_ids)) or '待生成'}。"
                )
            except Exception:  # noqa: BLE001 — 保留链状态，比较异常只提示待复核
                self.version_compare_label.setText("相邻版本比较暂不可用，需检查版本 Evidence/快照完整性。")
        else:
            self.version_compare_label.setText("尚未形成两个可比较版本；新增、删除和字段变化将在下一版本建立后显示。")

        self.historical_price_table.setRowCount(len(prices))
        direction_labels = {"upward": "对上结算", "downward": "对下结算", "unknown": "未标记"}
        status_labels = {"active": "可用", "revoked": "已撤销"}
        for row_no, price in enumerate(prices):
            values = (
                status_labels.get(price.status, "待复核"),
                price.normalized_project_name,
                price.normalized_feature or "—",
                price.normalized_unit or "—",
                price.region or "待补资料",
                price.observed_at or "待补资料",
                price.project_type or "待补资料",
                direction_labels.get(price.direction, "未标记"),
                str(price.unit_price),
                f"v{price.source_version_id}",
                str(price.source_row) if price.source_row is not None else "待补资料",
                str(price.evidence_id),
                str(price.audit_id),
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col in {8, 9, 10, 11, 12}:
                    item.setData(Qt.UserRole, value)
                self.historical_price_table.setItem(row_no, col, item)

    def _create_project_version_ui(self):
        """通过人工输入创建不可覆盖版本；原因、操作人进入 Evidence/Audit。"""
        kinds = list(project_versions.VERSION_KINDS.items())
        labels = [label for _kind, label in kinds]
        kind_label, ok = QInputDialog.getItem(self, "创建项目版本", "版本类型：", labels, 0, False)
        if not ok:
            return
        version_kind = next(kind for kind, label in kinds if label == kind_label)
        title, ok = QInputDialog.getText(self, "创建项目版本", "版本标题：")
        if not ok or not title.strip():
            return
        actor, ok = QInputDialog.getText(self, "创建项目版本", "操作人：")
        if not ok or not actor.strip():
            return
        dlg = ReasonDialog("创建项目版本", f"将当前清单冻结为「{kind_label}」版本，请填写原因。", self)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            version = project_versions.create_project_version(
                self.conn,
                self.project.project_id,
                version_kind,
                title.strip(),
                created_by=actor.strip(),
                reason=dlg.reason(),
            )
        except Exception as exc:  # noqa: BLE001 — 核心异常不在 UI 中展开堆栈
            QMessageBox.warning(self, "创建版本失败", str(exc))
            return
        self.refresh_all()
        QMessageBox.information(
            self, "版本已创建",
            f"已创建 v{version.version_no}「{version.title}」。旧版本仍保留，详情请回查 Evidence ID {version.evidence_id}。",
        )

    def _collect_historical_prices_ui(self):
        """关闭项目并沉淀历史单价，缺失单价由核心 API 返回待补资料。"""
        pid = self.project.project_id
        closure = pricing_history.get_project_closure(self.conn, pid)
        actor, ok = QInputDialog.getText(self, "沉淀历史单价", "操作人：")
        if not ok or not actor.strip():
            return
        dlg = ReasonDialog("沉淀历史单价", "将从已关闭的最终审定版本复制单价，请填写原因。", self)
        if dlg.exec() != QDialog.Accepted:
            return
        reason = dlg.reason()
        try:
            if closure is None:
                final_versions = [
                    item for item in project_versions.list_project_versions(self.conn, pid)
                    if item.version_kind == "final_approval"
                ]
                if not final_versions:
                    raise pricing_history.HistoricalPriceError(
                        "尚未建立最终审定版本，不能关闭项目"
                    )
                region, ok = QInputDialog.getText(self, "沉淀历史单价", "地区（可留空，缺失时不可直接比较）：")
                if not ok:
                    return
                project_type, ok = QInputDialog.getText(self, "沉淀历史单价", "项目类型（可留空）：")
                if not ok:
                    return
                observed_at, ok = QInputDialog.getText(self, "沉淀历史单价", "观察日期（可留空）：")
                if not ok:
                    return
                closure = pricing_history.close_project_for_history(
                    self.conn,
                    pid,
                    final_versions[-1].version_id,
                    closed_by=actor.strip(),
                    reason=reason,
                    region=region.strip(),
                    project_type=project_type.strip(),
                    observed_at=observed_at.strip() or None,
                )
            collection = pricing_history.collect_historical_prices(
                self.conn,
                closure.closure_id,
                created_by=actor.strip(),
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 — 核心异常保留为可行动提示
            QMessageBox.warning(self, "历史单价沉淀失败", str(exc))
            return
        self.refresh_all()
        QMessageBox.information(
            self,
            "历史单价沉淀完成",
            f"已沉淀 {len(collection.records)} 条历史单价；待补资料 {len(collection.pending_items)} 条。"
            "历史价格仅用于提示复核，不直接认定当前单价错误。",
        )

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
        summary = build_report_model(self.conn, pid, read_only=True).project_summary
        availability = summary.run_availability
        if not availability["available"]:
            reason = availability.get("reason")
            suffix = f"（{reason}）" if reason else ""
            self.export_status_label.setText(
                f"数据库不可写，当前结果不可用{suffix}；不能登记或生成当前 Excel/Word 成果。"
            )
            export_dir = Path(self.project_dir) / "exports"
            for key, kind in (("excel", "excel"), ("docx", "docx")):
                card = getattr(self, "export_card_values", {}).get(key)
                if not card:
                    continue
                files = _export_files(export_dir, kind)
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
        self.export_status_label.setText(_export_review_status_text(summary))
        # 成果卡片显示最近生成时间和文件状态，避免用户只看到“导出”按钮而
        # 不知道是否已有可用成果。文件名沿用导出器前缀，不读取文件内容。
        export_dir = Path(self.project_dir) / "exports"
        registry_by_kind = {
            "excel": run_contract.export_status(self.conn, pid, "excel_workbook"),
            "docx": run_contract.export_status(self.conn, pid, "management_summary_docx"),
        }
        for key, file_kind in (
            ("excel", "excel"),
            ("docx", "docx"),
        ):
            card = getattr(self, "export_card_values", {}).get(key)
            if not card:
                continue
            files = _export_files(export_dir, file_kind)
            if not files:
                card["generated"].setText("最近生成：—")
                card["status"].setText("文件状态：尚未生成")
                continue
            latest_path = files[-1]
            stamp = datetime.fromtimestamp(latest_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            registered = registry_by_kind[file_kind]
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
        self.refresh_documents()
        self.refresh_periods()
        self.refresh_items()
        self.refresh_anomalies()
        self.refresh_matches()
        self.refresh_export_status()
        self.refresh_version_assets()

    def _notify_import(
        self,
        ok: int,
        fail: list[str],
        pending: int = 0,
        skipped: int = 0,
        *,
        partial: list[str] | None = None,
        skipped_details=(),
    ):
        partial = partial or []
        outcome: list[str] = []
        if ok:
            outcome.append(f"成功导入 {ok} 个文件")
        if partial:
            outcome.append(f"部分导入 {len(partial)} 个文件")
        if fail:
            outcome.append(f"失败 {len(fail)} 个文件")
        msg = "；".join(outcome) if outcome else "没有文件完成导入"
        if ok or partial:
            msg += "（原文件未改动）。"
        else:
            msg += "。"
        if skipped_details:
            msg += "\n未导入：\n" + "\n".join(
                f"{path.name}（{reason}）" for path, reason in skipped_details[:8]
            )
            if len(skipped_details) > 8:
                msg += f"\n等 {len(skipped_details)} 项"
        elif skipped:
            msg += f"\n已跳过 {skipped} 个不支持的文件。"
        if pending:
            msg += f"\n其中 {pending} 个工作表待人工确认（表头歧义/无表头/表单），可用「人工确认清单页…」处理。"
        if partial:
            msg += "\n部分导入：\n" + "\n".join(partial)
        if fail:
            msg += "\n失败：\n" + "\n".join(fail)
        QMessageBox.information(self, "导入", msg)
