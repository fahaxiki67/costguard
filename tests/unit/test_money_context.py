"""CG-03 回归：Decimal 上下文确定性。

预审缺陷（2026-09-26）：money.py 仅在模块导入时 ``getcontext().prec = 34``，
金额运算实际依赖环境上下文——主线程 34 位、工作线程/线程池默认 28 位、
调用方临时改精度即改变结果。探针实测同一输入（100/3）得到 3 种结果。

修复口径：
- money.py 所有真正运算（乘/加/除/quantize/差/百分比除 100）在集中局部
  ``MONEY_CONTEXT``（prec=34，参数复制修复前主线程口径）内执行；
- aggregate.py 的累计加法与金额差值同样经集中上下文；
- ``getcontext().prec = 34`` 保留为导入线程兼容默认（run_contract 记录
  运行环境用），金额正确性不再依赖它；
- 超范围（Overflow/InvalidOperation/DivisionByZero 陷阱）显式拒绝。
"""
import concurrent.futures
import decimal
import threading
from decimal import Decimal

from jiadun.core.engine.money import (
    MONEY_CONTEXT,
    diff,
    money_add,
    money_mul,
    round2,
    to_percent,
    to_percent_number,
    weighted_avg_price,
)

D = Decimal

AMOUNT = D("100")
QTY = D("3")
EXPECTED_WAVG = "33.33333333333333333333333333333333"


def _wavg_str() -> str:
    return str(weighted_avg_price(AMOUNT, QTY))


def test_weighted_avg_price_deterministic_in_worker_thread():
    """工作线程默认上下文（prec=28）下结果必须与主线程一致。"""
    box: dict[str, str] = {}

    def run():
        box["wavg"] = _wavg_str()

    t = threading.Thread(target=run)
    t.start()
    t.join()
    assert box["wavg"] == EXPECTED_WAVG
    assert _wavg_str() == EXPECTED_WAVG  # 主线程参照


def test_weighted_avg_price_deterministic_in_thread_pool():
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: _wavg_str(), range(8)))
    assert results == [EXPECTED_WAVG] * 8


def test_results_invariant_under_caller_precision_change():
    """调用方临时降低/抬高精度不得改变金额运算结果。"""
    mul_a, mul_b = D("1234.5678901234"), D("98.7654321098")
    add_a, add_b = D("1.234567890123456789012345678901234"), D("9.876543210987654321098765432109876")
    diff_a, diff_b = D("100000000000000000000.01"), D("0.005")
    pct_raw = "13.123456789012345678901234567890123%"
    before = {
        "wavg": _wavg_str(),
        "mul": str(money_mul(mul_a, mul_b)),
        "add": str(money_add([add_a, add_b])),
        "diff": str(diff(diff_a, diff_b)),
        "pct": str(to_percent(pct_raw)),
        "pctn": str(to_percent_number(pct_raw[:-1])),
    }
    old = decimal.getcontext().prec
    try:
        for prec in (2, 5, 28, 60):
            decimal.getcontext().prec = prec
            assert _wavg_str() == before["wavg"]
            assert str(money_mul(mul_a, mul_b)) == before["mul"]
            assert str(money_add([add_a, add_b])) == before["add"]
            assert str(diff(diff_a, diff_b)) == before["diff"]
            assert str(to_percent(pct_raw)) == before["pct"]
            assert str(to_percent_number(pct_raw[:-1])) == before["pctn"]
            assert str(round2(D("2.675"))) == "2.68", "round2 半入口径不得随环境漂移"
    finally:
        decimal.getcontext().prec = old


def test_round2_invariant_under_precision_change():
    """quantize 的精度闸门在集中上下文内：低精度环境下大数不误判 InvalidOperation。"""
    big = D("123456789012345678901234567890.125")  # 33 位有效数字
    old = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 10
        assert str(round2(big)) == "123456789012345678901234567890.13"
    finally:
        decimal.getcontext().prec = old


def test_money_context_parameters_match_historical_behavior():
    """集中上下文复制修复前主线程口径：prec=34、默认舍入与陷阱。"""
    assert MONEY_CONTEXT.prec == 34
    assert str(MONEY_CONTEXT.rounding) == "ROUND_HALF_EVEN"
    assert decimal.InvalidOperation in MONEY_CONTEXT.traps
    assert decimal.DivisionByZero in MONEY_CONTEXT.traps
    assert decimal.Overflow in MONEY_CONTEXT.traps
    # 金额正确性不再依赖进程全局精度的同时，主线程兼容默认仍为 34
    assert decimal.getcontext().prec == 34


def test_out_of_range_explicitly_rejected():
    """超范围显式拒绝：溢出抛 Overflow、除零抛 ZeroDivisionError，不产生静默值。"""
    raised_overflow = False
    try:
        money_mul(D("1E999999"), D("10"))
    except decimal.Overflow:
        raised_overflow = True
    assert raised_overflow, "超范围必须显式拒绝，不得静默输出无限/截断值"

    try:
        weighted_avg_price(D("100"), D("0"))
    except ZeroDivisionError:
        pass
    else:
        raise AssertionError("除零必须显式拒绝（转换为不可比）")


def test_aggregate_deterministic_under_caller_precision_change(tmp_path):
    """累计链路（加法+除法）在调用方改变精度时结果逐字段一致。"""
    from tests.integration.test_aggregate_unit_compatibility import _build_project

    info, conn = _build_project(tmp_path, [
        (1, "downward", "C1", "清单A", "", "吨", "1", "1000", "1000"),
        (2, "downward", "C1", "清单A", "", "吨", "2", "1000.005", "2000.01"),
    ])
    try:
        baseline = _aggregate_snapshot(conn, info.project_id)
        old = decimal.getcontext().prec
        try:
            for prec in (2, 5, 60):
                decimal.getcontext().prec = prec
                assert _aggregate_snapshot(conn, info.project_id) == baseline, (
                    f"prec={prec} 时累计结果漂移"
                )
        finally:
            decimal.getcontext().prec = old
        assert baseline[0][1] == "3", "同单位累计数量保持"
    finally:
        conn.close()


def _aggregate_snapshot(conn, project_id):
    from jiadun.core.engine import aggregate

    aggs = aggregate.aggregate_project(
        conn, project_id, direction="downward", persist_derived_flags=False
    )
    return [
        (a.item_key,
         str(a.cum_qty), str(a.cum_amount), str(a.wavg_price),
         str(a.raw_cum_amount), str(a.calculated_cum_amount),
         a.amount_source, a.status,
         tuple(sorted((pid, str(pp["qty"]), str(pp["wavg_price"]),
                       str(pp["effective_amount"]))
                      for pid, pp in a.per_period.items())))
        for a in aggs
    ]
