"""CostGuard 主窗口（Phase 1 最小可运行壳）。

Phase 8 将扩展为完整工作台界面；本阶段提供：
- 项目列表 / 新建 / 打开；
- 文件导入（拖拽或按钮）进入当前项目 originals/；
- 状态栏显示当前项目与库版本。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
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
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from costguard.core.models import project as project_model
from costguard.core.models import source_file


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
        self.resize(920, 600)
        self._conn = None
        self._project = None

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
        import_btn = QPushButton("导入文件到当前项目…")
        import_btn.clicked.connect(self._on_import)
        for b in (new_btn, open_btn, refresh_btn, import_btn):
            btn_row.addWidget(b)
        layout.addLayout(btn_row)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

    # ---- 生命周期 ----
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
        if self._conn:
            self._conn.close()
        self._conn = conn
        self._project = info
        self.statusBar().showMessage(f"当前项目：{info.name}（schema v{info.schema_version}）")

    def _open(self, info: project_model.ProjectInfo):
        try:
            info, conn = project_model.open_project(Path(info.workspace_path))
        except project_model.ProjectError as exc:
            QMessageBox.warning(self, "打开项目", str(exc))
            return
        if self._conn:
            self._conn.close()
        self._conn = conn
        self._project = info
        self.statusBar().showMessage(f"当前项目：{info.name}（schema v{info.schema_version}）")

    def _on_import(self):
        if not (self._project and self._conn):
            QMessageBox.information(self, "导入文件", "请先打开一个项目")
            return
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择要导入的文件", "", "工程文件 (*.xlsx *.xlsm *.xls *.csv *.pdf *.docx *.doc *.png *.jpg *.jpeg)"
        )
        if not files:
            return
        ok, fail = 0, []
        for f in files:
            try:
                source_file.import_file(self._conn, self._project.project_id, Path(self._project.workspace_path), Path(f))
                ok += 1
            except source_file.SourceFileError as exc:
                fail.append(f"{Path(f).name}: {exc}")
        msg = f"已导入 {ok} 个文件（生成只读副本，原文件未改动）。"
        if fail:
            msg += "\n失败：\n" + "\n".join(fail)
        QMessageBox.information(self, "导入文件", msg)


def run_gui() -> int:
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()
    return app.exec()


def main() -> int:
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
