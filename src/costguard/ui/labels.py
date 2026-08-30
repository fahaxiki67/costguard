"""业务语言中文化映射（UI 展示层专用）。

纪律：
- 数据库与代码内部枚举原样保留，本模块只做 UI 显示转换；
- 每个映射必须提供 fallback（未知值原样返回，不崩溃、不丢信息）；
- 内部英文代码通过 Tooltip 保留可追溯性。
"""
from __future__ import annotations

# 方向
DIRECTION_ZH = {"upward": "对上", "downward": "对下", "unknown": "未标记"}

# 异常严重度 → (中文, 徽章级别)
SEVERITY_ZH = {
    "high": ("高", "danger"),
    "medium": ("中", "warning"),
    "low": ("低", "neutral"),
    "info": ("提示", "info"),
}

# 异常规则 → 业务中文名（未知规则回退原值）
RULE_ZH = {
    "qty_price_amount_mismatch": "工程量×单价与合价不一致",
    "rounding_difference": "舍入差异",
    "text_number_in_value_col": "数值列存在文本数字",
    "unparsed_number": "数值无法解析",
    "formula_no_cache": "公式缺少缓存值",
    "formula_error": "公式错误",
    "duplicate_item": "重复明细",
    "suspected_duplicate_settlement": "疑似重复结算",
    "price_changed": "单价变化",
    "price_abnormal_change": "单价异常变化",
    "tax_rate_changed": "税率变化",
    "tax_mode_mixed": "计税口径混用",
    "unit_changed": "计量单位变化",
    "qty_sudden_change": "工程量突变",
    "missing_key_fields": "关键字段缺失",
    "summary_mismatch": "汇总金额不一致",
    "summary_missing_data": "汇总数据缺失",
    "negative_quantity": "负数工程量",
    "orphan_numeric_row": "无名称数值行",
    "same_code_diff_name": "同编码不同名称",
    "same_name_diff_code": "同名称不同编码",
    "large_round_amount": "大额整数金额",
    "header_needs_review": "表头识别待复核",
    "missing_columns": "缺少必需列",
    "missing_key_column": "缺少关键列",
    "merged_cells_in_data": "数据区合并单元格",
    "hidden_rows": "隐藏行",
    "hidden_cols": "隐藏列",
}

# 匹配方法
METHOD_ZH = {
    "code_exact": "编码完全匹配",
    "name_exact": "名称完全匹配",
    "name_merge": "名称归并",
    "fuzzy_name": "名称相似",
    "alias": "已确认别名",
    "none": "—",
}

# 匹配级别（五档）
LEVEL_ZH = {
    "confirmed": "规则完全匹配（待人工确认）",
    "probable": "高概率匹配",
    "suspected": "疑似匹配",
    "incomparable": "不可比",
    "pending_data": "待补资料",
}

# 处理状态
ITEM_STATUS_ZH = {
    "pending": "待确认",
    "confirmed": "已确认",
    "open": "待处理",
    "resolved": "已处理",
}

# 工作表门控状态 → (中文, 徽章级别)
SHEET_STATE_ZH = {
    "parsed": ("已解析", "success"),
    "needs_role_review": ("待人工角色确认", "warning"),
    "non_settlement_form": ("非结算表单", "neutral"),
    "no_header": ("无表头（需完整人工映射）", "warning"),
}

LEVEL_SHORT_ZH = {
    "confirmed": "完全匹配",
    "probable": "高概率匹配",
    "suspected": "疑似匹配",
    "incomparable": "不可比",
    "pending_data": "待补资料",
}

WORKBENCH_TABS = ["期次概览", "清单明细", "异常检测", "匹配复核", "成果导出"]


def rule_zh(rule_id: str) -> str:
    """规则 ID → 业务中文名；动态规则（rule_error_*）与非未知值原样返回。"""
    return RULE_ZH.get(rule_id, rule_id)


def parse_group_key(group_key: str) -> str:
    """组键 → 业务友好显示（方向 · 对象）。

    例：'downward:code:0101' → '对下 · 编码 0101'；'downward:name:小计' →
    '对下 · 名称 小计'；'pending:orphan' → '待补资料 · 缺失名称/编码'。
    原始组键始终保留在 Tooltip（由调用方处理）。
    """
    parts = group_key.split(":", 2)
    if len(parts) == 3 and parts[0] in DIRECTION_ZH:
        kind = {"code": "编码", "name": "名称"}.get(parts[1], parts[1])
        return f"{DIRECTION_ZH[parts[0]]} · {kind} {parts[2]}"
    if group_key.startswith("pending:"):
        return "待补资料 · 缺失名称/编码"
    return group_key
