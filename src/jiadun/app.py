"""价盾（Jiadun）桌面应用入口。

`jiadun` 命令（pyproject gui-scripts）或 `python -m jiadun` 启动。
"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from jiadun import branding
from jiadun.ui.main_window import MainWindow, load_app_icon
from jiadun.ui.theme import apply_theme


def main() -> int:
    # Qt6 默认启用高 DPI 缩放；这里显式钉死取整策略，保证 100%–200% 缩放下
    # 布局按物理像素精确渲染，不因四舍五入产生模糊或错位。
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app)
    app.setApplicationName(branding.PRODUCT_SLUG)
    app.setApplicationDisplayName(branding.PRODUCT_DISPLAY_NAME)
    app.setOrganizationName(branding.ORGANIZATION_NAME)
    icon = load_app_icon()
    if icon is not None:
        app.setWindowIcon(icon)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
