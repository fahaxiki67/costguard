"""CostGuard 主窗口：项目列表页 ↔ 工作台页（QStackedWidget）。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
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

from costguard.core.models import project as project_model
from costguard.ui.workbench import WorkbenchPage

PAGE_PROJECTS = 0
PAGE_WORKBENCH = 1


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
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
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
        central = QWidget()
        layout = QVBoxLayout(central)
        tip = QLabel("项目列表（双击打开）")
        layout.addWidget(tip)
        self.project_list = QListWidget()
        self.project_list.itemDoubleClicked.connect(self._on_open)
        layout.addWidget(self.project_list, 1)

        btn_row = QHBoxLayout()
        new_btn = QPushButton("新建项目")
        new_btn.clicked.connect(self._on_new)
        open_btn = QPushButton("打开项目目录…")
        open_btn.clicked.connect(self._on_open_dir)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh_projects)
        for b in (new_btn, open_btn, refresh_btn):
            btn_row.addWidget(b)
        layout.addLayout(btn_row)
        return central

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_projects()

    def closeEvent(self, event):
        if self._conn:
            self._conn.close()
        super().closeEvent(event)

    # ---- 动作 ----
    def refresh_projects(self):
        self.project_list.clear()
        for info in project_model.list_projects():
            item = QListWidgetItem(f"{info.name}    [{info.path}]")
            item.setData(Qt.UserRole, info)
            self.project_list.addItem(item)

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
            QMessageBox.warning(self, "新建项目", str(exc))
            return
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
            QMessageBox.warning(self, "打开项目", str(exc))
            return
        self._enter_workbench(info, conn)

    def _open(self, info: project_model.ProjectInfo):
        try:
            info, conn = project_model.open_project(Path(info.workspace_path))
        except project_model.ProjectError as exc:
            QMessageBox.warning(self, "打开项目", str(exc))
            return
        self._enter_workbench(info, conn)

    def _enter_workbench(self, info: project_model.ProjectInfo, conn):
        if self._conn:
            self._conn.close()
        self._conn = conn
        self._project = info
        page = WorkbenchPage(conn, info, info.workspace_path, on_back=self._back_to_projects)
        old = self.stack.widget(PAGE_WORKBENCH)
        self.stack.removeWidget(old)
        if old is not None:
            old.deleteLater()
        self.stack.insertWidget(PAGE_WORKBENCH, page)
        self.stack.setCurrentIndex(PAGE_WORKBENCH)
        self.statusBar().showMessage(f"当前项目：{info.name}（schema v{info.schema_version}）")

    def _back_to_projects(self):
        self.stack.setCurrentIndex(PAGE_PROJECTS)
        self.refresh_projects()
        self.statusBar().showMessage("已返回项目列表")


def run_gui() -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()
    return app.exec()


def main() -> int:
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
