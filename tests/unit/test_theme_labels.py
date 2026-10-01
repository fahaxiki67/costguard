"""主题与业务语言映射测试：视觉 token 完整性 + 中英映射完备性（防漏）。"""
from __future__ import annotations

import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from jiadun.ui import labels, theme


def test_theme_tokens_present():
    assert theme.BG == "#1C1412"
    assert theme.PRIMARY == "#FF9C7A"
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
    from PySide6.QtGui import QPalette

    assert app.palette().color(QPalette.Base).name().upper() == theme.SURFACE
    assert app.palette().color(QPalette.WindowText).name().upper() == theme.TEXT


def test_dark_theme_text_contrast():
    """防止深色主题中的正文、按钮与风险徽章失去可读性。"""
    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c * w for c, w in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    for foreground, background in (
        (theme.TEXT, theme.BG), (theme.TEXT, theme.SURFACE),
        (theme.TEXT_SECONDARY, theme.SURFACE), (theme.TEXT, theme.SELECTED_ROW),
        (theme.TEXT, theme.PRIMARY_FILL), (theme.TEXT, theme.PRIMARY_HOVER),
        (theme.TEXT, theme.PRIMARY_PRESSED), (theme.PRIMARY, theme.SURFACE),
        (theme.SUCCESS, theme.SUCCESS_SOFT), (theme.WARNING, theme.WARNING_SOFT),
        (theme.DANGER, theme.DANGER_SOFT),
    ):
        light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
        assert (light + 0.05) / (dark + 0.05) >= 4.5, (foreground, background)


def test_rule_zh_covers_all_rules_in_engine():
    """anomalies 引擎里出现的每个 rule_id 都必须有中文映射（防漏）。"""
    from jiadun.core.anomalies import rules as anomaly_rules

    src = (anomaly_rules.__file__ and open(anomaly_rules.__file__, encoding="utf-8").read()) or ""
    ids = set(re.findall(r'"([a-z][a-z0-9_]+_[a-z0-9_]+)"', src))
    # 只核对形如规则 id 的稳定子集（与 Finding 构造相邻的字符串），
    # 对动态前缀 rule_error_* 的兜底由 rule_zh fallback 覆盖。
    known_internal = {"cross_check", "tax_rate", "unit_price", "line_item", "details_sum",
                      "excl_tax", "incl_tax", "cached_value", "col_map_json", "period_id",
                      "sheet_id", "raw_value", "hidden_rows", "hidden_cols",
                      "header_row_lo", "header_row_hi", "needs_review", "item_ids",
                      "n_rows", "item_ids_json", "flags_json", "merged_ranges_json",
                      "hidden_rows_json", "hidden_cols_json",
                      # 解析结构证据字段（不是异常 rule_id），由
                      # rules.py 的 SQL/元数据扫描一并出现。
                      "auto_filter_ref", "blocks_verification", "cache_status",
                      "data_row_start", "data_row_end", "expected_column",
                      "expected_columns", "expected_field", "expected_fields",
                      "file_id", "filter_conditions", "filter_conditions_json",
                      "filter_state", "formula_cache_status", "merge_anchor_copy",
                      "original_name", "sheet_name", "table_ranges", "table_ranges_json",
                      "filter_visibility_unknown", "group_row", "subtotal", "grand_total"}
    rule_like = {i for i in ids if i not in known_internal}
    missing = [i for i in sorted(rule_like) if i not in labels.RULE_ZH]
    assert not missing, f"以下规则 ID 缺少中文映射：{missing}"


def test_labels_fallbacks_never_crash():
    assert labels.rule_zh("rule_error_xxx") == "其他审核问题"
    assert labels.parse_group_key("weird:key") == "其他匹配对象"
    assert labels.parse_group_key("pending:orphan") == "待补资料 · 缺失名称/编码"
    assert labels.parse_group_key("downward:code:0101") == "对下结算 · 编码 0101"
    assert labels.method_zh("unknown_method") == "其他匹配方式"
    assert labels.level_short_zh("unknown_level") == "待人工确认"
    assert labels.item_status_zh("verified_no_issue") == "已核实无问题"


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


def test_control_baseline_terminology_consistent_across_layers():
    """控制基准相关业务词在 UI 与导出两层映射中保持一致（防翻译漂移）。

    译文审核回归：control_baseline_cap / contract_risk / control_baseline
    同时出现在 ui/labels.py（问题中心）与 export/excel_export.py（审核底稿）
    的映射里，两层必须给出同一中文，避免同一发现两种叫法。
    """
    from jiadun.core.export import excel_export

    assert labels.rule_zh("control_baseline_cap") == "对上控制基准上限比较"
    assert labels.rule_zh("contract_risk") == "合同关键条款风险"
    assert labels.subject_type_zh("control_baseline") == "控制基准"
    assert excel_export.RULE_ZH_CN["control_baseline_cap"] == "对上控制基准上限比较"
    assert excel_export.RULE_ZH_CN["contract_risk"] == "合同关键条款风险"
    assert excel_export.SUBJECT_ZH["control_baseline"] == "控制基准"
