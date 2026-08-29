"""CostGuard 桌面应用入口。

`costguard` 命令（pyproject gui-scripts）或 `python -m costguard` 启动。
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from costguard import ui


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("CostGuard")
    app.setApplicationDisplayName("CostGuard")
    app.setOrganizationName("CostGuard")
    win = ui.main_window.MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
