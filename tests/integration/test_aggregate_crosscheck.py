"""Phase 3 结算累计与双向校核测试。"""
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "synthetic_test_data"))

from generator import make_multi_period  # noqa: E402

from costguard.core.engine import aggregate, crosscheck, settlement_io  # noqa: E402

D = Decimal


def _make_amount_case(tmp_path, raw_amount=None):
    """构造含原始网格的最小结算项目，允许原始合价缺失或与公式不一致。"""
    from costguard.core.models import project as pm

    info = pm.create_project("金额计算反例", tmp_path / "ws")
    info, conn = pm.open_project(Path(info.workspace_path))
    with conn:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?,?,?,?)""",
            (info.project_id, 1, "第1期", "downward"),
        ).lastrowid
        file_id = conn.execute(
            """INSERT INTO source_files(project_id, original_path, stored_path,
               original_name, sha256, size_bytes, file_type, imported_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (info.project_id, "/amount-case.xlsx", "/amount-case.xlsx", "amount-case.xlsx",
             f"amount-case-{raw_amount}", 1, "xlsx", "2026"),
        ).lastrowid
        batch_id = conn.execute(
            """INSERT INTO parse_batches(file_id, parser, parsed_at, status)
               VALUES (?,?,?,?)""",
            (file_id, "test", "2026", "ok"),
        ).lastrowid
        sheet_id = conn.execute(
            """INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name, n_rows,
               n_cols, period_id) VALUES (?,?,?,?,?,?)""",
            (batch_id, 0, "第1期明细", 2, 5, period_id),
        ).lastrowid
        col_map = {"code": 1, "name": 2, "quantity": 3, "unit_price": 4, "amount": 5}
        conn.execute(
            """INSERT INTO table_headers(sheet_id, header_row_lo, header_row_hi,
               col_map_json, confidence, needs_review, data_row_start, data_row_end)
               VALUES (?,?,?,?,?,?,?,?)""",
            (sheet_id, 1, 1, json.dumps(col_map), 1.0, 0, 2, 2),
        )
        cells = [
            (sheet_id, 1, 1, "清单编码"), (sheet_id, 1, 2, "清单名称"),
            (sheet_id, 1, 3, "工程量"), (sheet_id, 1, 4, "综合单价"),
            (sheet_id, 1, 5, "合价"),
            (sheet_id, 2, 1, "C1"), (sheet_id, 2, 2, "清单A"),
            (sheet_id, 2, 3, "2"), (sheet_id, 2, 4, "100"),
        ]
        if raw_amount is not None:
            cells.append((sheet_id, 2, 5, raw_amount))
        conn.executemany(
            """INSERT INTO raw_cells(sheet_id, row, col, raw_value, cached_value, is_formula)
               VALUES (?,?,?,?,?,?)""",
            [(sid, row, col, value, value, 0) for sid, row, col, value in cells],
        )
        conn.execute(
            """INSERT INTO line_items(period_id, sheet_id, code, name, quantity, unit_price,
               amount, flags_json) VALUES (?,?,?,?,?,?,?,?)""",
            (period_id, sheet_id, "C1", "清单A", "2", "100", raw_amount,
             json.dumps({"row": 2})),
        )
    return info, conn, period_id


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
    def test_reaggregate_invalidates_previous_crosscheck_fields(self, tmp_path):
        """重新聚合后，旧 A/B/C 差异和证据必须失效，不能冒充当前校验。"""
        info, conn, period_id = _make_amount_case(tmp_path, raw_amount="250")
        try:
            agg = aggregate.aggregate_project(conn, info.project_id)[0]
            aggregate.persist_period_totals(conn, info.project_id, [agg])
            with conn:
                conn.execute(
                    """UPDATE period_totals SET
                       cross_check_status='control_diff', cross_check_diff='7', evidence_id=99,
                       ab_status='diff', ab_diff='7',
                       control_status='diff', control_diff='8'
                       WHERE period_id=? AND item_key=?""",
                    (period_id, agg.item_key),
                )

            aggregate.persist_period_totals(conn, info.project_id, [agg])
            row = conn.execute(
                """SELECT cross_check_status, cross_check_diff, evidence_id,
                          ab_status, ab_diff, control_status, control_diff
                   FROM period_totals WHERE period_id=? AND item_key=?""",
                (period_id, agg.item_key),
            ).fetchone()

            assert dict(row) == {
                "cross_check_status": "pending",
                "cross_check_diff": None,
                "evidence_id": None,
                "ab_status": "pending",
                "ab_diff": None,
                "control_status": "not_available",
                "control_diff": None,
            }
        finally:
            conn.close()

    def test_missing_amount_is_calculated_without_overwriting_source(self, tmp_path):
        """原始合价缺失时只保存计算候选，不把计算值伪装回原始 amount。"""
        info, conn, period_id = _make_amount_case(tmp_path)
        try:
            aggs = aggregate.aggregate_project(conn, info.project_id)
            assert len(aggs) == 1
            agg = aggs[0]
            assert agg.cum_qty == D("2")
            assert agg.cum_amount == D("200")
            assert agg.raw_cum_amount is None
            assert agg.calculated_cum_amount == D("200")
            assert agg.calculated_cum_amount_used == D("200")
            assert agg.amount_source == "calculated"
            assert agg.amount_status == "calculated"
            assert agg.status == "incomplete"

            line = conn.execute(
                "SELECT amount, flags_json FROM line_items WHERE period_id=?",
                (period_id,),
            ).fetchone()
            assert line["amount"] is None
            flags = json.loads(line["flags_json"])
            assert flags["calculated_amount"] == "200"
            assert flags["amount_source"] == "calculated"
            assert flags["amount_status"] == "calculated"

            assert aggregate.persist_period_totals(conn, info.project_id, aggs) == 1
            persisted = conn.execute(
                """SELECT qty_sum, amount_sum, cross_check_status,
                          raw_amount_sum, calculated_amount_sum,
                          calculated_amount_used_sum, effective_amount_sum,
                          amount_source, amount_status
                   FROM period_totals """
                "WHERE period_id=? AND item_key=?",
                (period_id, agg.item_key),
            ).fetchone()
            assert persisted["qty_sum"] == "2"
            assert persisted["amount_sum"] == "200"
            assert persisted["cross_check_status"] == "pending"
            assert persisted["raw_amount_sum"] is None
            assert persisted["calculated_amount_sum"] == "200"
            assert persisted["calculated_amount_used_sum"] == "200"
            assert persisted["effective_amount_sum"] == "200"
            assert persisted["amount_source"] == "calculated"
            assert persisted["amount_status"] == "calculated"

            result = crosscheck.run_crosscheck(
                conn, info.project_id, [1], direction="downward"
            )[0]
            assert result.path_a_total == D("200")
            assert result.path_b_total == D("200")
            assert result.diff_ab == D("0")
            assert result.status == "match"
            assert result.amount_source == "calculated"
            assert result.amount_status == "calculated"
            assert result.derived_rows == 1
            assert result.formula_mismatch_rows == 0

            checked = conn.execute(
                """SELECT cross_check_status, cross_check_diff, evidence_id,
                          ab_status, ab_diff, control_status, control_diff """
                "FROM period_totals WHERE period_id=? AND item_key=?",
                (period_id, agg.item_key),
            ).fetchone()
            assert checked["cross_check_status"] == "ab_match_control_not_available"
            assert checked["cross_check_diff"] == "0"
            assert checked["evidence_id"] is not None
            assert checked["ab_status"] == "match"
            assert checked["ab_diff"] == "0"
            assert checked["control_status"] == "not_available"
            assert checked["control_diff"] is None
        finally:
            conn.close()

    def test_present_amount_keeps_raw_and_reports_formula_difference(self, tmp_path):
        """三者齐全时原始合价仍走原始路径，同时报告数量×单价差异。"""
        info, conn, period_id = _make_amount_case(tmp_path, raw_amount="250")
        try:
            agg = aggregate.aggregate_project(conn, info.project_id)[0]
            assert agg.cum_amount == D("250")
            assert agg.raw_cum_amount == D("250")
            assert agg.calculated_cum_amount == D("200")
            assert agg.calculated_cum_amount_used is None
            assert agg.amount_source == "raw"
            assert agg.amount_status == "raw"
            assert agg.amount_check_status == "diff"
            assert agg.status == "incomplete"

            line = conn.execute(
                "SELECT amount, flags_json FROM line_items WHERE period_id=?",
                (period_id,),
            ).fetchone()
            assert line["amount"] == "250"
            flags = json.loads(line["flags_json"])
            assert flags["calculated_amount"] == "200"
            assert flags["amount_source"] == "raw"
            assert flags["amount_status"] == "raw"
            assert flags["amount_check_status"] == "diff"
            assert flags["amount_check"]["difference"] == "50"

            aggregate.persist_period_totals(conn, info.project_id, [agg])
            persisted = conn.execute(
                """SELECT raw_amount_sum, calculated_amount_sum,
                          calculated_amount_used_sum, effective_amount_sum,
                          amount_source, amount_status
                   FROM period_totals WHERE period_id=?""",
                (period_id,),
            ).fetchone()
            assert persisted["raw_amount_sum"] == "250"
            assert persisted["calculated_amount_sum"] == "200"
            assert persisted["calculated_amount_used_sum"] is None
            assert persisted["effective_amount_sum"] == "250"
            assert persisted["amount_source"] == "raw"
            assert persisted["amount_status"] == "raw"
            result = crosscheck.run_crosscheck(
                conn, info.project_id, [1], direction="downward"
            )[0]
            assert result.path_a_total == D("250")
            assert result.path_b_total == D("250")
            assert result.diff_ab == D("0")
            assert result.formula_mismatch_rows > 0
            assert result.amount_source == "raw"
            assert result.amount_status == "raw_checked_diff"
            assert result.status == "diff"
        finally:
            conn.close()

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

    def test_confirmed_amount_only_item_is_complete_without_quantity(self, tmp_path):
        """名称+金额型计价不虚构数量，金额累计仍应是完整状态。"""
        from costguard.core.models import project as pm

        info = pm.create_project("金额型计价", tmp_path / "ws")
        info, conn = pm.open_project(Path(info.workspace_path))
        try:
            with conn:
                period_id = conn.execute(
                    """INSERT INTO settlement_periods(project_id, period_no, title, direction)
                       VALUES (?,?,?,?)""",
                    (info.project_id, 2, "第2期", "downward"),
                ).lastrowid
                file_id = conn.execute(
                    """INSERT INTO source_files(project_id, original_path, stored_path,
                       original_name, sha256, size_bytes, file_type, imported_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (info.project_id, "/amount.xlsx", "/amount.xlsx", "amount.xlsx",
                     "amount-only", 1, "xlsx", "2026"),
                ).lastrowid
                batch_id = conn.execute(
                    """INSERT INTO parse_batches(file_id, parser, parsed_at, status)
                       VALUES (?,?,?,?)""",
                    (file_id, "test", "2026", "ok"),
                ).lastrowid
                sheet_id = conn.execute(
                    """INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name, n_rows,
                       n_cols, period_id) VALUES (?,?,?,?,?,?)""",
                    (batch_id, 0, "计算明细表", 7, 7, period_id),
                ).lastrowid
                conn.execute(
                    """INSERT INTO table_headers(sheet_id, header_row_lo, header_row_hi,
                       col_map_json, confidence, needs_review, data_row_start, data_row_end)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (sheet_id, 4, 6, json.dumps({"code": 1, "name": 2, "amount": 7}),
                     1.0, 0, 7, 7),
                )
                conn.execute(
                    """INSERT INTO line_items(period_id, sheet_id, code, name, amount,
                       flags_json) VALUES (?,?,?,?,?,?)""",
                    (period_id, sheet_id, "1", "暂估价设备结算", "3370591.52",
                     json.dumps({"row": 7})),
                )
            result = aggregate.aggregate_project(conn, info.project_id)
            assert len(result) == 1
            assert result[0].status == "ok"
            assert result[0].cum_qty is None
            assert result[0].cum_amount == D("3370591.52")
            assert result[0].wavg_price is None
            assert result[0].warnings == []
        finally:
            conn.close()


