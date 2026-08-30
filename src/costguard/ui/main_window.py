"""CostGuard 主窗口：项目列表页 ↔ 工作台页（QStackedWidget）。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from costguard.core import demo as demo_core
from costguard.core.models import project as project_model
from costguard.ui.widgets import empty_state, section_header
from costguard.ui.workbench import WorkbenchPage

PAGE_PROJECTS = 0
PAGE_WORKBENCH = 1

_GEOMETRY_KEY = "main/geometry"


def _project_error_text(error: Exception) -> str:
    """把核心层项目错误转换为普通用户可执行的中文提示。"""
    text = str(error)
    if text == "project name is empty":
        return "项目名称不能为空。"
    if text.startswith("project directory already exists"):
        return "项目目录已经存在且不为空，请更换项目名称或工作空间。"
    if text.startswith("not a CostGuard project"):
        return "所选目录不是 CostGuard 项目，请选择包含项目数据库的目录。"
    if text.startswith("project.db has no project record"):
        return "项目数据库中没有项目记录，暂时无法打开。"
    return "项目操作未完成，请检查所选目录和权限后重试。"


def load_app_icon() -> QIcon | None:
    """应用图标（打包/源码双模式定位）；缺失时返回 None，用系统默认。"""
    from costguard.platform import resources as platform_resources

    path = platform_resources.app_icon_path()
    return QIcon(str(path)) if path else None


class NewProjectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建项目")
        self.name_edit = QLineEdit()
        self.root_label = QLabel(str(project_model.workspace_root()))
        change_btn = QPushButton("更改工作空间…")
        change_btn.clicked.connect(self._change_root)
        row = QHBoxLayout()
        row.addWidget(self.root_label, 1)
        row.addWidget(change_btn)
        form = QFormLayout(self)
        form.addRow("项目名称：", self.name_edit)
        form.addRow("工作空间：", row)
        bb = QDialogButtonBox()
        create_btn = bb.addButton("创建", QDialogButtonBox.AcceptRole)
        create_btn.setObjectName("btnPrimary")
        bb.addButton("取消", QDialogButtonBox.RejectRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _change_root(self):
        d = QFileDialog.getExistingDirectory(self, "选择工作空间目录", str(self.root_label.text()))
        if d:
            self.root_label.setText(d)

    def values(self) -> tuple[str, Path]:
        return self.name_edit.text().strip(), Path(self.root_label.text())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CostGuard — 工程经营合规智能工作台")
        icon = load_app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        settings = QSettings("CostGuard", "CostGuard")
        geo = settings.value(_GEOMETRY_KEY)
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1080, 680)
        self._conn = None
        self._project = None

        self.stack = QStackedWidget()
        self.stack.addWidget(self._projects_page())
        self.stack.addWidget(QWidget())  # 工作台占位，打开项目时创建
        self.setCentralWidget(self.stack)
        self.setStatusBar(QStatusBar())

    # ---- 项目列表页 ----
    def _projects_page(self) -> QWidget:
        from costguard.ui import theme

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(theme.SP_XL, theme.SP_XL, theme.SP_XL, theme.SP_L)
        layout.setSpacing(theme.SP_S)

        title = QLabel("CostGuard")
        title.setStyleSheet(
            "font-size: 20px; font-weight: 700; background: transparent;")
        subtitle = QLabel("工程经营合规智能工作台")
        subtitle.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; background: transparent;")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(theme.SP_L)
        layout.addWidget(section_header("最近项目"))

        self.project_list = QListWidget()
        self.project_list.itemDoubleClicked.connect(self._on_open)
        layout.addWidget(self.project_list, 1)

        self.empty_box = empty_state(
            "还没有工程项目",
            "新建项目开始核对，或先体验匿名演示熟悉完整流程",
            # 页面底部保留唯一主按钮；空状态中的快捷入口使用默认次级样式，
            # 避免同一页出现两个同等权重的“新建项目”主操作。
            [("新建项目", "")])
        layout.addWidget(self.empty_box)
        for btn in self.empty_box.findChildren(QPushButton):
            if btn.text() == "新建项目":
                btn.clicked.connect(self._on_new)

        btn_row = QHBoxLayout()
        open_btn = QPushButton("打开项目目录…")
        open_btn.setObjectName("btnTertiary")
        open_btn.clicked.connect(self._on_open_dir)
        refresh_btn = QPushButton("刷新")
        refresh_btn.setObjectName("btnTertiary")
        refresh_btn.clicked.connect(self.refresh_projects)
        btn_row.addWidget(open_btn)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch(1)
        demo_btn = QPushButton("体验匿名演示")
        demo_btn.setToolTip("一键创建演示项目并导入完全合成的匿名演示数据，三分钟走完主流程")
        demo_btn.clicked.connect(self._on_demo)
        btn_row.addWidget(demo_btn)
        new_btn = QPushButton("新建项目")
        new_btn.setObjectName("btnPrimary")
        new_btn.clicked.connect(self._on_new)
        btn_row.addWidget(new_btn)
        layout.addLayout(btn_row)
        return central

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_projects()

    def closeEvent(self, event):
        settings = QSettings("CostGuard", "CostGuard")
        settings.setValue(_GEOMETRY_KEY, self.saveGeometry())
        if self._conn:
            self._conn.close()
        super().closeEvent(event)

    # ---- 动作 ----
    def refresh_projects(self):
        self.project_list.clear()
        settings = QSettings("CostGuard", "CostGuard")
        for info in project_model.list_projects():
            # 列表项保留项目名作为 UserRole/双击入口；卡片补充审核摘要。
            item = QListWidgetItem(info.name)
            item.setToolTip(str(info.path))
            item.setData(Qt.UserRole, info)
            self.project_list.addItem(item)
            snapshot = self._project_snapshot(info)
            last_opened = settings.value(
                f"projects/{info.project_id}/last_opened", "尚未打开"
            )
            card = QWidget()
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 8, 12, 8)
            card_layout.setSpacing(3)
            card_head = QHBoxLayout()
            card_title = QLabel(info.name)
            card_title.setStyleSheet(
                "font-size: 15px; font-weight: 600; background: transparent;"
            )
            card_head.addWidget(card_title)
            card_head.addStretch(1)
            continue_btn = QPushButton("继续审核")
            continue_btn.setProperty("btnLink", True)
            continue_btn.clicked.connect(lambda _checked=False, i=item: self._on_open(i))
            card_head.addWidget(continue_btn)
            card_layout.addLayout(card_head)
            period_line = QLabel(
                f"最近打开：{last_opened}　·　对上结算 {snapshot['upward']} 期　·　"
                f"对下结算 {snapshot['downward']} 期"
            )
            period_line.setStyleSheet("color: #667085; background: transparent;")
            card_layout.addWidget(period_line)
            status_line = QLabel(
                f"待确认事项：工作表 {snapshot['pending_sheets']}　·　"
                f"高风险 {snapshot['high']}　·　待确认匹配 {snapshot['matches']}　·　"
                f"最新校核：{snapshot['latest']}"
            )
            status_line.setStyleSheet("color: #667085; background: transparent;")
            card_layout.addWidget(status_line)
            item.setSizeHint(QSize(0, 78))
            self.project_list.setItemWidget(item, card)
        has_items = self.project_list.count() > 0
        self.project_list.setVisible(has_items)
        self.empty_box.setVisible(not has_items)

    @staticmethod
    def _project_snapshot(info: project_model.ProjectInfo) -> dict[str, str | int]:
        """只读读取项目卡片所需摘要，不触发迁移或修改项目数据库。"""
        fallback: dict[str, str | int] = {
            "upward": 0, "downward": 0, "pending_sheets": 0,
            "high": 0, "matches": 0, "latest": "尚未校核",
        }
        db = Path(info.workspace_path) / "project.db"
        if not db.is_file():
            return fallback
        conn = None
        try:
            uri = f"{db.resolve().as_uri()}?immutable=1&mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            periods = conn.execute(
                """SELECT COALESCE(direction, 'unknown') AS direction, COUNT(*) AS c
                   FROM settlement_periods WHERE project_id=? GROUP BY COALESCE(direction, 'unknown')""",
                (info.project_id,),
            ).fetchall()
            counts = {row["direction"]: int(row["c"]) for row in periods}
            fallback["upward"] = counts.get("upward", 0)
            fallback["downward"] = counts.get("downward", 0)
            fallback["high"] = int(conn.execute(
                """SELECT COUNT(*) AS c FROM anomalies
                   WHERE project_id=? AND severity='high' AND status IN ('open', 'deferred')""",
                (info.project_id,),
            ).fetchone()["c"])
            fallback["matches"] = int(conn.execute(
                "SELECT COUNT(*) AS c FROM matches WHERE project_id=? AND status='pending'",
                (info.project_id,),
            ).fetchone()["c"])
            fallback["pending_sheets"] = int(conn.execute(
                """SELECT COUNT(*) AS c
                   FROM raw_sheets rs JOIN parse_batches pb ON pb.id=rs.batch_id
                   JOIN source_files sf ON sf.id=pb.file_id
                   WHERE sf.project_id=? AND rs.period_id IS NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM audit_log al
                         WHERE al.project_id=sf.project_id
                           AND al.target='sheet:'||rs.id
                           AND al.action='confirm_sheet_non_settlement_role'
                     )""", (info.project_id,),
            ).fetchone()["c"])
            latest = conn.execute(
                """SELECT verification_level FROM crosscheck_results
                   WHERE project_id=? ORDER BY checked_at DESC, id DESC LIMIT 1""",
                (info.project_id,),
            ).fetchone()
            level_zh = {
                "sufficient": "校核充分", "findings": "校核有发现", "insufficient": "校核不充分",
            }
            if latest:
                fallback["latest"] = level_zh.get(latest["verification_level"], "待复核")
        except (OSError, sqlite3.Error):
            pass
        finally:
            if conn is not None:
                conn.close()
        return fallback

    def _on_demo(self):
        answer = QMessageBox.question(
            self, "体验匿名演示",
            "将自动创建「匿名演示项目」并导入完全合成的匿名演示数据\n"
            "（对上结算、对下结算、合同摘录，全部为程序生成的虚构内容）。\n\n"
            "随后可依次体验：双向校核 → 异常检测 → 匹配复核 → 成果导出。\n\n"
            "是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            return
        try:
            info = demo_core.provision_demo_project()
        except demo_core.DemoProvisionError as exc:
            QMessageBox.warning(self, "体验匿名演示", str(exc))
            return
        except Exception:  # noqa: BLE001 — UI 层兜底提示
            QMessageBox.warning(
                self, "体验匿名演示",
                "演示项目创建失败，请检查本地工作空间后重试。",
            )
            return
        self._open(info)

    def _on_new(self):
        dlg = NewProjectDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        name, root = dlg.values()
        if not name:
            QMessageBox.warning(self, "新建项目", "项目名称不能为空")
            return
        try:
            info = project_model.create_project(name, root)
        except project_model.ProjectError as exc:
            QMessageBox.warning(self, "新建项目", _project_error_text(exc))
            return
        try:
            # 自定义工作空间必须持久登记；否则本次窗口能打开，重启后项目列表却
            # 只扫描默认目录，看起来像“记录丢失”。登记只写软件配置，不动项目库。
            project_model.set_workspace_root(root)
        except OSError:
            QMessageBox.warning(
                self,
                "工作空间未记住",
                f"项目已经安全创建在：\n{info.workspace_path}\n\n"
                "但工作空间配置保存失败。\n"
                "本次仍可继续使用；下次启动可通过“打开项目目录”重新打开。",
            )
        self._open(info)

    def _on_open(self, item: QListWidgetItem):
        self._open(item.data(Qt.UserRole))

    def _on_open_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择项目目录", str(project_model.workspace_root()))
        if not d:
            return
        try:
            info, conn = project_model.open_project(Path(d))
        except project_model.ProjectError as exc:
            QMessageBox.warning(self, "打开项目", _project_error_text(exc))
            return
        try:
            project_model.set_workspace_root(Path(d).parent)
        except OSError:
            QMessageBox.warning(
                self,
                "工作空间未记住",
                "项目已经成功打开，但工作空间配置保存失败。\n"
                "下次启动可再次通过“打开项目目录”打开，项目数据不会因此被删除。",
            )
        self._enter_workbench(info, conn)

    def _open(self, info: project_model.ProjectInfo):
        try:
            info, conn = project_model.open_project(Path(info.workspace_path))
        except project_model.ProjectError as exc:
            QMessageBox.warning(self, "打开项目", _project_error_text(exc))
            return
        self._enter_workbench(info, conn)

    def _enter_workbench(self, info: project_model.ProjectInfo, conn):
        if self._conn:
            self._conn.close()
        self._conn = conn
        self._project = info
        QSettings("CostGuard", "CostGuard").setValue(
            f"projects/{info.project_id}/last_opened",
            datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        page = WorkbenchPage(conn, info, info.workspace_path, on_back=self._back_to_projects)
        old = self.stack.widget(PAGE_WORKBENCH)
        self.stack.removeWidget(old)
        if old is not None:
            old.deleteLater()
        self.stack.insertWidget(PAGE_WORKBENCH, page)
        self.stack.setCurrentIndex(PAGE_WORKBENCH)
        self.statusBar().showMessage(f"当前项目：{info.name}")
        self.statusBar().setToolTip(f"数据结构版本：{info.schema_version}")

    def _back_to_projects(self):
        self.stack.setCurrentIndex(PAGE_PROJECTS)
        self.refresh_projects()
        self.statusBar().showMessage("已返回项目列表")


def run_gui() -> int:
    from PySide6.QtWidgets import QApplication

    from costguard.ui.theme import apply_theme

    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    app.setApplicationName("CostGuard")
    app.setOrganizationName("CostGuard")
    icon = load_app_icon()
    if icon is not None:
        app.setWindowIcon(icon)
    win = MainWindow()
    win.show()
    return app.exec()


def main() -> int:
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
