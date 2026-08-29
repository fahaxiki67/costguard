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
