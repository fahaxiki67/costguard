"""轻量可复用 UI 组件：数据表格工厂 / 徽章 / 节头 / 空状态。

只做展示；不得在此引入业务逻辑或数据访问。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from jiadun.ui import theme


def make_data_table(headers: list[str], *,
                    stretch_cols: tuple[int, ...] = (),
                    right_cols: tuple[int, ...] = (),
                    center_cols: tuple[int, ...] = (),
                    fixed_widths: dict[int, int] | None = None) -> QTableWidget:
    """统一数据表格工厂。

    - 行高 32、隔行浅灰、hover、细网格（全局 QSS 提供配色）；
    - 列宽策略：stretch_cols 拉伸（描述/说明类）、fixed_widths 固定宽
      （数值/状态类，右对齐或居中）、其余 Interactive；
    - 表头由全局 QSS 定制（浅灰底/次级色/加粗）。
    """
    t = QTableWidget()
    t.setColumnCount(len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    t.setShowGrid(True)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(theme.ROW_HEIGHT)
    t.setWordWrap(False)
    t.setTextElideMode(Qt.ElideRight)
    fixed_widths = fixed_widths or {}
    for c in range(len(headers)):
        if c in stretch_cols:
            t.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        elif c in fixed_widths:
            t.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            t.setColumnWidth(c, fixed_widths[c])
        else:
            t.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
    return t


def fill_cell(table: QTableWidget, row: int, col: int, value, *,
               right: bool = False, center: bool = False,
               secondary: bool = False, mono: bool = False) -> None:
    """统一单元格填充：空值→次级色"—"；数值右对齐；证据/出处用次级色。"""
    from PySide6.QtGui import QColor, QFont

    text = "—" if value is None else str(value)
    item = QTableWidgetItem(text)
    if right:
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    elif center:
        item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
    if secondary:
        item.setForeground(QColor(theme.TEXT_SECONDARY))
    if mono:
        font = QFont()
        font.setStyleHint(QFont.Monospace)
        item.setFont(font)
    table.setItem(row, col, item)


def badge_item(text: str, kind: str = "neutral") -> QTableWidgetItem:
    """语义徽章单元格（浅底色 + 语义色文字，居中）。"""
    from PySide6.QtGui import QColor

    palette = {
        "danger": (theme.DANGER_SOFT, theme.DANGER),
        "warning": (theme.WARNING_SOFT, theme.WARNING),
        "success": (theme.SUCCESS_SOFT, theme.SUCCESS),
        "info": (theme.INFO_SOFT, theme.PRIMARY),
        "neutral": (theme.NEUTRAL_SOFT, theme.TEXT_SECONDARY),
    }
    bg, fg = palette.get(kind, (theme.NEUTRAL_SOFT, theme.TEXT_SECONDARY))
    item = QTableWidgetItem(text)
    item.setBackground(QColor(bg))
    item.setForeground(QColor(fg))
    item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
    return item


def section_header(text: str, parent: QWidget | None = None) -> QLabel:
    """区块标题：小号加粗主色前缀条 + 次级色文字。"""
    label = QLabel(text, parent)
    label.setStyleSheet(
        f"color: {theme.TEXT}; font-weight: 600; font-size: 13px;"
        f"padding-left: 6px; border-left: 3px solid {theme.PRIMARY}; background: transparent;")
    return label


def empty_state(title: str, description: str = "",
                actions: list[tuple[str, str]] | None = None) -> QWidget:
    """空状态占位：标题 + 说明 + [ (按钮文本, objectName) …]。

    返回容器；调用方把按钮信号接到业务动作。
    """
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.addStretch(2)
    t = QLabel(title)
    t.setAlignment(Qt.AlignCenter)
    t.setStyleSheet(f"color: {theme.TEXT}; font-size: 16px; font-weight: 600; background: transparent;")
    layout.addWidget(t)
    if description:
        d = QLabel(description)
        d.setAlignment(Qt.AlignCenter)
        d.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; background: transparent;")
        layout.addWidget(d)
    layout.addSpacing(theme.SP_M)
    if actions:
        from PySide6.QtWidgets import QHBoxLayout, QPushButton

        row = QHBoxLayout()
        row.addStretch(1)
        for text, obj_name in actions:
            btn = QPushButton(text)
            btn.setObjectName(obj_name)
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
    layout.addStretch(3)
    return box
