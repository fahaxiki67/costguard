"""工程量核对独立对话框（quantity ledger UI）。

只负责显示与人工确认，不做额外计算：
- 期次/上下文列表：含尚未登记上下文的旧期次（一律 pending，不自动确认）；
- 批量登记必要上下文 / 明确替代（supersedes）/ 批量确认（原因必填）；
- 运行核量：同一份 ``build_quantity_ledger`` 快照展示跨方向控制、数量
  分项与同方向比较，选中行可查看逐行来源（文件/Sheet/行/原量/系数）。

业务期号显示：优先使用 settlement_io.business_period_numbers（costguard-next
合并后存在）；不存在时明确标注内部序号，不把内部唯一序号当业务第 N 期。
"""
from __future__ import annotations

import json
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
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from jiadun.core.engine import quantity_control as qc

_LOG = logging.getLogger(__name__)

STATUS_ZH = {
    "pending": "待确认",
    "confirmed": "已确认",
    "superseded": "已被替代",
}
KIND_ZH = {"contract": "合同", "settlement": "结算"}
MODE_ZH = {"incremental": "增量累计", "cumulative": "累计快照"}
DIRECTION_ZH = {"upward": "对上资料", "downward": "对下资料", "unknown": "未定"}
COMPARE_ZH = {
    "PASS": "通过",
    "FAIL": "超出（仅差额）",
    "PENDING": "待确认",
    "INCOMPARABLE": "不可比",
    "CONTROL_CONFLICT": "身份冲突",
}


def _zh(mapping: dict, value, default="") -> str:
    return mapping.get(str(value or ""), str(value or default))


