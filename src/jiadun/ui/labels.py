"""业务语言中文化映射（UI 展示层专用）。

纪律：
- 数据库与代码内部枚举原样保留，本模块只做 UI 显示转换；
- 每个映射必须提供安全 fallback（未知值不直接暴露内部枚举）；
- 内部英文代码通过 Tooltip 保留可追溯性。
"""
from __future__ import annotations

import re

from jiadun.core.labels import DIRECTION_ZH

# 方向
# 异常严重度 → (中文, 徽章级别)
SEVERITY_ZH = {
    "high": ("高", "danger"),
    "medium": ("中", "warning"),
    "low": ("低", "neutral"),
    "info": ("提示", "info"),
}

# 异常规则 → 业务中文名（未知规则使用安全中文兜底，原值放 Tooltip）
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
    "formula_semantics_mismatch": "公式语义与来源值不一致",
    "formula_control_semantics_mismatch": "控制金额公式语义与来源值不一致",
    "formula_untrusted_cache": "公式缓存未经过本程序重算验证",
    "filter_visibility_unknown": "筛选后的实际可见行无法确认",
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
    # P1-06 Finding 闭环统一状态；旧状态继续保留用于历史库兼容。
    "new": "新发现",
    "pending_review": "待复核",
    "confirmed_issue": "已确认问题",
    "legitimate_business": "合理业务情形",
    "pending_data": "待补资料",
    "rectified": "已整改",
    "closed": "已关闭",
    "historical": "历史结果——当前数据或运行契约已经变化，不参与当前结论",
    "pending": "待确认",
    "confirmed": "已确认",
    "open": "待处理",
    "resolved": "已处理",
    "verified_no_issue": "已核实无问题",
    "supplemented": "已补资料",
    "corrected": "已修正",
    "deferred": "暂不处理",
    "stale": "已失效（历史）",
    "superseded": "已被新结果替代",
}

# 审计动作只在用户可见的处理历史中显示中文；原始 action 仍可通过高级信息追溯。
AUDIT_ACTION_ZH = {
    "update_finding_status": "更新审核问题状态",
    "resolve_anomaly": "处理审核问题",
    "confirm_match": "确认匹配",
    "override_match": "修正匹配级别",
    "set_direction": "标记结算方向",
    "confirm_sheet_role": "确认工作表角色并抽取",
    "confirm_sheet_non_settlement_role": "确认工作表仅作存证",
    "import_file": "导入文件",
}

# 工作表门控状态 → (中文, 徽章级别)
SHEET_STATE_ZH = {
    "confirmed": ("已确认", "success"),
    "pending": ("待确认", "warning"),
    "non_business": ("非业务表", "neutral"),
    "parse_failed": ("解析失败", "danger"),
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

WORKBENCH_TABS = ["期次概览", "清单明细", "审核问题中心", "匹配复核", "成果导出"]

SUBJECT_TYPE_ZH = {
    "line_item": "清单明细",
    "period": "期次",
    "sheet": "工作表",
    "project": "项目",
    "contract_doc": "合同文档",
}


def rule_zh(rule_id: str) -> str:
    """规则 ID → 业务中文名。

    未知规则仍须可追溯，但普通业务列不能裸露内部代码；调用方应把原始
    ``rule_id`` 放在 Tooltip 或高级信息中。
    """
    return RULE_ZH.get(rule_id, "其他审核问题")


def severity_zh(severity: str | None) -> str:
    """严重度的安全显示文案；未知值不直接泄露内部枚举。"""
    return SEVERITY_ZH.get(severity or "", ("其他", "neutral"))[0]


def item_status_zh(status: str | None) -> str:
    """处理状态的安全显示文案。"""
    return ITEM_STATUS_ZH.get(status or "", "待人工确认")


def subject_type_zh(subject_type: str | None) -> str:
    """证据对象类型的安全中文显示。"""
    return SUBJECT_TYPE_ZH.get(subject_type or "", "其他对象")


def audit_action_zh(action: str | None) -> str:
    """审计动作 → 普通用户可读的中文；未知动作不泄露内部枚举。"""
    return AUDIT_ACTION_ZH.get(action or "", "其他审核操作")


def method_zh(method: str | None) -> str:
    """匹配方法的安全显示文案。"""
    return METHOD_ZH.get(method or "", "其他匹配方式")


def level_short_zh(level: str | None) -> str:
    """匹配级别的安全短文案。"""
    return LEVEL_SHORT_ZH.get(level or "", "待人工确认")


def normalize_business_text(value: str | None) -> str:
    """归一化历史记录中的可见业务文案。

    旧版本可能已经把短方向词写入异常消息或证据摘要。重新打开项目时，
    普通界面仍应使用完整的业务术语；内部 rule code、状态值不在此函数中
    原样回显。
    """
    text = str(value or "")
    text = text.replace("[对上]", "[对上结算]").replace("[对下]", "[对下结算]")
    text = text.replace("对上双向校核", "对上结算双向校核")
    text = text.replace("对下双向校核", "对下结算双向校核")
    text = re.sub(r"(?<!结算)对上(?=第|清单|编码|名称|各期)", "对上结算", text)
    text = re.sub(r"(?<!结算)对下(?=第|清单|编码|名称|各期)", "对下结算", text)
    return text


def parse_group_key(group_key: str) -> str:
    """组键 → 业务友好显示（方向 · 对象）。

    例：'downward:code:0101' → '对下结算 · 编码 0101'；'downward:name:小计' →
    '对下结算 · 名称 小计'；'pending:orphan' → '待补资料 · 缺失名称/编码'。
    原始组键始终保留在 Tooltip（由调用方处理）。
    """
    parts = group_key.split(":", 2)
    if len(parts) == 3 and parts[0] in DIRECTION_ZH:
        kind = {"code": "编码", "name": "名称"}.get(parts[1], parts[1])
        return f"{DIRECTION_ZH[parts[0]]} · {kind} {parts[2]}"
    if len(parts) >= 2 and parts[0] in {"code", "name"}:
        kind = {"code": "编码", "name": "名称"}[parts[0]]
        return f"{kind} {':'.join(parts[1:])}"
    if group_key.startswith("pending:"):
        return "待补资料 · 缺失名称/编码"
    return "其他匹配对象"
