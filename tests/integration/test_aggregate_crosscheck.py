"""Phase 3 结算累计与双向校核测试。"""
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "synthetic_test_data"))

from generator import make_multi_period  # noqa: E402

from costguard.core.engine import aggregate, crosscheck, settlement_io  # noqa: E402

D = Decimal


@pytest.fixture()
def project_multi(tmp_path):
    from costguard.core.models import project as pm

    info = pm.create_project("累计-多期", tmp_path / "ws")
    info, conn = pm.open_project(Path(info.workspace_path))
    pdir = Path(info.workspace_path)
    src = pdir.parent / "multi.xlsx"
    make_multi_period(src, periods=3)
    report = settlement_io.import_settlement_file(conn, info.project_id, pdir, src)
    yield info, conn, report
    conn.close()


class TestAggregate:
    def test_cum_matches_db_sum(self, project_multi):
        info, conn, report = project_multi
        aggs = aggregate.aggregate_project(conn, info.project_id)
        # 每个清单的累计 = DB 内同组各期非小计行之和（同源核对）
        rows = conn.execute(
            """SELECT li.quantity, li.amount, li.code, li.name, sp.period_no FROM line_items li
               JOIN settlement_periods sp ON sp.id=li.period_id
               WHERE sp.project_id=? AND (li.flags_json NOT LIKE '%"subtotal": true%')""",
            (info.project_id,),
        ).fetchall()
        assert rows
        by_key = {}
        for r in rows:
            key = aggregate.group_key_of(r["code"], r["name"] or "")
            by_key.setdefault(key, [D(0), D(0)])
            if r["quantity"]:
                by_key[key][0] += D(r["quantity"])
            if r["amount"]:
                by_key[key][1] += D(r["amount"])
        for agg in aggs:
            q, a = by_key[agg.item_key]
            assert agg.cum_qty == q
            assert agg.cum_amount == a
        assert len(aggs) >= 6

    def test_missing_item_incomplete(self, project_multi):
        """第2期漏项 → 该清单状态 incomplete（待补资料），不静默。"""
        info, conn, report = project_multi
        aggs = aggregate.aggregate_project(conn, info.project_id)
        # 生成器第2期删掉最后一项（水泥砂浆楼地面），该项应有 incomplete 警示
        flagged = [a for a in aggs if "第2期" in "".join(a.warnings)]
        assert flagged, "missing period-2 rows must produce warnings"
        assert all(a.status in ("incomplete", "ok") for a in aggs)

    def test_subtotal_excluded(self, project_multi):
        info, conn, report = project_multi
        aggs = aggregate.aggregate_project(conn, info.project_id)
        names = {a.name for a in aggs}
        assert "小计" not in names

    def test_wavg_defined(self, project_multi):
        info, conn, report = project_multi
        aggs = aggregate.aggregate_project(conn, info.project_id)
        for agg in aggs:
            if agg.cum_qty and agg.cum_qty != 0 and agg.cum_amount is not None:
                assert agg.wavg_price is not None
                # 加权平均应在 min/max 期单价之间（合成数据单价恒定→等于单价）
                assert agg.wavg_price == agg.cum_amount / agg.cum_qty

    def test_total_amount_eq_period_sum(self, project_multi):
        info, conn, report = project_multi
        aggs = aggregate.aggregate_project(conn, info.project_id)
        total = sum((a.cum_amount for a in aggs if a.cum_amount is not None), D(0))
        rows = conn.execute(
            """SELECT li.amount FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
               WHERE sp.project_id=? AND li.flags_json NOT LIKE '%"subtotal": true%'""",
            (info.project_id,),
        ).fetchall()
        db_total = sum((D(r["amount"]) for r in rows if r["amount"]), D(0))
        assert total == db_total


class TestCrossCheck:
    def test_multi_period_match(self, project_multi):
        """A/B 两条独立路径结果一致（同一文件同网格）。"""
        info, conn, report = project_multi
        results = crosscheck.run_crosscheck(conn, info.project_id, [report.period_no])
        res = results[0]
        assert res.status == "match", f"A={res.path_a_total} B={res.path_b_total}"
        assert res.path_a_total == res.path_b_total
        # C 校验：原表小计行存在
        assert res.raw_subtotal is not None

    def test_diff_not_silently_balanced(self, project_multi):
        """人为制造 A≠B（改一条明细金额）→ 必须报 diff，不得自动调平。"""
        info, conn, report = project_multi
        with conn:
            conn.execute(
                """UPDATE line_items SET amount='99999.00' WHERE id=(
                   SELECT li.id FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
                   WHERE sp.project_id=? AND li.flags_json NOT LIKE '%"subtotal": true%' LIMIT 1)""",
                (info.project_id,),
            )
        results = crosscheck.run_crosscheck(conn, info.project_id, [report.period_no])
        assert results[0].status == "diff"
        assert results[0].diff_ab != 0
        # evidence 已生成
        ev = conn.execute(
            "SELECT COUNT(*) AS c FROM evidence WHERE project_id=? AND kind='cross_check'",
            (info.project_id,),
        ).fetchone()
        assert ev["c"] >= 1
