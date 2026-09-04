"""人工确认被门控工作表（SheetConfirmDialog）——视觉分组重构版。

业务规则不变（相对重构前逐条保持）：
- 待确认列表来自 PENDING_SHEETS_SQL（未入结算模型且未确认过非结算角色）；
- 候选列映射/表头范围预填自自动识别结果；
- 确认原因必填（原则 14），抽取/仅存证均写审计；
- 按结算清单抽取走 confirm_sheet_role_and_extract（显式列映射必填）；
  仅存证走 confirm_sheet_non_settlement_role（5 种非结算角色）。

视觉结构（本次重构）：左侧待确认列表（名称+状态徽章+来源文件 Tooltip）；
右侧区块：基本信息 / 原始工作表预览（列标题可直接映射）/ 字段映射（Spin +
"第 N 列 · 列名 · 示例值"提示 + 自动识别浅蓝底标识）/ 表头与数据范围 /
  确认依据 / 可复用映射模板 / 操作（关闭 Tertiary、仅存证 Secondary、确认并抽取 Primary）。
"""
from __future__ import annotations

import json
import logging
import sqlite3

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from jiadun.core.engine.settlement_io import PENDING_SHEETS_SQL
from jiadun.core.mapping import templates as mapping_templates
from jiadun.ui import theme
from jiadun.ui.labels import DIRECTION_ZH
from jiadun.ui.widgets import section_header

_LOG = logging.getLogger(__name__)

SHEET_FIELD_ZH = [
    ("code", "编码"), ("name", "名称"), ("feature", "特征"), ("unit", "单位"),
    ("quantity", "工程量"), ("unit_price", "单价"), ("amount", "合价"), ("tax_rate", "税率"),
]
NON_SETTLEMENT_ROLE_ZH = {
    "non_settlement_form": "非结算表单（封面/审批表/承诺书等）",
    "settlement_summary": "汇总/统计表",
    "supporting_evidence": "支持证据（计量/收方/照片等）",
    "contract_control": "合同/控制性内容",
    "other_non_settlement": "其他非结算",
}

