"""PySide6 壳冒烟测试（offscreen）。"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


def test_main_window_constructs():
    from PySide6.QtWidgets import QApplication

    from costguard.ui.main_window import MainWindow

    _app = QApplication.instance() or QApplication([])
    win = MainWindow()
    assert win.windowTitle().startswith("CostGuard")
    win.close()


def test_new_project_dialog_values():
    from PySide6.QtWidgets import QApplication

    from costguard.ui.main_window import NewProjectDialog

    _app = QApplication.instance() or QApplication([])
    dlg = NewProjectDialog()
    dlg.name_edit.setText("冒烟项目")
    name, root = dlg.values()
    assert name == "冒烟项目"


def test_installed_entrypoint_constructs_main_window(monkeypatch):
    """真实 `costguard` 启动入口必须直接构造主窗口，不能依赖隐式子模块属性。"""
    from PySide6.QtWidgets import QApplication

    from costguard import app as app_entry

    shown = []

    class FakeWindow:
        def show(self):
            shown.append(True)

    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(app_entry, "MainWindow", FakeWindow)
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    assert app_entry.main() == 0
    assert shown == [True]
