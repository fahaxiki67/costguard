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
        return value
    if isinstance(value, bool):
        raise NotANumberError("bool is not a number")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Excel 读出的 float 先走字符串化，避免二进制误差固化
        return Decimal(repr(value))
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


def money_mul(quantity: Decimal, unit_price: Decimal) -> Decimal:
    """合价 = 数量 × 单价（结果不主动舍入，比较时用 round2 后的值）。"""
    return quantity * unit_price


def round2(d: Decimal) -> Decimal:
    """结算惯例：保留两位，ROUND_HALF_UP。"""
    return d.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def money_add(values) -> Decimal:
    total = ZERO
    for v in values:
        total += v
    return total


def weighted_avg_price(total_amount: Decimal, total_quantity: Decimal) -> Decimal:
    """加权平均单价 = 累计金额 / 累计数量。数量为 0 或无效时不定义。

    Raises ZeroDivisionError / NotANumber 由调用方转换为'不可比'。
    """
    if total_quantity == ZERO:
        raise ZeroDivisionError("total quantity is zero: weighted average undefined")
    return total_amount / total_quantity


def within_tolerance(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    """差异是否在容差内（仅用于报告分级，禁止用于调平数据）。"""
    return abs(a - b) <= tol


def diff(a: Decimal, b: Decimal) -> Decimal:
    return a - b
