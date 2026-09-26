"""金额与数量计算唯一入口（ADR-004）。

纪律：
- 金额/数量/单价/税率一律 Decimal，禁止 float 进入金额路径；
- 舍入统一 ROUND_HALF_UP（工程结算惯例）；
- 本模块是 core 里唯一允许 Decimal 上下文调整的位置。

上下文确定性（CG-03）：
- 所有真正的金额运算（乘、加、除、quantize、差）都在 ``MONEY_CONTEXT``
  的局部副本内执行（``localcontext``），结果只由输入决定，与调用方线程、
  线程池或调用方临时修改的进程精度无关；
- ``getcontext().prec = 34`` 只是导入线程的兼容默认值：run_contract 把它
  作为运行环境记录（历史口径），模块外部的非金额路径 Decimal 运算仍沿用
  该环境；金额正确性不再依赖这一进程全局状态；
- 参数复制历史口径：prec=34、上下文舍入 ROUND_HALF_EVEN（round2 仍显式
  ROUND_HALF_UP）、默认 Emax/Emin 与陷阱（InvalidOperation/DivisionByZero/
  Overflow 显式拒绝，超范围直接抛错，不产生无限/静默值）。
"""
from __future__ import annotations

import re
from decimal import (
    ROUND_HALF_UP,
    Context,
    Decimal,
    InvalidOperation,
    getcontext,
    localcontext,
)

getcontext().prec = 34  # 兼容默认：仅记录环境口径（见模块 docstring），金额运算不依赖它

ZERO = Decimal("0")
TWO_PLACES = Decimal("0.01")

# 集中金额上下文（CG-03）：参数保持修复前的主线程有效口径。
MONEY_CONTEXT = Context(prec=34)

# 去除空白、货币符号和币种后缀。千分位逗号不在此处删除：必须先通过
# 严格千分位校验（CG-02），避免 "1,2,3"→123、"12,34"→1234 的歧义静默解析。
_NUM_CLEAN_RE = re.compile(r"[\s¥￥$€£]|人民币|元(?=$)")
# 严格千分位：1-3 位开头，其后每组恰好 3 位，小数部分不带逗号。
_THOUSANDS_RE = re.compile(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?")


class NotANumberError(ValueError):
    """输入无法解析为数值（调用方应标记'待补资料/不可比'，禁止补 0）。"""


def _strip_validated_thousands(s: str, original: str) -> str:
    """对含逗号的字符串做严格千分位校验，合法才剥逗号（CG-02）。

    "1,234,567.89" 合法；"1,2,3"、"12,34"、",123"、"123," 属歧义格式，
    统一抛 NotANumberError 并保留原文，由调用方转待补资料/待复核，
    绝不猜测数值。
    """
    if "," not in s:
        return s
    if not _THOUSANDS_RE.fullmatch(s):
        raise NotANumberError(f"ambiguous thousands separators: {original!r}")
    return s.replace(",", "")


def _decimal_or_nan(s: str, original: str, what: str) -> Decimal:
    """构造 Decimal；任何底层 InvalidOperation 统一转项目约定的错误。"""
    try:
        return Decimal(s)
    except InvalidOperation:
        raise NotANumberError(f"cannot parse {what}: {original!r}") from None


def to_decimal(value) -> Decimal:
    """把 Excel 单元格值安全转为 Decimal。

    支持: int/float/Decimal/str；str 允许千分位（严格 3 位分组校验）、
    货币符号、全角、括号负数 "(1,234.56)"、百分号 "13%"→0.13(仅当
    percent=True)。歧义千分位（如 "1,2,3"、"12,34"）不猜测，抛
    NotANumberError 保留原文转待确认。
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
        s = _strip_validated_thousands(s, value)
        if not re.fullmatch(r"\d*\.?\d*", s) or s in {"", "."}:
            raise NotANumberError(f"cannot parse: {value!r}")
        d = _decimal_or_nan(s, value, "")
        return -d if negative else d
    raise NotANumberError(f"unsupported type: {type(value)!r}")


def to_percent(value) -> Decimal:
    """'13%' / '0.13' / 13(percent_number=True) → Decimal('0.13')。"""
    if isinstance(value, str) and value.strip().endswith("%"):
        s = _NUM_CLEAN_RE.sub("", value.strip()[:-1])
        s = _strip_validated_thousands(s, value)
        if not re.fullmatch(r"\d*\.?\d*", s) or s in {"", "."}:
            raise NotANumberError(f"cannot parse percent: {value!r}")
        d = _decimal_or_nan(s, value, "percent")
        with localcontext(MONEY_CONTEXT):
            return d / Decimal("100")
    return to_decimal(value)


def to_percent_number(value) -> Decimal:
    """'13' 或 13 → Decimal('0.13')（Excel 中税率常以数字 13 表示）。"""
    d = to_decimal(value)
    with localcontext(MONEY_CONTEXT):
        return d / Decimal("100")


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
    with localcontext(MONEY_CONTEXT):
        return _require_decimal(quantity, "quantity") * _require_decimal(
            unit_price, "unit_price"
        )


def round2(d: Decimal) -> Decimal:
    """结算惯例：保留两位，ROUND_HALF_UP。"""
    with localcontext(MONEY_CONTEXT):
        return _require_decimal(d, "amount").quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def money_add(values) -> Decimal:
    total = ZERO
    with localcontext(MONEY_CONTEXT):
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
    with localcontext(MONEY_CONTEXT):
        return total_amount / total_quantity


def within_tolerance(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    """差异是否在容差内（仅用于报告分级，禁止用于调平数据）。"""
    with localcontext(MONEY_CONTEXT):
        left = _require_decimal(a, "left")
        right = _require_decimal(b, "right")
        return abs(left - right) <= _require_decimal(tol, "tolerance")


def diff(a: Decimal, b: Decimal) -> Decimal:
    with localcontext(MONEY_CONTEXT):
        return _require_decimal(a, "left") - _require_decimal(b, "right")
