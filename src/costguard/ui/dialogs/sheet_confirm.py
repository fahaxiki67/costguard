"""人工确认被门控工作表（SheetConfirmDialog）——视觉分组重构版。

业务规则不变（相对重构前逐条保持）：
- 待确认列表来自 PENDING_SHEETS_SQL（未入结算模型且未确认过非结算角色）；
- 候选列映射/表头范围预填自自动识别结果；
- 确认原因必填（原则 14），抽取/仅存证均写审计；
- 按结算清单抽取走 confirm_sheet_role_and_extract（显式列映射必填）；
  仅存证走 confirm_sheet_non_settlement_role（5 种非结算角色）。

视觉结构（本次重构）：左侧待确认列表（名称+状态徽章+来源文件 Tooltip）；
右侧五个区块：基本信息 / 字段映射（Spin + "第 N 列 · 列名"提示 + 自动识别
浅蓝底标识）/ 表头范围 / 确认依据 / 操作（关闭 Tertiary、仅存证 Secondary、
确认并抽取 Primary）。
"""
from __future__ import annotations

import json

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from costguard.ui import theme
from costguard.ui.labels import DIRECTION_ZH
from costguard.ui.widgets import section_header

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

# 待人工确认 sheet：尚未进入结算模型，且未确认过非结算角色
PENDING_SHEETS_SQL = """
SELECT rs.id AS sheet_id, rs.sheet_name, rs.n_cols, rs.n_rows, sf.original_name,
       th.col_map_json, th.header_row_lo, th.header_row_hi, th.needs_review
FROM raw_sheets rs
JOIN parse_batches pb ON pb.id = rs.batch_id
JOIN source_files sf ON sf.id = pb.file_id
LEFT JOIN table_headers th ON th.sheet_id = rs.id
WHERE sf.project_id=? AND rs.period_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM audit_log al
                  WHERE al.project_id=sf.project_id AND al.target='sheet:'||rs.id
                  AND al.action='confirm_sheet_non_settlement_role')
ORDER BY sf.id, rs.id"""


class SheetConfirmDialog(QDialog):
    """人工确认被门控工作表：选列映射/表头范围 → 按清单抽取，或仅存证。"""

    def __init__(self, conn, project_id: int, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.project_id = project_id
        self.setWindowTitle("人工确认工作表（表头歧义/无表头/表单）")
        self.resize(920, 620)
        self._sheets: list[dict] = []
        self._col_spins: dict[str, QSpinBox] = {}
        self._auto_fields: set[str] = set()

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

        # ---- 3. 字段映射 ----
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

        # ---- 4. 表头范围 ----
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

        # ---- 5. 确认依据 ----
        form.addWidget(section_header("确认依据（必填，写入审计日志）"))
        self.reason_edit = QTextEdit()
        self.reason_edit.setPlaceholderText("必填：说明确认依据（将写入审计日志）")
        self.reason_edit.setMinimumHeight(64)
        form.addWidget(self.reason_edit, 1)

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
        evidence_btn = QPushButton("仅存证（非结算表单）")
        evidence_btn.clicked.connect(self._do_evidence_only)
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("btnTertiary")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(evidence_btn)
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
    def reload(self):
        self._sheets = [dict(r) for r in self.conn.execute(
            PENDING_SHEETS_SQL, (self.project_id,)).fetchall()]
        self.sheet_list.clear()
        for s in self._sheets:
            state = "表头歧义待复核" if s["col_map_json"] else "无表头（需完整人工映射）"
            item = QListWidgetItem(f"{s['sheet_name']}\n{state}")
            item.setToolTip(f"来源文件：{s['original_name']}")
            self.sheet_list.addItem(item)
        if self._sheets:
            self.sheet_list.setCurrentRow(0)
        else:
            self.info_label.setText("当前项目没有待人工确认的工作表。")

    def _current(self) -> dict | None:
        row = self.sheet_list.currentRow()
        return self._sheets[row] if 0 <= row < len(self._sheets) else None

    def _column_hint(self, sheet_id: int, col: int, n_rows: int) -> str:
        """列提示：'第 N 列 · 表头文字（截断）'。表头文字取自动识别的表头行范围。"""
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
        if texts:
            return f"{base} · {texts[0]}"
        return base

    def _on_select(self, row: int):
        s = self._current()
        if not s:
            return
        max_col = int(s["n_cols"] or 1)
        for spin in self._col_spins.values():
            spin.setMaximum(max(1, max_col))
        self._auto_fields = set()
        self._loading = True
        if s["col_map_json"]:
            col_map = json.loads(s["col_map_json"])
            for field, spin in self._col_spins.items():
                spin.setValue(int(col_map.get(field, 0)))
            self.hdr_lo.setValue(int(s["header_row_lo"] or 1))
            self.hdr_hi.setValue(int(s["header_row_hi"] or 1))
            self._auto_fields = {f for f, sp in self._col_spins.items() if sp.value() > 0}
            src = (f"来自文件「{s['original_name']}」；自动识别存在歧义，"
                   "已预填候选（浅蓝底 = 自动识别），请核对后确认")
        else:
            for spin in self._col_spins.values():
                spin.setValue(0)
            self.hdr_lo.setValue(1)
            self.hdr_hi.setValue(1)
            src = f"来自文件「{s['original_name']}」；该表无自动表头，请人工指定列映射与表头行"
        self._loading = False
        self._refresh_auto_style()
        self._refresh_hints()
        self.info_label.setText(f"工作表「{s['sheet_name']}」（共 {max_col} 列）\n{src}")

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

    def _do_extract(self):
        s = self._current()
        if not s:
            return
        try:
            col_map, hdr, reason = self._validated()
        except ValueError as exc:
            QMessageBox.warning(self, "人工确认", str(exc))
            return
        from costguard.core.engine import settlement_io

        direction = self.dir_combo.currentData()
        try:
            n = settlement_io.confirm_sheet_role_and_extract(
                self.conn, self.project_id, int(s["sheet_id"]), actor="user",
                reason=reason, direction=direction, period_no=self.period_spin.value(),
                confirmed_col_map=col_map, confirmed_header_range=hdr)
            QMessageBox.information(self, "人工确认", f"已抽取 {n} 行明细。")
        except Exception as exc:  # noqa: BLE001 — UI 层兜底提示
            QMessageBox.warning(self, "人工确认失败", f"{type(exc).__name__}: {exc}")
            return
        self.reason_edit.clear()
        self.reload()

    def _do_evidence_only(self):
        s = self._current()
        if not s:
            return
        reason = self.reason_edit.toPlainText().strip()
        if not reason:
            QMessageBox.warning(self, "人工确认", "确认原因必填（原则 14）")
            return
        from costguard.core.engine import settlement_io

        try:
            settlement_io.confirm_sheet_non_settlement_role(
                self.conn, self.project_id, int(s["sheet_id"]), actor="user",
                confirmed_role=self.role_combo.currentData(), reason=reason)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "仅存证失败", f"{type(exc).__name__}: {exc}")
            return
        self.reason_edit.clear()
        self.reload()
