"""统一视觉系统（主题 token + 全局 QSS）。

设计定位：现代 macOS 专业工具 + 企业审计软件。克制、可信、高信息密度。
规则：
- 颜色只在此处定义；页面代码不得散落 setStyleSheet（objectName 语义样式除外）；
- 风险颜色仅用于语义标签（浅底 Badge），不做装饰、不做整行高饱和；
- 不硬编码字体：macOS/Windows 使用系统字体，中文走系统中文字体
  （PingFang SC / Microsoft YaHei 由系统回退保证）；
- 间距体系 4/8/12/16/24px；表格行高 32px。
"""
from __future__ import annotations

import sys

from PySide6.QtGui import QFont, QFontDatabase

# ---- 颜色 token ----
BG = "#F6F7F9"          # 页面背景
SURFACE = "#FFFFFF"     # 内容面板背景
TEXT = "#101828"        # 主文字
TEXT_SECONDARY = "#667085"  # 次级文字
TEXT_DISABLED = "#98A2B3"
BORDER = "#E4E7EC"
PRIMARY = "#2563EB"     # 主色（专业蓝）
PRIMARY_HOVER = "#1D4ED8"
PRIMARY_PRESSED = "#1E40AF"
PRIMARY_SOFT = "#EFF4FF"  # 自动识别候选/选中底
SELECTED_ROW = "#EFF4FF"
HOVER_ROW = "#F8FAFC"

SUCCESS = "#067647"
SUCCESS_SOFT = "#ECFDF3"
WARNING = "#B54708"
WARNING_SOFT = "#FFFAEB"
DANGER = "#B42318"
DANGER_SOFT = "#FEF3F2"
NEUTRAL_SOFT = "#F2F4F7"
INFO_SOFT = "#EFF8FF"

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

QToolTip {{
    background: {TEXT};
    color: #FFFFFF;
    border: none;
    padding: 4px 8px;
}}

QFrame#fileDropZone {{
    background: {SURFACE};
    color: {TEXT_SECONDARY};
    border: 1px dashed #B8C4D6;
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
QPushButton:hover {{ border-color: {PRIMARY}; color: {PRIMARY}; }}
QPushButton:pressed {{ background: {BG}; }}
QPushButton:disabled {{ color: {TEXT_DISABLED}; border-color: {BORDER}; background: {NEUTRAL_SOFT}; }}
QPushButton:focus {{ border: 1px solid {PRIMARY}; }}

QPushButton#btnPrimary {{
    background: {PRIMARY};
    color: #FFFFFF;
    border: 1px solid {PRIMARY};
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
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 3px 6px;
    min-height: 26px;
    selection-background-color: {PRIMARY_SOFT};
    selection-color: {TEXT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus {{
    border: 1px solid {PRIMARY};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}

/* ---- 表格（本软件的核心控件）---- */
QTableWidget {{
    background: {SURFACE};
    alternate-background-color: #FCFCFD;
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    selection-background-color: {SELECTED_ROW};
    selection-color: {TEXT};
}}
QTableWidget::item {{ padding: 2px 6px; }}
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
QTabBar::tab:selected {{ color: {PRIMARY}; border-bottom: 2px solid {PRIMARY}; font-weight: 600; }}
QTabBar::tab:hover:!selected {{ color: {TEXT}; }}

/* ---- 列表 ---- */
QListWidget {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    outline: 0;
}}
QListWidget::item {{ padding: 6px 8px; border-bottom: 1px solid #F2F4F7; }}
QListWidget::item:hover {{ background: {HOVER_ROW}; }}
QListWidget::item:selected {{ background: {SELECTED_ROW}; color: {TEXT}; }}

/* ---- 滚动条：窄、不抢视觉 ---- */
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: #D0D5DD; border-radius: 5px; min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_DISABLED}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #D0D5DD; border-radius: 5px; min-width: 32px; }}

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
    apply_app_font(app)
    app.setStyleSheet(build_qss())
