"""项目结论的统一状态语义。

状态码保持稳定、适合程序判断；``label`` 是面向工作台和报告的中文文案。
本模块只根据已经存在的确定性事实推导状态，不计算金额，也不替代业务确认或
审批。任何证据范围未闭合的情况都只能降级，不能因为局部结果相等而升级。
"""
from __future__ import annotations

from typing import Any

SHEET_LABELS = {
    "confirmed": "已确认",
    "pending": "待确认",
    "non_business": "非业务表",
    "parse_failed": "解析失败",
}

SHEET_STATE_CODES = frozenset(SHEET_LABELS)

PERIOD_LABELS = {
    "sufficient": "校核充分",
    "findings": "校核有发现",
    "insufficient": "校核不充分",
}

DIRECTION_LABELS = {
    "complete": "完整有效",
    "partial": "部分有效",
    "none": "当前无有效结果",
}

PROJECT_LABELS = {
    "can_conclude": "可形成项目结论",
    "conditional": "有条件结论",
    "cannot_conclude": "不可形成项目结论",
}


def period_state(
    verification_status: str,
    *,
    periods_total: int,
    periods_unchecked: int,
    run_available: bool = True,
) -> dict[str, Any]:
    """将摘要内部的期次校核结果映射为三档业务状态。

    ``verification_status`` 仍可保留 ``unavailable``、``not_started`` 等技术
    状态供兼容调用方使用；对外的期次状态只有交接文件规定的三档。
    """
    if (
        run_available
        and periods_total > 0
        and periods_unchecked == 0
        and verification_status == "sufficient"
    ):
        code = "sufficient"
    elif verification_status == "findings":
        code = "findings"
    else:
        code = "insufficient"
    return {
        "code": code,
        "label": PERIOD_LABELS[code],
        "periods_total": int(periods_total),
        "periods_unchecked": int(max(0, periods_unchecked)),
    }


def direction_state(
    *,
    direction: str | None = None,
    periods_total: int,
    periods_checked: int,
    sufficient: int,
    findings: int,
    insufficient: int,
    run_available: bool,
    coverage_complete: bool,
) -> dict[str, Any]:
    """根据一个方向的当前期次结果推导方向状态。"""
    if direction is not None and direction not in {"upward", "downward"}:
        # unknown 只是未完成方向标记，不是一个可对外确认的结算方向；即使
        # 其局部 A/B/C 恰好完整，也不得把它提升为“完整有效”。
        code = "none"
    elif not run_available or periods_total <= 0 or periods_checked <= 0:
        code = "none"
    elif (
        periods_checked == periods_total
        and sufficient == periods_total
        and findings == 0
        and insufficient == 0
        and coverage_complete
    ):
        code = "complete"
    else:
        code = "partial"
    return {
        "code": code,
        "label": DIRECTION_LABELS[code],
        "periods_total": int(periods_total),
        "periods_checked": int(periods_checked),
        "periods_unchecked": int(max(0, periods_total - periods_checked)),
        "levels": {
            "sufficient": int(sufficient),
            "findings": int(findings),
            "insufficient": int(insufficient),
        },
    }


