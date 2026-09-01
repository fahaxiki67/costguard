"""金额与数量计算唯一入口（ADR-004）。

纪律：
- 金额/数量/单价/税率一律 Decimal，禁止 float 进入金额路径；
- 舍入统一 ROUND_HALF_UP（工程结算惯例）；
- 本模块是 core 里唯一允许 Decimal 上下文调整的位置。
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, getcontext

getcontext().prec = 34  # 足够容纳工程金额与中间乘积

ZERO = Decimal("0")
TWO_PLACES = Decimal("0.01")

_NUM_CLEAN_RE = re.compile(r"[,\s¥￥$€£]|人民币|元(?=$)")


class NotANumberError(ValueError):
    """输入无法解析为数值（调用方应标记'待补资料/不可比'，禁止补 0）。"""


def to_decimal(value) -> Decimal:
    """把 Excel 单元格值安全转为 Decimal。

    支持: int/float/Decimal/str；str 允许千分位、货币符号、全角、
    括号负数 "(1,234.56)"、百分号 "13%"→0.13(仅当 percent=True)。
    """
    if value is None:
        raise NotANumberError("empty value")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise NotANumberError("non-finite Decimal")
        return value
    if isinstance(value, bool):
        raise NotANumberError("bool is not a number")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Excel 读出的 float 先走字符串化，避免二进制误差固化
        parsed = Decimal(repr(value))
        if not parsed.is_finite():
            raise NotANumberError("non-finite float")
        return parsed
    if isinstance(value, str):
        s = value.strip()
        if not s or s in {"-", "—", "–", "/", "N/A", "n/a", "#N/A"}:
            raise NotANumberError(f"non-numeric placeholder: {value!r}")
        negative = False
        if s.startswith("(") and s.endswith(")"):
            negative = True
            s = s[1:-1]
        s = _NUM_CLEAN_RE.sub("", s)
        if s.startswith("-"):
            negative = True
            s = s[1:]
        elif s.startswith("+"):
            s = s[1:]
        if s.endswith("%"):
            raise NotANumberError("percent string must use to_percent()")
        if not re.fullmatch(r"\d*\.?\d*", s) or s in {"", "."}:
            raise NotANumberError(f"cannot parse: {value!r}")
        d = Decimal(s)
        return -d if negative else d
    raise NotANumberError(f"unsupported type: {type(value)!r}")


def to_percent(value) -> Decimal:
    """'13%' / '0.13' / 13(percent_number=True) → Decimal('0.13')。"""
    if isinstance(value, str) and value.strip().endswith("%"):
        s = _NUM_CLEAN_RE.sub("", value.strip()[:-1])
        if not re.fullmatch(r"\d*\.?\d*", s) or not s:
            raise NotANumberError(f"cannot parse percent: {value!r}")
        return Decimal(s) / Decimal("100")
    return to_decimal(value)


def to_percent_number(value) -> Decimal:
    """'13' 或 13 → Decimal('0.13')（Excel 中税率常以数字 13 表示）。"""
    return to_decimal(value) / Decimal("100")


def _require_decimal(value: object, field_name: str) -> Decimal:
    """核心运算的类型闸门。

    Excel/CSV 等外部边界可以由 ``to_decimal`` 把数值转换为 Decimal；进入
    业务乘加、比较和加权平均后不再接受 float、int 或非有限 Decimal，避免
    调用方无意中把二进制近似或 NaN/Infinity 带入结算结果。
    """
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} 必须是 Decimal，核心金额计算禁止 float/int")
    if not value.is_finite():
        raise ValueError(f"{field_name} 必须是有限 Decimal")
    return value


def money_mul(quantity: Decimal, unit_price: Decimal) -> Decimal:
    """合价 = 数量 × 单价（结果不主动舍入，比较时用 round2 后的值）。"""
    return _require_decimal(quantity, "quantity") * _require_decimal(unit_price, "unit_price")


def round2(d: Decimal) -> Decimal:
    """结算惯例：保留两位，ROUND_HALF_UP。"""
    return _require_decimal(d, "amount").quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def money_add(values) -> Decimal:
    total = ZERO
    for v in values:
        total += _require_decimal(v, "amount")
    return total


def weighted_avg_price(total_amount: Decimal, total_quantity: Decimal) -> Decimal:
    """加权平均单价 = 累计金额 / 累计数量。数量为 0 或无效时不定义。

    Raises ZeroDivisionError / NotANumber 由调用方转换为'不可比'。
    """
    total_amount = _require_decimal(total_amount, "total_amount")
    total_quantity = _require_decimal(total_quantity, "total_quantity")
    if total_quantity == ZERO:
        raise ZeroDivisionError("total quantity is zero: weighted average undefined")
    return total_amount / total_quantity


def within_tolerance(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    """差异是否在容差内（仅用于报告分级，禁止用于调平数据）。"""
    return abs(_require_decimal(a, "left") - _require_decimal(b, "right")) <= _require_decimal(tol, "tolerance")


def diff(a: Decimal, b: Decimal) -> Decimal:
    return _require_decimal(a, "left") - _require_decimal(b, "right")
