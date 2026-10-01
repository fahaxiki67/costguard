"""PySide6 壳冒烟测试（offscreen）。"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


def test_main_window_constructs():
    from PySide6.QtWidgets import QApplication

    from jiadun.ui.main_window import MainWindow

    _app = QApplication.instance() or QApplication([])
    win = MainWindow()
    assert win.windowTitle().startswith("Jiadun（价盾）")
    win.close()


def test_new_project_dialog_values():
    from PySide6.QtWidgets import QApplication

    from jiadun.ui.main_window import NewProjectDialog

    _app = QApplication.instance() or QApplication([])
    dlg = NewProjectDialog()
    dlg.name_edit.setText("冒烟项目")
    name, root = dlg.values()
    assert name == "冒烟项目"


def test_home_mascot_loads_from_source_and_bundle(tmp_path, monkeypatch):
    import shutil
    import sys

    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication, QLabel

    from jiadun import branding
    from jiadun.platform import resources
    from jiadun.ui.main_window import MainWindow

    _app = QApplication.instance() or QApplication([])
    source = resources.home_mascot_path()
    image = QImage(str(source))
    assert not image.isNull()
    assert image.hasAlphaChannel()
    assert image.pixelColor(0, 0).alpha() == 0
    bundle = tmp_path / branding.RESOURCE_DIR_NAME
    bundle.mkdir()
    bundled_image = bundle / source.name
    shutil.copyfile(source, bundled_image)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resources.home_mascot_path() == bundled_image
    win = MainWindow()
    try:
        mascot = win.findChild(QLabel, "homeMascot")
        assert mascot is not None
        assert not mascot.pixmap().isNull()
        assert mascot.accessibleName() == "两只小狗：小白与鸡毛"
        assert win.stack.indexOf(mascot.parentWidget()) == 0
    finally:
        win.close()
    bundled_image.unlink()
    with pytest.raises(FileNotFoundError, match="双小狗"):
        resources.home_mascot_path()


def test_installed_entrypoint_constructs_main_window(monkeypatch):
    """真实 `jiadun` 启动入口必须直接构造主窗口，不能依赖隐式子模块属性。"""
    from PySide6.QtWidgets import QApplication

    from jiadun import app as app_entry

    shown = []

    class FakeWindow:
        def show(self):
            shown.append(True)

    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(app_entry, "MainWindow", FakeWindow)
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    assert app_entry.main() == 0
    assert shown == [True]