class QuantityControlDialog(QDialog):
    """工程量核对：上下文登记/确认/替代 + 台账运行与来源查看。"""

    def __init__(self, conn: sqlite3.Connection, project_id: int, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.project_id = int(project_id)
        self._ledger: dict | None = None
        self.setWindowTitle("工程量核对（数量台账）")
        self.resize(1200, 720)

        layout = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Vertical, self)

        # ---- 上半：期次上下文管理 + 行口径确认（滚动区，小屏可用）----
        top_tabs = QTabWidget()
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.addWidget(QLabel(
            "期次与数量上下文（旧资料补登后仍为待确认；不同文件不得未经确认"
            "挤进同一业务期；替代关系需显式声明）"))
        # 表头与值一一对应（8 列）；期次 id 绑定行 metadata，不占列。
        self._context_headers = [
            "来源/期次", "方向", "合同键", "业务期号",
            "单位", "类型/模式", "工作口径/楼栋", "状态/替代",
        ]
        self.table = QTableWidget(0, len(self._context_headers))
        self.table.setHorizontalHeaderLabels(self._context_headers)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.cellClicked.connect(self._show_context_row)
        tv.addWidget(self.table, 1)

        form_w = QWidget()
        form = QFormLayout(form_w)
        self.direction_combo = QComboBox()
        for code, label in (("downward", "对下资料"), ("upward", "对上资料")):
            self.direction_combo.addItem(label, code)
        form.addRow("方向：", self.direction_combo)
        self.contract_edit = QLineEdit()
        self.contract_edit.setPlaceholderText("合同键（必填；同单位多合同靠它隔离）")
        form.addRow("合同键：", self.contract_edit)
        self.business_spin = QSpinBox()
        self.business_spin.setRange(1, 9999)
        form.addRow("业务期号：", self.business_spin)
        self.unit_edit = QLineEdit()
        self.unit_edit.setPlaceholderText("单位名称（分包/劳务单位）")
        form.addRow("单位名称：", self.unit_edit)
        self.kind_combo = QComboBox()
        for code, label in (("settlement", "结算"), ("contract", "合同")):
            self.kind_combo.addItem(label, code)
        form.addRow("资料类型：", self.kind_combo)
        self.mode_combo = QComboBox()
        for code, label in (("incremental", "增量累计"), ("cumulative", "累计快照")):
            self.mode_combo.addItem(label, code)
        form.addRow("计量模式：", self.mode_combo)
        self.scope_edit = QLineEdit()
        self.scope_edit.setPlaceholderText("工作计量口径（材料供应/浇筑劳务等；空口径不可比）")
        form.addRow("工作口径：", self.scope_edit)
        self.building_edit = QLineEdit()
        self.building_edit.setPlaceholderText("楼栋范围（如 1号楼；区间范围如 1-3号楼 不拆分到单栋）")
        form.addRow("楼栋范围：", self.building_edit)
        self.building_status_combo = QComboBox()
        self.building_status_combo.addItem("待确认", "pending")
        self.building_status_combo.addItem("已确认", "confirmed")
        form.addRow("楼栋确认：", self.building_status_combo)
        self.supersedes_combo = QComboBox()
        form.addRow("明确替代（修订版）：", self.supersedes_combo)
        btn_row = QHBoxLayout()
        self.create_btn = QPushButton("登记为新的独立期次")
        self.create_btn.clicked.connect(self._create_context)
        self.attach_btn = QPushButton("批量登记到所选期次")
        self.attach_btn.clicked.connect(self._attach_selected)
        self.confirm_btn = QPushButton("确认所选上下文")
        self.confirm_btn.setObjectName("btnPrimary")
        self.confirm_btn.clicked.connect(self._confirm_selected)
        for b in (self.create_btn, self.attach_btn, self.confirm_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        form.addRow(btn_row)
        reason_row = QHBoxLayout()
        reason_row.addWidget(QLabel("原因（必填，写入审计）"))
        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("登记/确认/替代的操作原因")
        reason_row.addWidget(self.reason_edit, 1)
        form.addRow(reason_row)
        tv.addWidget(form_w)
        top_tabs.addTab(top, "期次上下文（登记/确认/替代）")
        top_tabs.addTab(self._build_line_tab(), "行口径确认（标准键/楼栋/换算）")
        # 原生 QScrollArea 包住上下文区：小屏（720 高）可滚动使用，
        # 不抬高整窗最小高度；不另造布局系统。
        from PySide6.QtWidgets import QScrollArea

        top_scroll = QScrollArea()
        top_scroll.setWidgetResizable(True)
        top_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        top_scroll.setWidget(top_tabs)
        split.addWidget(top_scroll)

        # ---- 下半：运行结果 ----
        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        run_row = QHBoxLayout()
        self.run_btn = QPushButton("运行核量")
        self.run_btn.setObjectName("btnPrimary")
        self.run_btn.clicked.connect(self._run_ledger)
        self.run_summary = QLabel("尚未运行")
        run_row.addWidget(self.run_btn)
        run_row.addWidget(self.run_summary, 1)
        bv.addLayout(run_row)
        self.tabs = QTabWidget()
        self.controls_table = QTableWidget(0, 9)
        self.controls_table.setHorizontalHeaderLabels([
            "口径", "清单项", "楼栋", "对上合同基准", "对上已结算",
            "对下累计", "贡献（合同/单位）", "差额vs合同", "状态/原因",
        ])
        self.items_table = QTableWidget(0, 9)
        self.items_table.setHorizontalHeaderLabels([
            "方向", "类型", "合同", "单位", "口径", "清单项", "标准单位",
            "数量（全项目/楼栋）", "状态/原因",
        ])
        self.compare_table = QTableWidget(0, 9)
        self.compare_table.setHorizontalHeaderLabels([
            "方向", "合同", "单位", "口径", "清单项", "楼栋",
            "合同量", "已结算量", "状态/原因",
        ])
        for table in (self.controls_table, self.items_table, self.compare_table):
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.horizontalHeader().setSectionResizeMode(
                table.columnCount() - 1, QHeaderView.ResizeMode.Stretch)
            table.cellClicked.connect(
                lambda r, c, t=table: self._show_row_sources(r, c, t))
        self.tabs.addTab(self.controls_table, "跨方向控制（对上 vs 对下多合同）")
        self.tabs.addTab(self.items_table, "数量分项（多合同/单位）")
        self.tabs.addTab(self.compare_table, "同方向比较（合同 vs 已结算）")
        bv.addWidget(self.tabs, 2)
        self.detail_view = QTextEdit()
        self.detail_view.setReadOnly(True)
        self.detail_view.setPlaceholderText("选中上方任意行查看逐行来源（文件/Sheet/行/原量/系数/标准量）")
        bv.addWidget(self.detail_view, 1)
        split.addWidget(bottom)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 4)
        layout.addWidget(split)

        self._reload()

    # ---- 行口径确认页 ----

    def _build_line_tab(self) -> QWidget:
        tab = QWidget()
        lv = QVBoxLayout(tab)
        lv.addWidget(QLabel(
            "按真实明细登记行级上下文：显式标准键、楼栋（组合范围不摊分到单栋）、"
            "目标单位与人工换算系数。登记后仍为待确认，必须显式确认才参与计算；"
            "楼栋候选仅预填，不静默确认。"))
        self.lines_table = QTableWidget(0, 9)
        self.lines_table.setHorizontalHeaderLabels([
            "行#", "文件", "Sheet", "行号", "编码", "名称", "原单位",
            "原数量", "行上下文",
        ])
        self.lines_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.lines_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.lines_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.lines_table.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Stretch)
        self.lines_table.cellClicked.connect(self._prefill_building_candidates)
        lv.addWidget(self.lines_table, 2)
        form_w = QWidget()
        form = QFormLayout(form_w)
        self.line_key_edit = QLineEdit()
        self.line_key_edit.setPlaceholderText("显式标准清单键（同名异码/跨文件合并需人工键）")
        form.addRow("标准键：", self.line_key_edit)
        self.line_building_combo = QComboBox()
        self.line_building_combo.setEditable(True)
        self.line_building_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        form.addRow("楼栋：", self.line_building_combo)
        convert_row = QHBoxLayout()
        self.line_factor_edit = QLineEdit()
        self.line_factor_edit.setPlaceholderText("人工系数（如 0.5）")
        self.line_target_edit = QLineEdit()
        self.line_target_edit.setPlaceholderText("目标单位（如 kg）")
        self.line_basis_edit = QLineEdit()
        self.line_basis_edit.setPlaceholderText("换算依据（必填，如 1批=0.5kg 询价单）")
        for w in (self.line_factor_edit, self.line_target_edit, self.line_basis_edit):
            convert_row.addWidget(w)
        form.addRow("人工换算：", convert_row)
        self.line_scope_edit = QLineEdit()
        self.line_scope_edit.setPlaceholderText("行级工作口径（覆盖期次口径）")
        form.addRow("工作口径：", self.line_scope_edit)
        btn_row = QHBoxLayout()
        self.line_set_btn = QPushButton("登记所选行上下文")
        self.line_set_btn.clicked.connect(self._set_line_contexts)
        self.line_confirm_btn = QPushButton("确认所选行上下文")
        self.line_confirm_btn.setObjectName("btnPrimary")
        self.line_confirm_btn.clicked.connect(self._confirm_line_contexts)
        btn_row.addWidget(self.line_set_btn)
        btn_row.addWidget(self.line_confirm_btn)
        btn_row.addStretch(1)
        form.addRow(btn_row)
        self.line_reason_edit = QLineEdit()
        self.line_reason_edit.setPlaceholderText("原因（必填，写入审计）")
        form.addRow("原因：", self.line_reason_edit)
        lv.addWidget(form_w)
        self._load_lines()
        return tab

    def _load_lines(self) -> None:
        rows = self.conn.execute(
            """SELECT li.id, li.code, li.name, li.unit, li.quantity, li.flags_json,
                      li.sheet_id, sp.id AS period_id, sp.period_no,
                      qlc.status AS line_status, qlc.standard_key, qlc.building
               FROM line_items li
               JOIN settlement_periods sp ON sp.id=li.period_id
               LEFT JOIN quantity_line_context qlc ON qlc.line_item_id=li.id
               WHERE sp.project_id=? ORDER BY sp.period_no, li.id""",
            (self.project_id,),
        ).fetchall()
        meta: dict[int, dict] = {}
        for row in rows:
            if row["sheet_id"] is None:
                continue
            info = meta.get(int(row["sheet_id"]))
            if info is None:
                m = self.conn.execute(
                    """SELECT rs.sheet_name, sf.original_name FROM raw_sheets rs
                       JOIN parse_batches pb ON pb.id=rs.batch_id
                       JOIN source_files sf ON sf.id=pb.file_id WHERE rs.id=?""",
                    (int(row["sheet_id"]),),
                ).fetchone()
                info = meta[int(row["sheet_id"])] = (
                    dict(m) if m else {"sheet_name": None, "original_name": None})
        self._line_rows = []
        self.lines_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            flags = json.loads(row["flags_json"] or "{}")
            sheet_id = int(row["sheet_id"]) if row["sheet_id"] is not None else None
            info = (
                meta.get(sheet_id, {"sheet_name": None, "original_name": None})
                if sheet_id is not None
                else {"sheet_name": None, "original_name": None})
            status_text = (
                _zh(STATUS_ZH, row["line_status"]) if row["line_status"] else "未登记")
            if row["line_status"] and (row["standard_key"] or row["building"]):
                status_text += (
                    f"（键:{row['standard_key'] or '—'}｜楼栋:{row['building'] or '—'}）")
            values = [
                str(row["id"]), info.get("original_name") or "—",
                info.get("sheet_name") or "—",
                str(flags.get("row") or "—"),
                row["code"] or "—", row["name"] or "—",
                row["unit"] or "—", row["quantity"] if row["quantity"] is not None else "缺失",
                status_text,
            ]
            for c, text in enumerate(values):
                self.lines_table.setItem(r, c, QTableWidgetItem(str(text)))
            self._line_rows.append({"line_item_id": int(row["id"]),
                                    "period_id": int(row["period_id"])})

    def _selected_line_ids(self) -> list[int]:
        return [
            self._line_rows[row]["line_item_id"]
            for row in sorted(set(
                index.row() for index in self.lines_table.selectedIndexes()))
            if 0 <= row < len(self._line_rows)
        ]

    def _prefill_building_candidates(self, row: int, _column: int) -> None:
        """选中行时用楼栋候选预填（仅候选，不代替人工确认）。"""
        self.line_building_combo.clear()
        if not (0 <= row < len(self._line_rows)):
            return
        period_id = self._line_rows[row]["period_id"]
        try:
            candidates = qc.suggest_building_candidates(self.conn, self.project_id)
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("楼栋候选生成失败")
            candidates = []
        tokens = sorted({
            c["building"] for c in candidates
            if c["period_id"] == period_id and not c["conflict"]
        })
        conflicts = sorted({
            c["building"] for c in candidates
            if c["period_id"] == period_id and c["conflict"]
        })
        if conflicts:
            tokens = [t for t in tokens if t not in conflicts] + [
                f"{t}（候选冲突，需人工裁决）" for t in conflicts]
        if not tokens:
            self.line_building_combo.addItem("（无候选，可手工输入）")
        for token in tokens:
            self.line_building_combo.addItem(token)

    def _set_line_contexts(self) -> None:
        line_ids = self._selected_line_ids()
        if not line_ids:
            QMessageBox.information(self, "未选择", "请先在明细表中选择行。")
            return
        has_convert = bool(
            self.line_factor_edit.text().strip() or self.line_target_edit.text().strip())
        standard_key = self.line_key_edit.text().strip() or None
        building = self.line_building_combo.currentText().strip() or None
        if building and "（候选冲突" in building:
            building = building.split("（候选冲突")[0].strip()
        if building and building.startswith("（无候选"):
            building = None
        ok, failures = 0, []
        for line_id in line_ids:
            try:
                qc.set_line_context(
                    self.conn, self.project_id, line_id,
                    standard_key=standard_key,
                    building=building,
                    work_scope=self.line_scope_edit.text().strip() or None,
                    convert_factor=self.line_factor_edit.text().strip() or None
                    if has_convert else None,
                    target_unit=self.line_target_edit.text().strip() or None
                    if has_convert else None,
                    convert_basis=self.line_basis_edit.text().strip() or None
                    if has_convert else None,
                    actor="user",
                    reason=self.line_reason_edit.text().strip())
                ok += 1
            except ValueError as exc:
                failures.append(f"行 {line_id}：{exc}")
            except Exception:  # noqa: BLE001
                _LOG.exception("登记行上下文失败")
                failures.append(f"行 {line_id}：写入失败")
        self._load_lines()
        if failures:
            QMessageBox.warning(
                self, "部分失败",
                f"成功 {ok} 行；失败 {len(failures)} 行：\n" + "\n".join(failures[:8]))
        else:
            QMessageBox.information(
                self, "已登记（待确认）",
                f"{ok} 行已登记为待确认行上下文；请核对后显式确认。")

    def _confirm_line_contexts(self) -> None:
        line_ids = self._selected_line_ids()
        if not line_ids:
            QMessageBox.information(self, "未选择", "请先在明细表中选择行。")
            return
        ok, failures = 0, []
        for line_id in line_ids:
            try:
                qc.confirm_line_context(
                    self.conn, self.project_id, line_id,
                    actor="user", reason=self.line_reason_edit.text().strip())
                ok += 1
            except ValueError as exc:
                failures.append(f"行 {line_id}：{exc}")
            except Exception:  # noqa: BLE001
                _LOG.exception("确认行上下文失败")
                failures.append(f"行 {line_id}：写入失败")
        self._load_lines()
        if failures:
            QMessageBox.warning(
                self, "部分失败",
                f"成功 {ok} 行；失败 {len(failures)} 行：\n" + "\n".join(failures[:8]))
        else:
            self.line_reason_edit.clear()
            QMessageBox.information(self, "完成", f"已确认 {ok} 行的行上下文。")

    # ---- 数据装载 ----

    def _business_period_map(self) -> dict[int, int]:
        """来源业务期号映射（costguard-next 合并后可用；缺失则返回空）。"""
        from jiadun.core.engine import settlement_io

        getter = getattr(settlement_io, "business_period_numbers", None)
        if getter is None:
            return {}
        try:
            return {int(k): int(v) for k, v in getter(self.conn, self.project_id).items()}
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("读取业务期号映射失败")
            return {}

    def _reload(self) -> None:
        self._contexts = qc.list_period_contexts(self.conn, self.project_id)
        context_period_ids = {int(c["period_id"]) for c in self._contexts}
        business_map = self._business_period_map()
        periods = self.conn.execute(
            """SELECT id, period_no, title, direction, contract_party
               FROM settlement_periods WHERE project_id=? ORDER BY period_no""",
            (self.project_id,),
        ).fetchall()
        self._legacy_periods = [
            dict(row) for row in periods if int(row["id"]) not in context_period_ids
        ]
        rows = []
        for context in self._contexts:
            business = business_map.get(
                int(context["period_id"]), context["business_period_no"])
            rows.append((
                context,
                context["title"] or f"{context['contract_key']}独立期次",
                _zh(DIRECTION_ZH, context["direction"]),
                context["contract_key"],
                f"第{business}期",
                context["unit_name"] or "—",
                f"{_zh(KIND_ZH, context['doc_kind'])}·{_zh(MODE_ZH, context['amount_mode'])}",
                f"{context['work_scope'] or '（空口径）'}｜{context['building_scope'] or '楼栋待确认'}",
                _zh(STATUS_ZH, context["status"])
                + (f"（替代期次 {context['supersedes_period_id']}）"
                   if context["supersedes_period_id"] else ""),
            ))
        for period in self._legacy_periods:
            business = business_map.get(int(period["id"]))
            rows.append((
                None,
                period["title"] or f"期次{period['period_no']}",
                _zh(DIRECTION_ZH, period["direction"]),
                "—",
                f"第{business}期" if business else "（业务期号未登记）",
                period["contract_party"] or "—",
                "—",
                "—",
                "未登记上下文",
            ))
        self.table.setRowCount(len(rows))
        context_count = len(self._contexts)
        self._row_contexts: list[dict | None] = []
        self._row_period_ids: list[int | None] = []
        for r, row in enumerate(rows):
            context = row[0]
            self._row_contexts.append(context)
            if context is not None:
                period_id = int(context["period_id"])
            elif r >= context_count:
                period_id = int(self._legacy_periods[r - context_count]["id"])
            else:
                period_id = None
            self._row_period_ids.append(period_id)
            values = [str(text) for text in row[1:]]
            assert len(values) == len(self._context_headers), (
                "期次表头与值的列数必须一致")
            for c, text in enumerate(values):
                self.table.setItem(r, c, QTableWidgetItem(text))
        self.supersedes_combo.clear()
        self.supersedes_combo.addItem("（无）", None)
        for context in self._contexts:
            if context["status"] != PERIOD_SUPERSEDED_DISPLAY:
                self.supersedes_combo.addItem(
                    f"#{context['context_id']}（{_zh(DIRECTION_ZH, context['direction'])}"
                    f"·{context['contract_key']}·第{context['business_period_no']}期）",
                    int(context["period_id"]),
                )
        self._refresh_pending_hint()

    def _show_context_row(self, row: int, _column: int) -> None:
        """期次行点击：显示该行期次的登记/确认状态详情。"""
        if not (0 <= row < len(self._row_contexts)):
            return
        context = self._row_contexts[row]
        if context is None:
            period_id = self._row_period_ids[row] if row < len(self._row_period_ids) else None
            self.detail_view.setPlainText(
                f"期次 {period_id}：尚未登记数量上下文。"
                "请填写上方表单后选择本行执行「批量登记到所选期次」。")
            return
        self.detail_view.setPlainText(
            f"上下文 #{context['context_id']}｜期次 {context['period_id']}"
            f"（内部序号 {context['internal_period_no']}，业务第"
            f"{context['business_period_no']}期）｜"
            f"{_zh(DIRECTION_ZH, context['direction'])}·"
            f"{_zh(KIND_ZH, context['doc_kind'])}·{context['contract_key']}"
            f"·{context['unit_name'] or '单位未登记'}\n"
            f"状态：{_zh(STATUS_ZH, context['status'])}"
            + (f"，替代期次 {context['supersedes_period_id']}"
               if context["supersedes_period_id"] else "")
            + (f"\n确认依据：{context['confirmed_reason']}"
               if context["confirmed_reason"] else ""))

    def _refresh_pending_hint(self) -> None:
        pending = sum(1 for c in self._contexts if c["status"] == "pending")
        legacy = len(self._legacy_periods)
        self.run_summary.setText(
            f"待确认上下文 {pending} 个；未登记上下文的旧期次 {legacy} 个"
            "（台账只使用已确认上下文）")

    # ---- 操作 ----

    def _selected_contexts(self) -> list[dict]:
        result = []
        for row in sorted(set(index.row() for index in self.table.selectedIndexes())):
            context = self._row_contexts[row] if row < len(self._row_contexts) else None
            if context:
                result.append(context)
        return result

    def _selected_legacy_periods(self) -> list[dict]:
        result = []
        for row in sorted(set(index.row() for index in self.table.selectedIndexes())):
            context = self._row_contexts[row] if row < len(self._row_contexts) else None
            if context is None and row < len(self._row_period_ids) \
                    and self._row_period_ids[row] is not None:
                period_id = self._row_period_ids[row]
                period = next(
                    (p for p in self._legacy_periods
                     if int(p["id"]) == int(period_id)), None)
                if period:
                    result.append(period)
        return result

    def _form_values(self) -> dict:
        return {
            "direction": self.direction_combo.currentData(),
            "contract_key": self.contract_edit.text().strip(),
            "business_period_no": self.business_spin.value(),
            "unit_name": self.unit_edit.text().strip(),
            "doc_kind": self.kind_combo.currentData(),
            "amount_mode": self.mode_combo.currentData(),
            "work_scope": self.scope_edit.text().strip(),
            "building_scope": self.building_edit.text().strip(),
            "building_status": self.building_status_combo.currentData(),
            "supersedes_period_id": self.supersedes_combo.currentData(),
            "actor": "user",
            "reason": self.reason_edit.text().strip(),
        }

    def _create_context(self) -> None:
        values = self._form_values()
        try:
            created = qc.create_period_context(self.conn, self.project_id, **values)
        except ValueError as exc:
            QMessageBox.warning(self, "无法登记", str(exc))
            return
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("登记数量上下文失败")
            QMessageBox.critical(self, "写入失败", "上下文未能写入数据库，请重试。")
            return
        self.reason_edit.clear()
        self._reload()
        QMessageBox.information(
            self, "已登记（待确认）",
            f"已创建独立期次 {created['period_id']}（上下文 #{created['context_id']}，"
            "状态待确认；确认后才参与有效核量）。")

    def _attach_selected(self) -> None:
        periods = self._selected_legacy_periods()
        if not periods:
            QMessageBox.information(self, "未选择", "请先选择未登记上下文的期次行。")
            return
        values = self._form_values()
        supersedes = values.pop("supersedes_period_id")
        ok, failures = 0, []
        for period in periods:
            try:
                qc.attach_period_context(
                    self.conn, self.project_id, int(period["id"]),
                    direction=values["direction"],
                    contract_key=values["contract_key"],
                    business_period_no=values["business_period_no"],
                    unit_name=values["unit_name"],
                    doc_kind=values["doc_kind"],
                    amount_mode=values["amount_mode"],
                    work_scope=values["work_scope"],
                    building_scope=values["building_scope"],
                    building_status=values["building_status"],
                    supersedes_period_id=supersedes,
                    actor=values["actor"], reason=values["reason"])
                ok += 1
            except ValueError as exc:
                failures.append(f"期次 {period['id']}：{exc}")
            except Exception:  # noqa: BLE001
                _LOG.exception("批量登记数量上下文失败")
                failures.append(f"期次 {period['id']}：写入失败")
        self._reload()
        if failures:
            QMessageBox.warning(
                self, "部分失败",
                f"成功 {ok} 个；失败 {len(failures)} 个：\n" + "\n".join(failures[:8]))
        else:
            self.reason_edit.clear()
            QMessageBox.information(self, "完成", f"已为 {ok} 个期次登记待确认上下文。")

    def _confirm_selected(self) -> None:
        contexts = self._selected_contexts()
        if not contexts:
            QMessageBox.information(self, "未选择", "请先选择上下文行。")
            return
        reason = self.reason_edit.text().strip()
        ok, failures = 0, []
        for context in contexts:
            try:
                qc.confirm_period_context(
                    self.conn, self.project_id, context["context_id"],
                    actor="user", reason=reason)
                ok += 1
            except ValueError as exc:
                failures.append(f"上下文 #{context['context_id']}：{exc}")
            except Exception:  # noqa: BLE001
                _LOG.exception("确认数量上下文失败")
                failures.append(f"上下文 #{context['context_id']}：写入失败")
        self._reload()
        if failures:
            QMessageBox.warning(
                self, "部分失败",
                f"成功 {ok} 个；失败 {len(failures)} 个：\n" + "\n".join(failures[:8]))
        else:
            self.reason_edit.clear()
            QMessageBox.information(self, "完成", f"已确认 {ok} 个上下文。")

    def _run_ledger(self) -> None:
        from jiadun.core.contracts import run_contract

        # 运行前刷新运行契约：上下文/输入漂移时旧契约退出 current，本页
        # 显示的最新签名才是当前结果身份；不把旧签名冒作当前。
        previous = run_contract.get_current_contract(self.conn, self.project_id)
        try:
            run_contract.ensure_if_materialized(self.conn, self.project_id)
            self._ledger = qc.build_quantity_ledger(self.conn, self.project_id)
        except Exception:  # noqa: BLE001 — UI 层兜底
            _LOG.exception("工程量台账计算失败")
            QMessageBox.critical(self, "计算失败", "台账未能生成，请重试。")
            return
        current = run_contract.get_current_contract(self.conn, self.project_id)
        drift = bool(
            previous and current and previous.signature != current.signature)
        ledger = self._ledger
        counts = ledger.get("status_counts", {})
        signature_text = (
            f"｜运行签名 {current.signature[:12]}…" if current else "")
        drift_text = (
            "｜输入已变化：上一结果已作历史，以下为最新运行重算"
            if drift else "")
        self.run_summary.setText(
            f"分项 {counts.get('items', 0)}｜控制 {counts.get('quantity_controls', 0)}"
            f"（通过 {counts.get('controls_PASS', 0)}／超出 {counts.get('controls_FAIL', 0)}"
            f"／待确认 {counts.get('controls_PENDING', 0)}"
            f"／不可比 {counts.get('controls_INCOMPARABLE', 0)}）"
            f"｜待确认上下文 {len(ledger.get('pending_contexts', []))}"
            f"｜未登记期次 {len(ledger.get('unregistered_periods', []))}"
            + signature_text + drift_text)

        controls = ledger.get("quantity_controls", [])
        self.controls_table.setRowCount(len(controls))
        for r, ctl in enumerate(controls):
            base = ctl["baselines"]
            contributions = "；".join(
                f"{c['contract_key']}（{c['unit_name'] or '—'}）{c['quantity']}"
                for c in ctl["contributions"]) or "—"
            values = [
                ctl["work_scope"] or "（空口径）",
                ctl["display_name"] or ctl["code"],
                ctl["building"] or "全项目",
                base["upward_contract"]["quantity"] or "—",
                base["upward_settlement"]["quantity"] or "—",
                ctl["downstream_quantity"] if ctl["downstream_status"] == "ok" and not ctl["excluded_details"] else "待确认",
                contributions,
                ctl["delta_vs_contract"] or "—",
                f"{_zh(COMPARE_ZH, ctl['status'])}｜{ctl['reason']}",
            ]
            for c, text in enumerate(values):
                self.controls_table.setItem(r, c, QTableWidgetItem(str(text)))

        items = ledger.get("items", [])
        self.items_table.setRowCount(len(items))
        for r, item in enumerate(items):
            buildings = item["buildings"]
            building_text = "；".join(
                f"{'全项目' if b == '__all__' else b}:{v['quantity'] or '—'}"
                for b, v in buildings.items()) or "—"
            values = [
                _zh(DIRECTION_ZH, item["direction"]),
                _zh(KIND_ZH, item["doc_kind"]),
                item["contract_key"], item["unit_name"] or "—",
                item["work_scope"] or "（空口径）",
                item["display_name"] or item["code"],
                item["standard_unit"] or "—",
                building_text,
                f"{_zh(STATUS_ZH, item['status'])}｜{item['reason']}",
            ]
            for c, text in enumerate(values):
                self.items_table.setItem(r, c, QTableWidgetItem(str(text)))

        comparisons = ledger.get("comparisons", [])
        self.compare_table.setRowCount(len(comparisons))
        for r, cmp in enumerate(comparisons):
            values = [
                _zh(DIRECTION_ZH, cmp["direction"]),
                cmp["contract_key"], cmp["unit_name"] or "—",
                cmp["work_scope"] or "（空口径）",
                cmp["code"],
                cmp["building"] or "全项目",
                cmp["contract_quantity"] or "—",
                cmp["settlement_quantity"] or "—",
                f"{_zh(COMPARE_ZH, cmp['status'])}｜{cmp['reason']}",
            ]
            for c, text in enumerate(values):
                self.compare_table.setItem(r, c, QTableWidgetItem(str(text)))

    def _show_row_sources(self, row: int, column: int, table=None) -> None:
        widget = table if table is not None else self.sender()
        if not isinstance(widget, QTableWidget) or self._ledger is None:
            return
        if widget is self.items_table:
            entries = self._ledger.get("items", [])
            index = row
            sources = entries[index].get("sources", []) if 0 <= index < len(entries) else []
            title = entries[index].get("display_name", "") if 0 <= index < len(entries) else ""
        elif widget is self.controls_table:
            ctl = self._ledger.get("quantity_controls", [])[row]
            lines = []
            for item in self._ledger.get("items", []):
                if (item["work_scope"], tuple(item["identity"])) == (
                        ctl["work_scope"], tuple(ctl["identity"])):
                    lines.extend(self._format_sources(item))
            self.detail_view.setPlainText(
                f"控制组：{ctl['display_name'] or ctl['code']}｜{ctl['building'] or '全项目'}\n"
                + "\n".join(lines))
            return
        elif widget is self.compare_table:
            cmp = self._ledger.get("comparisons", [])[row]
            lines = []
            for item in self._ledger.get("items", []):
                if (item["direction"], item["contract_key"], item["unit_name"],
                        item["work_scope"], tuple(item["identity"])) == (
                        cmp["direction"], cmp["contract_key"], cmp["unit_name"],
                        cmp["work_scope"], tuple(cmp["identity"])):
                    lines.extend(self._format_sources(item))
            self.detail_view.setPlainText(
                f"比较：{cmp['code']}｜{cmp['building'] or '全项目'}\n" + "\n".join(lines))
            return
        else:
            return
        self.detail_view.setPlainText(
            f"清单项：{title}\n" + "\n".join(self._format_sources({"sources": sources})))

    @staticmethod
    def _format_sources(item: dict) -> list[str]:
        lines = []
        for source in item.get("sources", []):
            counted = "计入" if source.get("counted") else "未计入"
            note = source.get("note") or source.get("problem") or ""
            lines.append(
                f"行#{source['line_item_id']}｜业务第{source['business_period_no']}期"
                f"（内部序号{source['internal_period_no']}）｜"
                f"{source.get('file') or '—'}/Sheet「{source.get('sheet') or '—'}」"
                f"第{source.get('row') or '—'}行｜"
                f"原量 {source.get('original_quantity')}{source.get('original_unit') or ''}"
                f" × {source.get('factor') or '—'} = "
                f"{source.get('standard_quantity') or '待补'}"
                f"{source.get('target_unit') or ''}｜{counted}"
                + (f"｜{note}" if note else ""))
        return lines


PERIOD_SUPERSEDED_DISPLAY = "superseded"
