"""ADR-004：金额计算纪律测试。

float 严禁进入金额路径 —— property-based 验证 Decimal 入口的行为边界。
"""
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from costguard.core.engine.money import (
    NotANumberError,
    diff,
    money_add,
    money_mul,
    round2,
    to_decimal,
    to_percent,
    to_percent_number,
    weighted_avg_price,
    within_tolerance,
)

D = Decimal


class TestToDecimal:
    @given(
        int_part=st.integers(min_value=0, max_value=10**12),
        frac=st.integers(min_value=0, max_value=99),
    )
    def test_plain_numbers_roundtrip(self, int_part, frac):
        s = f"{int_part}.{frac:02d}"
        assert to_decimal(s) == D(s)

    def test_thousands_separator(self):
        assert to_decimal("1,234,567.89") == D("1234567.89")

    def test_currency_symbols(self):
        assert to_decimal("¥12,345.67") == D("12345.67")
        assert to_decimal("￥99.5") == D("99.5")
        assert to_decimal("人民币 1,000.00") == D("1000.00")

    def test_negative_forms(self):
        assert to_decimal("-123.45") == D("-123.45")
        assert to_decimal("(1,234.56)") == D("-1234.56")
        assert to_decimal("－50") == D("-50") if False else True  # 全角负号不支持，明确报错
        with pytest.raises(NotANumberError):
            to_decimal("－50")

    def test_whitespace_and_empty(self):
        assert to_decimal("  42 ") == D("42")
        for bad in ["", " ", "-", "—", "/", "N/A", "#N/A"]:
            with pytest.raises(NotANumberError):
                to_decimal(bad)

    def test_text_is_not_silently_zero(self):
        """核心纪律：脏文本不得补 0，必须显式报错。"""
        for bad in ["abc", "12a3", "1.2.3", "12%", "元", "None", "nan"]:
            with pytest.raises(NotANumberError):
                to_decimal(bad)

    def test_float_via_repr(self):
        assert to_decimal(0.1) == D("0.1")
        assert to_decimal(42) == D("42")

    def test_percent(self):
        assert to_percent("13%") == D("0.13")
        assert to_percent("6.72%") == D("0.0672")
        assert to_percent_number(13) == D("0.13")
        assert to_percent_number("9") == D("0.09")


class TestMoneyOps:
    def test_mul_exact(self):
        assert round2(money_mul(D("1234.5"), D("56.78"))) == D("70094.91")

    @given(
        q=st.decimals(min_value=-10**9, max_value=10**9, places=3),
        p=st.decimals(min_value=-10**6, max_value=10**6, places=4),
    )
    def test_mul_linearity_before_rounding(self, q, p):
        """舍入前的 Decimal 乘加必须精确线性（比较与聚合永不经过 float）。"""
        assert money_add([money_mul(q, p), money_mul(q, p)]) == money_mul(q, p) * 2
        assert money_mul(q, p) == money_mul(p, q)
        assert money_mul(q, Decimal(0)) == Decimal(0)

    @given(vals=st.lists(st.decimals(min_value=-10**10, max_value=10**10, places=2), max_size=30))
    def test_add_matches_fraction_exact_sum(self, vals):
        """Decimal 加法与有理数精确求和一致（无二进制误差）。"""
        from fractions import Fraction

        assert money_add(vals) == sum((Fraction(v) for v in vals), Fraction(0))

    @given(vals=st.lists(st.decimals(min_value=-10**10, max_value=10**10, places=2), max_size=50))
    def test_add_order_independent(self, vals):
        assert money_add(vals) == money_add(list(reversed(vals)))

    def test_round2_half_up(self):
        """结算惯例：半入。0.005 → 0.01（Python 内建 round 是银行家舍入，禁用）。"""
        assert round2(D("2.675")) == D("2.68")
        assert round2(D("0.005")) == D("0.01")
        assert round2(D("-0.005")) == D("-0.01")

    def test_wavg(self):
        assert weighted_avg_price(D("1000.00"), D("10")) == D("100")
        with pytest.raises(ZeroDivisionError):
            weighted_avg_price(D("1000"), D("0"))

    def test_tolerance_reporting_only(self):
        assert within_tolerance(D("100.00"), D("100.005"), D("0.01"))
        assert not within_tolerance(D("100.00"), D("100.02"), D("0.01"))

    def test_diff(self):
        assert diff(D("10.00"), D("9.99")) == D("0.01")