class SheetConfirmDialog(QDialog):
    """人工确认被门控工作表：选列映射/表头范围 → 按清单抽取，或仅存证。"""

    def __init__(self, conn, project_id: int, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.project_id = project_id
        self.setWindowTitle("人工确认工作表（表头歧义/无表头/表单）")
        self.resize(1120, 780)
        self._sheets: list[dict] = []
        self._col_spins: dict[str, QSpinBox] = {}
        self._auto_fields: set[str] = set()
        self._loading = False

        split = QSplitter(self)
        self.sheet_list = QListWidget()
        self.sheet_list.currentRowChanged.connect(self._on_select)
        split.addWidget(self.sheet_list)

        right = QWidget()
        form = QVBoxLayout(right)
        form.setContentsMargins(theme.SP_L, theme.SP_M, theme.SP_L, theme.SP_M)
        form.setSpacing(theme.SP_S)

        # ---- 1. 基本信息 ----
        form.addWidget(section_header("基本信息"))
        self.info_label = QLabel("（无待确认工作表）")
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent;")
        form.addWidget(self.info_label)

        # ---- 2. 方向与期次 ----
        row_dir = QHBoxLayout()
        self.dir_combo = QComboBox()
        for key in ("upward", "downward", "unknown"):
            self.dir_combo.addItem(DIRECTION_ZH[key], key)
        self.period_spin = QSpinBox()
        self.period_spin.setRange(1, 999)
        row_dir.addWidget(QLabel("方向："))
        row_dir.addWidget(self.dir_combo)
        row_dir.addSpacing(theme.SP_L)
        row_dir.addWidget(QLabel("期次："))
        row_dir.addWidget(self.period_spin)
        row_dir.addStretch(1)
        form.addLayout(row_dir)

        # ---- 3. 原始工作表预览 ----
        form.addWidget(section_header("原始工作表预览（点击列标题可映射）"))
        preview_map_row = QHBoxLayout()
        preview_map_row.addWidget(QLabel("点击预览列映射到："))
        self.preview_field_combo = QComboBox()
        for field, zh in SHEET_FIELD_ZH:
            self.preview_field_combo.addItem(zh, field)
        preview_map_row.addWidget(self.preview_field_combo)
        preview_map_row.addStretch(1)
        form.addLayout(preview_map_row)
        self.preview_table = QTableWidget()
        self.preview_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.preview_table.setSelectionMode(QTableWidget.SingleSelection)
        self.preview_table.setMinimumHeight(240)
        self.preview_table.setMaximumHeight(380)
        self.preview_table.horizontalHeader().sectionClicked.connect(self._preview_column_clicked)
        form.addWidget(self.preview_table)

        # ---- 4. 字段映射 ----
        form.addWidget(section_header("字段映射（列号 1 起始；0 = 该字段不使用）"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.SP_L)
        self._col_hints: dict[str, QLabel] = {}
        for i, (field, zh) in enumerate(SHEET_FIELD_ZH):
            row, col_base = divmod(i, 2)
            grid.addWidget(QLabel(zh), row, col_base * 3)
            spin = QSpinBox()
            spin.setRange(0, 256)
            spin.valueChanged.connect(lambda _v, f=field: self._mark_manual(f))
            grid.addWidget(spin, row, col_base * 3 + 1)
            hint = QLabel("—")
            hint.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent;")
            hint.setMinimumWidth(120)
            grid.addWidget(hint, row, col_base * 3 + 2)
            self._col_spins[field] = spin
            self._col_hints[field] = hint
        form.addLayout(grid)

        # ---- 5. 表头范围 ----
        form.addWidget(section_header("表头行范围"))
        row_hdr = QHBoxLayout()
        self.hdr_lo = QSpinBox()
        self.hdr_hi = QSpinBox()
        for s in (self.hdr_lo, self.hdr_hi):
            s.setRange(1, 9999)
        row_hdr.addWidget(QLabel("自第"))
        row_hdr.addWidget(self.hdr_lo)
        row_hdr.addWidget(QLabel("行至第"))
        row_hdr.addWidget(self.hdr_hi)
        row_hdr.addWidget(QLabel("行"))
        row_hdr.addStretch(1)
        form.addLayout(row_hdr)

        form.addWidget(section_header("数据行范围（可选；勾选表示人工确认完整范围）"))
        row_data = QHBoxLayout()
        self.data_confirm_check = QCheckBox("人工确认")
        self.data_lo = QSpinBox()
        self.data_hi = QSpinBox()
        for s in (self.data_lo, self.data_hi):
            s.setRange(0, 9999)
            s.setSpecialValueText("未指定")
        row_data.addWidget(self.data_confirm_check)
        row_data.addWidget(QLabel("第"))
        row_data.addWidget(self.data_lo)
        row_data.addWidget(QLabel("行至第"))
        row_data.addWidget(self.data_hi)
        row_data.addWidget(QLabel("行"))
        row_data.addStretch(1)
        form.addLayout(row_data)

        # ---- 6. 确认依据 ----
        form.addWidget(section_header("确认依据（必填，写入审计日志）"))
        self.reason_edit = QTextEdit()
        self.reason_edit.setPlaceholderText("必填：说明确认依据（将写入审计日志）")
        self.reason_edit.setMinimumHeight(64)
        form.addWidget(self.reason_edit, 1)

        # ---- 7. 可复用字段映射模板 ----
        form.addWidget(section_header("可复用字段映射模板（仅作候选，不会自动套用）"))
        template_row = QHBoxLayout()
        template_row.addWidget(QLabel("名称："))
        self.template_name_edit = QLineEdit()
        self.template_name_edit.setPlaceholderText("保存时填写模板名称")
        template_row.addWidget(self.template_name_edit, 2)
        template_row.addWidget(QLabel("作用域："))
        self.template_scope_combo = QComboBox()
        for key, label in (("sheet", "本 Sheet"), ("project", "本项目"), ("global", "全局")):
            self.template_scope_combo.addItem(label, key)
        template_row.addWidget(self.template_scope_combo, 1)
        self.save_template_btn = QPushButton("保存映射模板")
        self.save_template_btn.setToolTip("只保存当前人工核对的候选；不会修改当前 Sheet 映射")
        self.save_template_btn.clicked.connect(self._save_template)
        template_row.addWidget(self.save_template_btn)
        form.addLayout(template_row)
        self.template_hint = QLabel("暂无候选模板。模板推荐必须再次人工核对。")
        self.template_hint.setWordWrap(True)
        self.template_hint.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent;")
        form.addWidget(self.template_hint)

        # ---- 仅存证角色 ----
        role_row = QHBoxLayout()
        role_row.addWidget(QLabel("仅存证角色："))
        self.role_combo = QComboBox()
        for key, zh in NON_SETTLEMENT_ROLE_ZH.items():
            self.role_combo.addItem(zh, key)
        role_row.addWidget(self.role_combo, 1)
        form.addLayout(role_row)

        # ---- 操作 ----
        btn_row = QHBoxLayout()
        extract_btn = QPushButton("确认并抽取")
        extract_btn.setObjectName("btnPrimary")
        extract_btn.clicked.connect(self._do_extract)
        extract_next_btn = QPushButton("保存并下一张")
        extract_next_btn.clicked.connect(lambda: self._do_extract(advance=True))
        evidence_btn = QPushButton("仅存证（非结算表单）")
        evidence_btn.clicked.connect(self._do_evidence_only)
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("btnTertiary")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(evidence_btn)
        btn_row.addWidget(extract_next_btn)
        btn_row.addWidget(extract_btn)
        form.addLayout(btn_row)

        split.addWidget(right)
        split.setSizes([270, 640])
        split.setHandleWidth(1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(theme.SP_M, theme.SP_M, theme.SP_M, theme.SP_M)
        outer.addWidget(split)
        self.reload()

    # ---- 数据 ----
    def reload(self, select_row: int | None = None):
        self._sheets = [dict(r) for r in self.conn.execute(
            PENDING_SHEETS_SQL, (self.project_id,)).fetchall()]
        self.sheet_list.clear()
        for s in self._sheets:
            state = "表头歧义待复核" if s["col_map_json"] else "无表头（需完整人工映射）"
            item = QListWidgetItem(f"{s['sheet_name']}\n{state}")
            item.setToolTip(f"来源文件：{s['original_name']}")
            self.sheet_list.addItem(item)
        if self._sheets:
            target = 0 if select_row is None else min(max(0, select_row), len(self._sheets) - 1)
            self.sheet_list.setCurrentRow(target)
        else:
            self.info_label.setText("当前项目没有待人工确认的工作表。")
            self._refresh_preview()

    def _current(self) -> dict | None:
        row = self.sheet_list.currentRow()
        return self._sheets[row] if 0 <= row < len(self._sheets) else None

    def _column_hint(self, sheet_id: int, col: int, n_rows: int) -> str:
        """列提示：列号 + 表头文字 + 第一条样例值，帮助人工确认映射。"""
        base = f"第 {col} 列"
        s = self._current()
        lo, hi = (s["header_row_lo"] or 1), (s["header_row_hi"] or 1)
        texts = []
        for r in range(int(lo), min(int(hi), int(n_rows or 1)) + 1):
            v = self.conn.execute(
                "SELECT raw_value FROM raw_cells WHERE sheet_id=? AND row=? AND col=?",
                (sheet_id, r, col)).fetchone()
            if v and v["raw_value"] and str(v["raw_value"]).strip():
                texts.append(str(v["raw_value"]).split("\n")[0].strip()[:8])
        header_text = texts[0] if texts else "—"
        sample = self.conn.execute(
            """SELECT raw_value FROM raw_cells
               WHERE sheet_id=? AND col=? AND row>? AND raw_value IS NOT NULL
                 AND TRIM(raw_value)<>'' ORDER BY row LIMIT 1""",
            (sheet_id, col, int(hi)),
        ).fetchone()
        sample_text = str(sample["raw_value"]).split("\n")[0].strip()[:16] if sample else "—"
        return f"{base} · 表头：{header_text} · 示例：{sample_text}"

    def _on_select(self, row: int):
        s = self._current()
        if not s:
            return
        max_col = int(s["n_cols"] or 1)
        for spin in self._col_spins.values():
            spin.setMaximum(max(1, max_col))
        max_row = int(s["n_rows"] or 1)
        self.data_lo.setMaximum(max_row)
        self.data_hi.setMaximum(max_row)
        self._auto_fields = set()
        self._loading = True
        if s["col_map_json"]:
            col_map = json.loads(s["col_map_json"])
            for field, spin in self._col_spins.items():
                spin.setValue(int(col_map.get(field, 0)))
            self.hdr_lo.setValue(int(s["header_row_lo"] or 1))
            self.hdr_hi.setValue(int(s["header_row_hi"] or 1))
            self.data_lo.setValue(int(s.get("data_row_start") or 0))
            self.data_hi.setValue(int(s.get("data_row_end") or 0))
            self.data_confirm_check.setChecked(False)
            self._auto_fields = {f for f, sp in self._col_spins.items() if sp.value() > 0}
            src = (f"来自文件「{s['original_name']}」；自动识别存在歧义，"
                   "已预填候选（浅蓝底 = 自动识别），请核对后确认")
        else:
            for spin in self._col_spins.values():
                spin.setValue(0)
            self.hdr_lo.setValue(1)
            self.hdr_hi.setValue(1)
            self.data_lo.setValue(0)
            self.data_hi.setValue(0)
            self.data_confirm_check.setChecked(False)
            src = f"来自文件「{s['original_name']}」；该表无自动表头，请人工指定列映射与表头行"
        self._loading = False
        self._refresh_auto_style()
        self._refresh_hints()
        self._refresh_preview()
        self.info_label.setText(
            f"当前第 {self.sheet_list.currentRow() + 1} / 共 {len(self._sheets)} 张待确认工作表\n"
            f"工作表「{s['sheet_name']}」（共 {max_col} 列）\n{src}"
        )
        self._refresh_template_hint()

    def _refresh_template_hint(self):
        """显示只读候选模板；候选不会直接写入 SpinBox 或 table_headers。"""
        s = self._current()
        if not s:
            self.template_hint.setText("暂无候选模板。模板推荐必须再次人工核对。")
            return
        try:
            candidates = mapping_templates.recommend_mapping_templates(
                self.conn, self.project_id, int(s["sheet_id"]), limit=3)
        except (ValueError, sqlite3.Error):
            candidates = []
        if not candidates:
            self.template_hint.setText("暂无相似模板。保存后模板只会作为后续人工核对候选。")
            return
        details = []
        for candidate in candidates:
            template = candidate.template
            scope = {"sheet": "本 Sheet", "project": "本项目", "global": "全局"}.get(
                template.scope, template.scope)
            details.append(
                f"{template.template_name} v{template.version}（{scope}，相似度 {candidate.score}，"
                f"创建人：{template.created_by}）"
            )
        self.template_hint.setText(
            "候选模板（仅供人工核对，不会自动套用）：" + "；".join(details)
        )

    def _refresh_auto_style(self):
        for field, spin in self._col_spins.items():
            auto = field in self._auto_fields and spin.value() > 0
            spin.setProperty("auto", auto)
            spin.setStyleSheet(
                f"QSpinBox[auto='true'] {{ background: {theme.PRIMARY_SOFT}; }}"
                if auto else "")
            spin.style().unpolish(spin)
            spin.style().polish(spin)

    def _refresh_hints(self):
        s = self._current()
        if not s:
            return
        for field, spin in self._col_spins.items():
            col = spin.value()
            if col > 0:
                auto_mark = " · 自动" if field in self._auto_fields else ""
                self._col_hints[field].setText(
                    self._column_hint(int(s["sheet_id"]), col, s["n_rows"]) + auto_mark)
            else:
                self._col_hints[field].setText("—")

    def _refresh_preview(self):
        """显示当前工作表的真实原始网格，限制预览规模但不改变取数范围。"""
        s = self._current()
        if not s:
            self.preview_table.clear()
            self.preview_table.setRowCount(0)
            self.preview_table.setColumnCount(0)
            return
        n_rows = min(int(s["n_rows"] or 0), 30)
        n_cols = min(int(s["n_cols"] or 0), 20)
        values = {
            (int(row["row"]), int(row["col"])): row["raw_value"]
            for row in self.conn.execute(
                """SELECT row, col, raw_value FROM raw_cells
                   WHERE sheet_id=? AND row<=? AND col<=?""",
                (int(s["sheet_id"]), n_rows, n_cols),
            ).fetchall()
        }
        self.preview_table.setRowCount(n_rows)
        self.preview_table.setColumnCount(n_cols)
        self.preview_table.setHorizontalHeaderLabels([f"列{i}" for i in range(1, n_cols + 1)])
        self.preview_table.setVerticalHeaderLabels([f"行{i}" for i in range(1, n_rows + 1)])
        for row in range(1, n_rows + 1):
            for col in range(1, n_cols + 1):
                value = values.get((row, col))
                text = str(value) if value not in (None, "") else ""
                item = QTableWidgetItem(text)
                # 单元格内容可能很长：悬浮显示全文，列宽只给合理宽度 + 横向滚动
                item.setToolTip(text)
                self.preview_table.setItem(row - 1, col - 1, item)
        self.preview_table.resizeColumnsToContents()
        # 列宽钳制：过窄看不清、过宽挤掉其他列；超出部分横向滚动查看
        for col in range(self.preview_table.columnCount()):
            width = self.preview_table.columnWidth(col)
            self.preview_table.setColumnWidth(col, max(72, min(width, 240)))

    def _preview_column_clicked(self, column: int):
        """点击预览列直接填入当前选择的字段，仍走互斥列校验。"""
        field = self.preview_field_combo.currentData()
        spin = self._col_spins.get(field)
        if spin is None:
            return
        spin.setValue(column + 1)

    def _mark_manual(self, field: str):
        if getattr(self, "_loading", False):
            return
        self._auto_fields.discard(field)
        spin = self._col_spins[field]
        spin.setProperty("auto", False)
        spin.setStyleSheet("")
        spin.style().unpolish(spin)
        spin.style().polish(spin)
        if spin.value() > 0:
            hint = self._col_hints.get(field)
            if hint and hint.text() != "—":
                hint.setText(hint.text().split(" · 自动")[0])

    def _col_map(self) -> dict[str, int]:
        return {f: sp.value() for f, sp in self._col_spins.items() if sp.value() > 0}

    def _data_range(self, n_rows: int) -> tuple[int, int] | None:
        if not self.data_confirm_check.isChecked():
            return None
        start, end = self.data_lo.value(), self.data_hi.value()
        if not (1 <= start <= end <= n_rows):
            raise ValueError(f"确认数据行范围无效（有效行 1..{n_rows}）")
        if start <= self.hdr_hi.value():
            raise ValueError("数据起始行必须位于表头结束行之后")
        return start, end

    def _validated(self) -> tuple[dict, tuple[int, int], str]:
        s = self._current()
        if not s:
            raise ValueError("没有选中的工作表")
        reason = self.reason_edit.toPlainText().strip()
        if not reason:
            raise ValueError("确认原因必填（原则 14）")
        col_map = self._col_map()
        if "name" not in col_map:
            raise ValueError("列映射必须包含「名称」列")
        if "amount" not in col_map and not ("quantity" in col_map and "unit_price" in col_map):
            raise ValueError("金额口径不完整：需「合价」列，或同时提供「工程量」+「单价」列")
        cols = list(col_map.values())
        if len(cols) != len(set(cols)):
            raise ValueError("同一列被映射到了多个字段")
        hdr = (self.hdr_lo.value(), self.hdr_hi.value())
        n_rows = int(s["n_rows"] or 1)
        if not (1 <= hdr[0] <= hdr[1] <= n_rows):
            raise ValueError(f"表头行范围无效（有效行 1..{n_rows}）")
        return col_map, hdr, reason

    def _save_template(self):
        """把当前人工选择追加保存为模板，不改变当前 Sheet 的实际映射。"""
        s = self._current()
        if not s:
            return
        template_name = self.template_name_edit.text().strip()
        if not template_name:
            QMessageBox.warning(self, "保存映射模板", "模板名称必填。")
            return
        try:
            col_map, hdr, reason = self._validated()
            data_range = self._data_range(int(s["n_rows"] or 1))
            template = mapping_templates.save_mapping_template(
                self.conn,
                self.project_id,
                int(s["sheet_id"]),
                scope=self.template_scope_combo.currentData(),
                template_name=template_name,
                col_map=col_map,
                header_range=hdr,
                data_range=data_range,
                created_by="user",
                reason=reason,
            )
        except (ValueError, mapping_templates.audit_log.AuditReasonRequiredError) as exc:
            QMessageBox.warning(self, "保存映射模板", str(exc))
            return
        QMessageBox.information(
            self,
            "保存映射模板",
            f"已保存「{template.template_name}」v{template.version}。\n"
            "后续仅作为候选推荐，仍需人工核对后再确认。",
        )

    def _do_extract(self, advance: bool = False):
        s = self._current()
        if not s:
            return
        current_row = self.sheet_list.currentRow()
        try:
            col_map, hdr, reason = self._validated()
            data_range = self._data_range(int(s["n_rows"] or 1))
        except ValueError as exc:
            QMessageBox.warning(self, "人工确认", str(exc))
            return
        from jiadun.core.engine import settlement_io

        direction = self.dir_combo.currentData()
        try:
            n = settlement_io.confirm_sheet_role_and_extract(
                self.conn, self.project_id, int(s["sheet_id"]), actor="user",
                reason=reason, direction=direction, period_no=self.period_spin.value(),
                confirmed_col_map=col_map, confirmed_header_range=hdr,
                confirmed_data_range=data_range)
            state = self.conn.execute(
                "SELECT sheet_status, sheet_status_reason FROM raw_sheets WHERE id=?",
                (int(s["sheet_id"]),),
            ).fetchone()
            if state and str(state["sheet_status"] or "") == "pending":
                pending_reason = str(state["sheet_status_reason"] or "结构性证据仍待复核")
                QMessageBox.warning(
                    self,
                    "人工确认已保存，仍待复核",
                    f"已保存并抽取 {n} 行明细，但该工作表仍待复核。\n"
                    f"{pending_reason}\n可继续调整范围或字段映射后重试。",
                )
            else:
                QMessageBox.information(self, "人工确认", f"已抽取 {n} 行明细。")
        except Exception as exc:  # noqa: BLE001 — UI 层兜底提示
            _LOG.exception(
                "工作表确认抽取失败 project_id=%s sheet_id=%s",
                self.project_id,
                s["sheet_id"],
            )
            detail = str(exc).strip() or "程序未返回具体原因"
            QMessageBox.warning(
                self, "人工确认失败",
                "工作表确认未完成："
                f"{detail}\n请检查字段映射、表头/数据行范围和确认依据后重试。",
            )
            return
        self.reason_edit.clear()
        self.reload(current_row if advance else None)

    def _do_evidence_only(self):
        s = self._current()
        if not s:
            return
        reason = self.reason_edit.toPlainText().strip()
        if not reason:
            QMessageBox.warning(self, "人工确认", "确认原因必填（原则 14）")
            return
        from jiadun.core.engine import settlement_io

        try:
            settlement_io.confirm_sheet_non_settlement_role(
                self.conn, self.project_id, int(s["sheet_id"]), actor="user",
                confirmed_role=self.role_combo.currentData(), reason=reason)
        except Exception:  # noqa: BLE001
            QMessageBox.warning(
                self, "仅存证失败",
                "工作表未完成仅存证确认，请检查确认依据后重试。",
            )
            return
        self.reason_edit.clear()
        self.reload()
