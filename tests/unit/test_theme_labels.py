"""主题与业务语言映射测试：视觉 token 完整性 + 中英映射完备性（防漏）。"""
from __future__ import annotations

import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from costguard.ui import labels, theme


def test_theme_tokens_present():
    assert theme.BG == "#F6F7F9"
    assert theme.PRIMARY == "#2563EB"
    assert theme.ROW_HEIGHT == 32
    for token in ("SUCCESS", "WARNING", "DANGER", "BORDER", "TEXT_SECONDARY"):
        assert getattr(theme, token).startswith("#")


def test_qss_contains_key_selectors():
    qss = theme.build_qss()
    for selector in ("QPushButton#btnPrimary", "QPushButton#btnTertiary",
                     "QPushButton#btnDanger", "QHeaderView::section",
                     "QTabBar::tab:selected", "QTableWidget::item:hover"):
        assert selector in qss, f"QSS 缺少 {selector}"
    assert "font-family" not in qss, "不得硬编码字体（系统字体由平台回退保证）"


def test_apply_theme_smoke(qt_app=None):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app)
    assert app.styleSheet(), "应用样式表不应为空"


def test_rule_zh_covers_all_rules_in_engine():
    """anomalies 引擎里出现的每个 rule_id 都必须有中文映射（防漏）。"""
    from costguard.core.anomalies import rules as anomaly_rules

    src = (anomaly_rules.__file__ and open(anomaly_rules.__file__, encoding="utf-8").read()) or ""
    ids = set(re.findall(r'"([a-z][a-z0-9_]+_[a-z0-9_]+)"', src))
    # 只核对形如规则 id 的稳定子集（与 Finding 构造相邻的字符串），
    # 对动态前缀 rule_error_* 的兜底由 rule_zh fallback 覆盖。
    known_internal = {"cross_check", "tax_rate", "unit_price", "line_item", "details_sum",
                      "excl_tax", "incl_tax", "cached_value", "col_map_json", "period_id",
                      "sheet_id", "raw_value", "hidden_rows", "hidden_cols",
                      "header_row_lo", "header_row_hi", "needs_review", "item_ids",
                      "n_rows", "item_ids_json", "flags_json", "merged_ranges_json",
                      "hidden_rows_json", "hidden_cols_json"}
    rule_like = {i for i in ids if i not in known_internal}
    missing = [i for i in sorted(rule_like) if i not in labels.RULE_ZH]
    assert not missing, f"以下规则 ID 缺少中文映射：{missing}"


def test_labels_fallbacks_never_crash():
    assert labels.rule_zh("rule_error_xxx") == "rule_error_xxx"
    assert labels.parse_group_key("weird:key") == "weird:key"
    assert labels.parse_group_key("pending:orphan") == "待补资料 · 缺失名称/编码"
    assert labels.parse_group_key("downward:code:0101") == "对下 · 编码 0101"


@pytest.mark.parametrize("method,expected", [
    ("code_exact", "编码完全匹配"),
    ("name_exact", "名称完全匹配"),
    ("name_merge", "名称归并"),
    ("fuzzy_name", "名称相似"),
    ("alias", "已确认别名"),
])
def test_method_zh(method, expected):
    assert labels.METHOD_ZH[method] == expected


@pytest.mark.parametrize("level,expected", [
    ("confirmed", "完全匹配"),
    ("probable", "高概率匹配"),
    ("suspected", "疑似匹配"),
    ("incomparable", "不可比"),
    ("pending_data", "待补资料"),
])
def test_level_short_zh(level, expected):
    assert labels.LEVEL_SHORT_ZH[level] == expected


@pytest.mark.parametrize("status,expected", [
    ("pending", "待确认"),
    ("open", "待处理"),
    ("resolved", "已处理"),
])
def test_item_status_zh(status, expected):
    assert labels.ITEM_STATUS_ZH[status] == expected
