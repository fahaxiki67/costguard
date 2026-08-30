"""CostGuard 桌面应用入口。

`costguard` 命令（pyproject gui-scripts）或 `python -m costguard` 启动。
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from costguard.ui.main_window import MainWindow, load_app_icon


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("CostGuard")
    app.setApplicationDisplayName("CostGuard")
    app.setOrganizationName("CostGuard")
    icon = load_app_icon()
    if icon is not None:
        app.setWindowIcon(icon)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
