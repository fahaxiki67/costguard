"""统一视觉系统（主题 token + 全局 QSS）。

设计定位：参考暖红橙与炭黑的统一深色工作台，保持审计数据可读。
规则：
- 颜色只在此处定义；页面代码不得散落 setStyleSheet（objectName 语义样式除外）；
- 风险颜色仅用于语义标签（低饱和深底 Badge），不做装饰、不做整行高饱和；
- 不硬编码字体：macOS/Windows 使用系统字体，中文走系统中文字体
  （PingFang SC / Microsoft YaHei 由系统回退保证）；
- 间距体系 4/8/12/16/24px；表格行高 32px。
"""
from __future__ import annotations

import sys

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

# ---- 颜色 token ----
BG = "#1C1412"          # 暖炭黑页面背景
SURFACE = "#281E1A"     # 内容面板
PANEL = "#231916"       # 总览与分组的中间层
TEXT = "#FFF1E6"        # 暖白主文字
TEXT_SECONDARY = "#CBB4A5"
TEXT_DISABLED = "#99877B"
BORDER = "#594239"
PRIMARY = "#FF9C7A"     # 珊瑚橙强调文字与焦点
PRIMARY_FILL = "#A83822"  # 深红橙按钮底，保证暖白文字对比度
PRIMARY_HOVER = "#BB4329"
PRIMARY_PRESSED = "#8F2D1B"
PRIMARY_SOFT = "#4A2A21"
SELECTED_ROW = PRIMARY_SOFT
HOVER_ROW = "#352620"
ALTERNATE_ROW = "#2E231E"
SCROLL_HANDLE = "#806052"

SUCCESS = "#A6D4B5"
SUCCESS_SOFT = "#243A2D"
WARNING = "#FFD08A"
WARNING_SOFT = "#49351F"
DANGER = "#FFAAA0"
DANGER_SOFT = "#4B2623"
NEUTRAL_SOFT = "#352A24"
INFO_SOFT = PRIMARY_SOFT

# ---- 间距（4/8/12/16/24）----
SP_XS, SP_S, SP_M, SP_L, SP_XL = 4, 8, 12, 16, 24

ROW_HEIGHT = 32
BADGE_HEIGHT = 20


def preferred_font_family() -> str:
    """选择当前平台实际存在的统一界面字体。

    不把某一个中文字体硬编码进 QSS：不同系统可用字体不同，由这里按
    平台优先级选择并交给 Qt 继承到所有控件。找不到候选时使用系统字体
    列表中的第一个可用族，避免落到不存在的 ``Sans Serif`` 别名。
    """
    available = set(QFontDatabase.families())
    if sys.platform == "darwin":
        candidates = (
            "PingFang SC", "Hiragino Sans GB", "Heiti SC", "Noto Sans CJK SC",
            "Arial Unicode MS", "Helvetica Neue",
        )
    elif sys.platform.startswith("win"):
        candidates = (
            "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC",
            "Segoe UI", "SimSun",
        )
    else:
        candidates = (
            "Noto Sans CJK SC", "Noto Sans SC", "WenQuanYi Zen Hei",
            "DejaVu Sans", "Liberation Sans",
        )
    for family in candidates:
        if family in available:
            return family
    if available:
        return sorted(available, key=str.casefold)[0]
    return QFont().family()


def apply_app_font(app) -> str:
    """设置 QApplication 级字体并返回实际选中的字体族。"""
    family = preferred_font_family()
    font = QFont(family)
    font.setPointSize(13)
    app.setFont(font)
    return family


def build_qss() -> str:
    """全局样式表。所有规则集中于此；objectName 级语义样式见个页面说明。"""
    return f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-size: 14px;
}}

QWidget:disabled {{ color: {TEXT_DISABLED}; }}
QWidget#projectOverview {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}

QWidget#overviewMetric {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}

QToolTip {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px 8px;
}}

QFrame#fileDropZone {{
    background: {SURFACE};
    color: {TEXT_SECONDARY};
    border: 1px dashed {BORDER};
    border-radius: 8px;
}}
QFrame#fileDropZone[dragActive="true"] {{
    background: {PRIMARY_SOFT};
    border: 2px dashed {PRIMARY};
}}
QLabel#fileDropZoneLabel {{
    color: {TEXT_SECONDARY};
    background: transparent;
}}

/* ---- 按钮：Primary / Secondary(默认) / Tertiary / Danger ---- */
QPushButton {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 14px;
    min-height: 26px;
}}
QPushButton:hover {{ background: {HOVER_ROW}; border-color: {PRIMARY}; color: {PRIMARY}; }}
QPushButton:pressed {{ background: {BG}; }}
QPushButton:disabled {{ color: {TEXT_DISABLED}; border-color: {BORDER}; background: {NEUTRAL_SOFT}; }}
QPushButton:focus {{ border: 1px solid {PRIMARY}; }}
QDialogButtonBox QPushButton {{ min-width: 72px; }}

QPushButton#btnPrimary {{
    background: {PRIMARY_FILL};
    color: {TEXT};
    border: 1px solid {PRIMARY_FILL};
    font-weight: 600;
}}
QPushButton#btnPrimary:hover {{ background: {PRIMARY_HOVER}; border-color: {PRIMARY_HOVER}; }}
QPushButton#btnPrimary:pressed {{ background: {PRIMARY_PRESSED}; }}
QPushButton#btnPrimary:disabled {{ background: {NEUTRAL_SOFT}; color: {TEXT_DISABLED}; border-color: {BORDER}; }}