class TestCrossCheck:
    def test_multi_period_match(self, project_multi):
        """A/B 两条独立路径结果一致（同一文件同网格）。"""
        info, conn, report = project_multi
        aggregate.persist_period_totals(
            conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
        )
        results = crosscheck.run_crosscheck(conn, info.project_id, [report.period_no])
        res = results[0]
        assert res.status == "match", f"A={res.path_a_total} B={res.path_b_total}"
        assert res.path_a_total == res.path_b_total
        # C 校验：原表小计行存在
        assert res.raw_subtotal is not None
        assert res.control_status == "match"
        assert abs(res.control_diff) <= crosscheck.TOL
        persisted = conn.execute(
            """SELECT DISTINCT cross_check_status, ab_status, control_status,
                      ab_diff, control_diff
               FROM period_totals WHERE period_id=?""",
            (res.period_id,),
        ).fetchall()
        assert persisted
        assert {r["cross_check_status"] for r in persisted} == {"match"}
        assert {r["ab_status"] for r in persisted} == {"match"}
        assert {r["control_status"] for r in persisted} == {"match"}
        assert all(D(r["ab_diff"]) == 0 for r in persisted)
        assert all(abs(D(r["control_diff"])) <= crosscheck.TOL for r in persisted)

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
        assert results[0].control_status == "diff"
        # evidence 已生成
        ev = conn.execute(
            "SELECT COUNT(*) AS c FROM evidence WHERE project_id=? AND kind='cross_check'",
            (info.project_id,),
        ).fetchone()
        assert ev["c"] >= 1