def project_state(
    *,
    source_files: int,
    period_count: int,
    run_available: bool,
    current_periods_checked: int,
    period_code: str,
    direction_states: dict[str, dict[str, Any]],
    detection_complete: bool,
    aggregate_complete: bool,
    pending_count: int,
    manifest_blocked: bool,
    sheet_parse_failed_count: int = 0,
    evidence_complete: bool = True,
    # 金额单位、尺度、币种和含税口径没有明确确认时必须 fail-closed。调用方
    # 若省略该参数，不能把未知口径误当成已确认并升级为项目级结论。
    amount_unit_confirmed: bool = False,
) -> dict[str, Any]:
    """推导项目级三档状态。

    ``can_conclude`` 只表示证据条件允许形成项目结论，不表示业务确认或审批
    已完成。没有当前有效结果、没有资料或运行级不可用时直接返回
    ``cannot_conclude``；有部分结果或可修复缺口时返回 ``conditional``。
    """
    reasons: list[str] = []
    if not source_files:
        reasons.append("no_source_files")
    if not period_count:
        reasons.append("no_periods")
    if not run_available:
        reasons.append("run_unavailable")
    if current_periods_checked <= 0 and period_count:
        reasons.append("no_current_period_results")
    if sheet_parse_failed_count:
        # 解析失败的工作表可能包含尚未进入结算模型的业务资料；在没有
        # 人工确认其角色或补齐解析证据前，项目不能形成项目级结论。
        reasons.append("sheet_parse_failed")
    if not evidence_complete:
        # 结果表即使有数值和状态，也不能在主结论缺少 Evidence 时显示为
        # 可形成项目结论；所有正式结论都必须能回溯到运行内证据链。
        reasons.append("evidence_incomplete")
    if reasons:
        code = "cannot_conclude"
    else:
        # 方向快照必须与项目级期次数量勾稽。即使每个已传入方向都显示
        # complete，也不能用缺失方向或缩窄的 periods_total/checked 形成
        # can_conclude。这里不推断方向名称，只校验调用方已声明的完整计数。
        direction_period_totals: list[int] = []
        direction_period_checked: list[int] = []
        direction_count_invalid = False
        for _direction, item in direction_states.items():
            if not isinstance(item, dict):
                direction_count_invalid = True
                continue
            total = item.get("periods_total")
            checked = item.get("periods_checked")
            if (
                isinstance(total, bool)
                or not isinstance(total, int)
                or total < 0
                or isinstance(checked, bool)
                or not isinstance(checked, int)
                or checked < 0
                or checked > total
            ):
                direction_count_invalid = True
                continue
            direction_period_totals.append(total)
            direction_period_checked.append(checked)
        if direction_count_invalid:
            reasons.append("direction_period_count_invalid")
        elif sum(direction_period_totals) != period_count:
            reasons.append("direction_period_count_mismatch")
        elif sum(direction_period_checked) != current_periods_checked:
            reasons.append("direction_checked_count_mismatch")
        invalid_direction_snapshot = any(
            key not in {"upward", "downward"}
            or not isinstance(value, dict)
            or value.get("code") not in DIRECTION_LABELS
            for key, value in direction_states.items()
        )
        if period_count > 0 and not direction_states:
            # 方向快照缺失时不能把 ``any(empty)`` 的数学结果误当成全部方向
            # 已完成；这是未来 schema、损坏数据库或调用方漏传参数时的安全闸门。
            reasons.append("direction_scope_incomplete")
        if invalid_direction_snapshot:
            # 方向字典是项目级状态快照的一部分；未知方向或未来状态码不能
            # 因为字典非空而绕过 fail-closed 闸门。
            reasons.append("direction_scope_incomplete")
        if period_code != "sufficient":
            reasons.append("period_scope_incomplete")
        if not detection_complete:
            reasons.append("detection_coverage_incomplete")
        if not aggregate_complete:
            reasons.append("aggregate_coverage_incomplete")
        if pending_count:
            reasons.append("pending_human_review")
        if manifest_blocked:
            reasons.append("manifest_incomplete")
        if any(
            not isinstance(item, dict) or item.get("code") != "complete"
            for item in direction_states.values()
        ):
            reasons.append("direction_scope_incomplete")
        if not amount_unit_confirmed:
            # 数字可以先按 Decimal 完成技术复算，但金额单位/尺度/币种或
            # 含税口径未确认时，项目级状态不得升级为无条件结论；这属于
            # 可补齐的业务口径缺口，因此保留为“有条件结论”。
            reasons.append("amount_unit_unconfirmed")
        code = "conditional" if reasons else "can_conclude"

    return {
        "code": code,
        "label": PROJECT_LABELS[code],
        "reason_codes": reasons,
        "business_confirmation": "not_requested",
        "approval": "not_requested",
    }
