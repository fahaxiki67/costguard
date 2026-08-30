"""工作台页面（Phase 8）。

Tab 结构：期次概览 | 清单明细 | 异常检测 | 匹配复核 | 成果导出。
纪律落进 UI：
- 修改匹配级别 / 处理异常必须填写原因（原则 14）；
- 所有查询只读，绝不直接改业务数据；
- 期次方向标记（对上/对下）保存前要求确认。
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from costguard.core.anomalies import engine as anomaly_engine
from costguard.core.engine import crosscheck, settlement_io
from costguard.core.export import excel_export
from costguard.core.matching import matching
from costguard.platform import paths as platform_paths

SEVERITY_ZH = {"high": "高", "medium": "中", "low": "低", "info": "提示"}
DIRECTION_ZH = {"upward": "对上", "downward": "对下", "unknown": "未标记"}
LEVEL_ZH = {
    "confirmed": "规则完全匹配（待人工确认）",
    "probable": "高概率匹配",
    "suspected": "疑似匹配",
    "incomparable": "不可比",
    "pending_data": "待补资料",
}


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
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
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


def _make_table(headers: list[str]) -> QTableWidget:
    t = QTableWidget()
    t.setColumnCount(len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setAlternatingRowColors(True)
    return t


def _polish_table(t: QTableWidget, right_align_cols: tuple[int, ...]) -> None:
    """统一观感：数字列右对齐、表头加粗，按内容自适应列宽。"""
    for row in range(t.rowCount()):
        for col in right_align_cols:
            item = t.item(row, col)
            if item is not None:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    t.resizeColumnsToContents()
    header = t.horizontalHeader()
    font = header.font()
    font.setBold(True)
    header.setFont(font)


class WorkbenchPage(QWidget):
    def __init__(self, conn, project, project_dir: str, on_back):
        super().__init__()
        self.conn = conn
        self.project = project
        self.project_dir = project_dir
        self.tabs = QTabWidget()
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        back_btn = QPushButton("← 返回项目列表")
        back_btn.clicked.connect(on_back)
        title = QLabel(f"当前项目：{project.name}")
        top.addWidget(back_btn)
        top.addWidget(title, 1)
        layout.addLayout(top)
        layout.addWidget(self.tabs, 1)
        self.tabs.addTab(self._period_tab(), "期次概览")
        self.tabs.addTab(self._items_tab(), "清单明细")
        self.tabs.addTab(self._anomaly_tab(), "异常检测")
        self.tabs.addTab(self._match_tab(), "匹配复核")
        self.tabs.addTab(self._export_tab(), "成果导出")
        self.refresh_all()

    # ---------- 期次概览 ----------
    def _period_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        btn_row = QHBoxLayout()
        import_btn = QPushButton("导入结算文件…")
        import_btn.clicked.connect(self._import_files)
        contract_btn = QPushButton("导入合同/纪要…")
        contract_btn.clicked.connect(self._import_contract)
        detect_btn = QPushButton("运行异常检测")
        detect_btn.clicked.connect(self._run_anomalies)
        check_btn = QPushButton("双向校核")
        check_btn.clicked.connect(self._run_crosscheck)
        for b in (import_btn, contract_btn, detect_btn, check_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        v.addLayout(btn_row)
        self.period_table = _make_table(
            ["期次", "标题", "方向", "明细行数", "小计行", "合同单位"])
        v.addWidget(self.period_table, 1)
        self.dir_combo = QComboBox()
        self.dir_combo.addItems(["", "upward", "downward"])
        self.dir_combo.setItemText(0, "（未标记）")
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
            direction = {"upward": "对上", "downward": "对下"}.get(r["direction"], "（未标记）")
            vals = [r["period_no"], r["title"], direction, r["items"], r["subs"], r["contract_party"]]
            for c, v in enumerate(vals):
                t.setItem(i, c, QTableWidgetItem(str(v if v is not None else "—")))
            # 行必须绑定明确 period_id：对上/对下可同期号，按期号更新会双向覆盖
            t.item(i, 0).setData(Qt.UserRole, int(r["id"]))
        _polish_table(t, (0, 3, 4))

    def _import_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择结算文件", "", "结算文件 (*.xlsx *.xlsm *.xls *.csv)")
        if not files:
            return
        ok, fail = 0, []
        for f in files:
            try:
                settlement_io.import_settlement_file(
                    self.conn, self.project.project_id, self.project_dir, f)
                ok += 1
            except Exception as exc:  # noqa: BLE001 — UI 层兜底提示
                fail.append(f"{Path(f).name}: {exc}")
        self._notify_import(ok, fail)
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
            except Exception as exc:  # noqa: BLE001
                fail.append(f"{Path(f).name}: {exc}")
        self._notify_import(ok, fail)
        self.refresh_all()

    def _run_anomalies(self):
        findings = anomaly_engine.run_anomalies(self.conn, self.project.project_id)
        summary = anomaly_engine.anomaly_summary(findings)
        QMessageBox.information(
            self, "异常检测",
            f"检测完成：高 {summary['high']} / 中 {summary['medium']} / 低 {summary['low']}\n"
            "详见「异常检测」页。")
        self.refresh_anomalies()

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
            except AmbiguousPeriodError as exc:
                errors.append(str(exc))
        status_zh = {"match": "一致", "diff": "存在差异", "incomplete": "数据不完整"}
        dir_zh = {"upward": "对上", "downward": "对下", "unknown": ""}
        msg = "\n".join(
            f"第{r.period_no}期{dir_zh.get(r.direction, '')}：{status_zh[r.status]}"
            f"（A={r.path_a_total}，B={r.path_b_total}）" for r in results)
        if errors:
            msg += "\n\n以下期号存在方向歧义，已跳过（请先标记方向）：\n" + "\n".join(errors)
        QMessageBox.information(self, "双向校核", msg or "无期次可校核")
        if any(r.status == "diff" for r in results):
            self.tabs.setCurrentIndex(2)
        self.refresh_anomalies()

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
        direction = self.dir_combo.currentText()
        # 未标记方向使用 schema 允许的明确值，不写 NULL
        direction = "unknown" if direction.startswith("（") else direction
        dir_zh = {"upward": "对上", "downward": "对下", "unknown": "未标记"}[direction]
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
                raise RuntimeError(f"direction update affected {cur.rowcount} rows for period_id={period_id}")
        audit_log.record_audit(
            self.conn, self.project.project_id, "user", "set_direction", f"period:{period_id}",
            None, {"direction": direction, "period_no": pno}, dlg.reason())
        self.refresh_periods()

    # ---------- 清单明细 ----------
    def _items_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.items_table = _make_table(
            ["方向", "期次", "编码", "名称", "单位", "数量", "单价", "合价", "税率", "出处(数量)"])
        v.addWidget(self.items_table, 1)
        return w

    def refresh_items(self):
        rows = self.conn.execute(
            """SELECT li.*, sp.period_no AS pno, sp.direction AS direction FROM line_items li
               JOIN settlement_periods sp ON sp.id = li.period_id
               WHERE sp.project_id=? ORDER BY sp.period_no, li.id LIMIT 2000""",
            (self.project.project_id,),
        ).fetchall()
        t = self.items_table
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            flags = json.loads(r["flags_json"] or "{}")
            ev = json.loads(r["qty_evid"]) if r["qty_evid"] else None
            vals = [
                DIRECTION_ZH.get(r["direction"], r["direction"]), r["pno"], r["code"],
                ("【小计】" if flags.get("subtotal") else "") + (r["name"] or ""),
                r["unit"], r["quantity"], r["unit_price"], r["amount"], r["tax_rate"],
                (f"行{ev['row']}列{ev['col']}" if ev else "—"),
            ]
            for c, v in enumerate(vals):
                t.setItem(i, c, QTableWidgetItem(str(v if v is not None else "—")))
        _polish_table(t, (1, 5, 6, 7, 8))

    # ---------- 异常检测 ----------
    def _anomaly_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh_anomalies)
        resolve_btn = QPushButton("标记选中异常为已处理（需原因）")
        resolve_btn.clicked.connect(self._resolve_anomaly)
        row = QHBoxLayout()
        row.addWidget(refresh_btn)
        row.addWidget(resolve_btn)
        row.addStretch(1)
        v.addLayout(row)
        self.anomaly_table = _make_table(["编号", "方向", "级别", "规则", "说明", "证据ID", "状态"])
        v.addWidget(self.anomaly_table, 1)
        return w

    def refresh_anomalies(self):
        rows = self.conn.execute(
            """SELECT a.id, a.rule_id, a.severity, a.message, a.evidence_id, a.status,
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
               WHERE a.project_id=? ORDER BY CASE a.severity
               WHEN 'high' THEN 0 WHEN 'medium' THEN 1
               WHEN 'low' THEN 2 ELSE 3 END, a.id""",
            (self.project.project_id,),
        ).fetchall()
        t = self.anomaly_table
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            vals = [r["id"], DIRECTION_ZH.get(r["direction"], r["direction"] or "项目级"),
                    SEVERITY_ZH.get(r["severity"], r["severity"]), r["rule_id"],
                    r["message"], r["evidence_id"], r["status"]]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(str(v if v is not None else "—"))
                if r["severity"] == "high" and c <= 3:
                    item.setForeground(Qt.red)
                t.setItem(i, c, item)
        _polish_table(t, (0,))

    def _resolve_anomaly(self):
        row = self.anomaly_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "处理异常", "请先选择异常")
            return
        aid = int(self.anomaly_table.item(row, 0).text())
        dlg = ReasonDialog("处理异常", f"将异常 #{aid} 标记为已处理", self)
        if dlg.exec() != QDialog.Accepted:
            return
        from costguard.core.evidence import audit as audit_log
        from costguard.core.evidence import evidence as evidence_api

        with self.conn:
            self.conn.execute(
                "UPDATE anomalies SET status='resolved', resolved_note=? WHERE id=?",
                (dlg.reason(), aid))
        evidence_api.add_evidence(
            self.conn, self.project.project_id, "anomaly_resolution",
            f"异常 #{aid} 已处理：{dlg.reason()}",
            steps=[{"step": "人工处理", "reason": dlg.reason()}],
            sources=[{"anomaly_id": aid}])
        audit_log.record_audit(
            self.conn, self.project.project_id, "user", "resolve_anomaly", f"anomaly:{aid}",
            {"status": "open"}, {"status": "resolved"}, dlg.reason())
        self.refresh_anomalies()

    # ---------- 匹配复核 ----------
    def _match_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        run_btn = QPushButton("运行匹配")
        run_btn.clicked.connect(self._run_match)
        confirm_btn = QPushButton("确认选中匹配（需原因）")
        confirm_btn.clicked.connect(self._confirm_match)
        override_btn = QPushButton("修正选中级别（需原因）")
        override_btn.clicked.connect(self._override_match)
        for b in (run_btn, confirm_btn, override_btn):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.match_table = _make_table(["编号", "组键", "级别", "方法", "得分", "行数", "备注"])
        v.addWidget(self.match_table, 1)
        return w

    def _run_match(self):
        groups = matching.match_items(self.conn, self.project.project_id)
        n = matching.save_matches(self.conn, self.project.project_id, groups)
        QMessageBox.information(self, "匹配", f"已生成 {n} 个匹配组，疑似/待复核项请人工确认。")
        self.refresh_matches()

    def refresh_matches(self):
        rows = self.conn.execute(
            """SELECT id, group_key, level, method, score, item_ids_json, status FROM matches
               WHERE project_id=? ORDER BY CASE level
               WHEN 'confirmed' THEN 0 WHEN 'probable' THEN 1 WHEN 'suspected' THEN 2
               WHEN 'incomparable' THEN 3 ELSE 4 END, id""",
            (self.project.project_id,),
        ).fetchall()
        t = self.match_table
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            n_items = len(json.loads(r["item_ids_json"] or "[]"))
            vals = [r["id"], r["group_key"], LEVEL_ZH.get(r["level"], r["level"]),
                    r["method"], f"{r['score']:.2f}" if r["score"] is not None else "—", n_items, r["status"]]
            for c, v in enumerate(vals):
                t.setItem(i, c, QTableWidgetItem(str(v)))
        _polish_table(t, (0, 4, 5))

    def _confirm_match(self):
        row = self.match_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "匹配复核", "请先选择匹配组")
            return
        mid = int(self.match_table.item(row, 0).text())
        alias = self.match_table.item(row, 1).text()
        dlg = ReasonDialog("人工确认匹配", f"将匹配组 #{mid} 标记为人工已确认？", self)
        if dlg.exec() != QDialog.Accepted:
            return
        matching.confirm_match(self.conn, self.project.project_id, mid, "user", dlg.reason(),
                               alias_name=alias)
        self.refresh_matches()

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

    # ---------- 成果导出 ----------
    def _export_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        export_btn = QPushButton("导出全部报表（Excel）")
        export_btn.clicked.connect(self._export_excel)
        doc_btn = QPushButton("导出管理层摘要（Word）")
        doc_btn.clicked.connect(self._export_docx)
        open_dir_btn = QPushButton("在 Finder 中显示导出目录")
        open_dir_btn.clicked.connect(self._open_export_dir)
        row = QHBoxLayout()
        for b in (export_btn, doc_btn, open_dir_btn):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        tip = QLabel(
            "导出说明：\n"
            "· 审核底稿保留公式（合价=数量×单价、差异列），WPS/Excel 打开自动计算；\n"
            "· 缺失数据不自动补 0，标注「待补资料」；不可比数据不强行比较；\n"
            "· 全部结论附证据索引，可追溯至原始单元格。")
        tip.setWordWrap(True)
        v.addWidget(tip)
        v.addStretch(1)
        return w

    def _export_excel(self):
        path = excel_export.export_workbook(
            self.conn, self.project.project_id, Path(self.project_dir) / "exports")
        platform_paths.reveal_in_file_manager(path)
        QMessageBox.information(self, "导出完成", f"已导出：\n{path}")

    def _export_docx(self):
        path = excel_export.export_management_summary_docx(
            self.conn, self.project.project_id, Path(self.project_dir) / "exports")
        platform_paths.reveal_in_file_manager(path)
        QMessageBox.information(self, "导出完成", f"已导出：\n{path}")

    def _open_export_dir(self):
        platform_paths.reveal_in_file_manager(Path(self.project_dir) / "exports")

    # ---------- 公共 ----------
    def refresh_all(self):
        self.refresh_periods()
        self.refresh_items()
        self.refresh_anomalies()
        self.refresh_matches()

    def _notify_import(self, ok: int, fail: list[str]):
        msg = f"成功导入 {ok} 个文件（原文件未改动）。"
        if fail:
            msg += "\n失败：\n" + "\n".join(fail)
        QMessageBox.information(self, "导入", msg)