QPushButton#btnTertiary {{
    background: transparent;
    border: none;
    color: {TEXT_SECONDARY};
    padding: 5px 8px;
}}
QPushButton#btnTertiary:hover {{ color: {PRIMARY}; background: {PRIMARY_SOFT}; }}

QPushButton#btnDanger {{
    background: {SURFACE};
    color: {DANGER};
    border: 1px solid {DANGER};
}}
QPushButton#btnDanger:hover {{ background: {DANGER_SOFT}; }}

QPushButton[btnLink="true"] {{
    background: transparent; border: none; color: {PRIMARY};
    padding: 2px 4px; text-decoration: underline;
}}

/* ---- 输入控件 ---- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QTextEdit, QPlainTextEdit {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 3px 6px;
    min-height: 26px;
    selection-background-color: {PRIMARY_SOFT};
    selection-color: {TEXT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus, QPlainTextEdit:focus, QDoubleSpinBox:focus, QDateEdit:focus {{
    border: 1px solid {PRIMARY};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QTextEdit:disabled, QPlainTextEdit:disabled {{
    background: {NEUTRAL_SOFT}; color: {TEXT_DISABLED};
}}
QComboBox QAbstractItemView {{
    background: {SURFACE}; color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {SELECTED_ROW}; selection-color: {TEXT};
}}
QMenu {{ background: {SURFACE}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 20px; }}
QMenu::item:selected {{ background: {SELECTED_ROW}; color: {TEXT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px; }}
QProgressBar {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 4px; text-align: center;
}}
QProgressBar::chunk {{ background: {PRIMARY_FILL}; border-radius: 3px; }}

/* ---- 表格（本软件的核心控件）---- */
QTableWidget, QTableView, QTreeView {{
    background: {SURFACE};
    alternate-background-color: {ALTERNATE_ROW};
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    selection-background-color: {SELECTED_ROW};
    selection-color: {TEXT};
}}
QTableWidget::item {{ padding: 2px 6px; }}
QTableWidget::item:selected {{ background: {SELECTED_ROW}; color: {TEXT}; }}
QTableWidget::item:hover {{ background: {HOVER_ROW}; }}
QHeaderView::section {{
    background: {NEUTRAL_SOFT};
    color: {TEXT_SECONDARY};
    font-weight: 600;
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 5px 6px;
}}
QTableCornerButton::section {{ background: {NEUTRAL_SOFT}; border: none; }}

/* ---- Tab：简洁下划线选中态，去默认框 ---- */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 0px;
    background: {SURFACE};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_SECONDARY};
    padding: 7px 16px;
    border: none;
    border-bottom: 2px solid transparent;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {PRIMARY_SOFT}; color: {PRIMARY};
    border-top-left-radius: 6px; border-top-right-radius: 6px;
    border-bottom: 2px solid {PRIMARY}; font-weight: 600;
}}
QTabBar::tab:hover:!selected {{ background: {HOVER_ROW}; color: {TEXT}; }}

/* ---- 列表 ---- */
QListWidget {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    outline: 0;
}}
QListWidget::item {{ padding: 6px 8px; border-bottom: 1px solid {BORDER}; }}
QListWidget::item:hover {{ background: {HOVER_ROW}; }}
QListWidget::item:selected {{ background: {SELECTED_ROW}; color: {TEXT}; }}

/* ---- 滚动条：窄、不抢视觉 ---- */
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {SCROLL_HANDLE}; border-radius: 5px; min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_DISABLED}; }}
/* PyQtDarkTheme 的轨道/滑块状态分离方式，来源见 docs/UI_THEME_REFERENCES.md。 */
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {SCROLL_HANDLE}; border-radius: 5px; min-width: 32px; }}
QScrollBar::handle:horizontal:hover {{ background: {TEXT_DISABLED}; }}
QScrollBar::handle:vertical:pressed, QScrollBar::handle:horizontal:pressed {{ background: {PRIMARY}; }}

QSplitter::handle {{ background: {BORDER}; }}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 12px;
    background: {SURFACE};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {TEXT_SECONDARY}; }}
QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: {TEXT_SECONDARY}; }}
"""


def apply_theme(app) -> None:
    """应用全局主题。仅在 QApplication 创建后调用一次。"""
    # Fusion + palette 让原生箭头、复选框和未被 QSS 覆盖的控件也遵循深色主题。
    app.setStyle("Fusion")
    palette = QPalette()
    for role, color in (
        (QPalette.Window, BG), (QPalette.WindowText, TEXT),
        (QPalette.Base, SURFACE), (QPalette.AlternateBase, ALTERNATE_ROW),
        (QPalette.Text, TEXT), (QPalette.Button, SURFACE),
        (QPalette.ButtonText, TEXT), (QPalette.Highlight, SELECTED_ROW),
        (QPalette.HighlightedText, TEXT), (QPalette.Link, PRIMARY),
        (QPalette.ToolTipBase, SURFACE), (QPalette.ToolTipText, TEXT),
        (QPalette.PlaceholderText, TEXT_SECONDARY),
        (QPalette.Light, BORDER), (QPalette.Midlight, NEUTRAL_SOFT),
        (QPalette.Mid, BORDER), (QPalette.Dark, BG), (QPalette.Shadow, BG),
        (QPalette.BrightText, TEXT),
    ):
        palette.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(TEXT_DISABLED))
    app.setPalette(palette)
    apply_app_font(app)
    app.setStyleSheet(build_qss())
