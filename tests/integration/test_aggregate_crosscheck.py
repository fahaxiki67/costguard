"""Phase 3 结算累计与双向校核测试。"""
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "synthetic_test_data"))

from generator import make_multi_period  # noqa: E402

from costguard.core.anomalies import coverage  # noqa: E402
from costguard.core.contracts import run_contract  # noqa: E402
from costguard.core.engine import aggregate, crosscheck, settlement_io  # noqa: E402
from costguard.core.reporting import build_project_summary  # noqa: E402

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
    def test_aggregate_validation_coverage_records_control_skip_and_run_kind(self, tmp_path):
        """A/B 已执行、C 无控制值时必须显式跳过，不能伪装成未覆盖或全量通过。"""
        info, conn, _period_id = _make_amount_case(tmp_path)
        try:
            aggregate.persist_period_totals(
                conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
            )
            crosscheck.run_crosscheck(conn, info.project_id, [1], direction="downward")
            summary = coverage.coverage_summary(
                conn,
                info.project_id,
                run_kind=coverage.AGGREGATE_VALIDATION,
            )
            assert summary["run_kind"] == coverage.AGGREGATE_VALIDATION
            assert summary["status"] == coverage.PARTIAL
            assert summary["expected_count"] == 3
            assert summary["executed_count"] == 2
            assert summary["skipped_count"] == 1
            assert summary["failed_count"] == 0
            assert any(key.endswith(":path_c") for key in summary["skipped"])
        finally:
            conn.close()

    def test_missing_raw_amount_persists_insufficient_state_across_storage_layers(
        self, tmp_path
    ):
        """缺失原始合价时各持久化层均不得落成 match/sufficient。"""
        info, conn, period_id = _make_amount_case(tmp_path)
        try:
            aggs = aggregate.aggregate_project(conn, info.project_id)
            aggregate.persist_period_totals(conn, info.project_id, aggs)
            result = crosscheck.run_crosscheck(
                conn, info.project_id, [1], direction="downward"
            )[0]
            assert result.raw_subtotal is None
            assert result.control_status == "not_available"
            assert result.verification_level == "insufficient"

            persisted = conn.execute(
                """SELECT cross_check_status, verification_level, control_status,
                          evidence_id
                   FROM period_totals WHERE period_id=? LIMIT 1""",
                (period_id,),
            ).fetchone()
            assert persisted["cross_check_status"] != "match"
            assert persisted["cross_check_status"] == "ab_match_control_not_available"
            assert persisted["verification_level"] == "insufficient"
            assert persisted["control_status"] == "not_available"
            assert persisted["evidence_id"] is not None

            stored_result = conn.execute(
                """SELECT status, verification_level, raw_subtotal, control_status,
                          evidence_id
                   FROM crosscheck_results WHERE project_id=? AND period_id=?""",
                (info.project_id, period_id),
            ).fetchone()
            assert stored_result["status"] != "match"
            assert stored_result["status"] == "ab_match_control_not_available"
            assert stored_result["verification_level"] == "insufficient"
            assert stored_result["raw_subtotal"] is None
            assert stored_result["control_status"] == "not_available"
            assert stored_result["evidence_id"] == persisted["evidence_id"]

            evidence = conn.execute(
                """SELECT summary, steps_json FROM evidence
                   WHERE project_id=? AND kind='cross_check' ORDER BY id DESC LIMIT 1""",
                (info.project_id,),
            ).fetchone()
            assert evidence is not None
            assert "校核不充分" in evidence["summary"]
            steps = json.loads(evidence["steps_json"])
            assert steps["verification_level"] == "insufficient"
            assert steps["control_status"] == "not_available"

            summary = coverage.coverage_summary(
                conn,
                info.project_id,
                run_kind=coverage.AGGREGATE_VALIDATION,
            )
            assert summary["status"] == coverage.PARTIAL
            assert summary["executed_count"] == 2
            assert summary["skipped_count"] == 1
            assert summary["failed_count"] == 0
            assert any(key.endswith(":path_c") for key in summary["skipped"])
        finally:
            conn.close()

    def test_detection_coverage_write_failure_rolls_back_crosscheck_business_results(
        self, tmp_path
    ):
        """detection_runs 自身写入失败时不得留下可识别成功的业务结果。"""
        info, conn, period_id = _make_amount_case(tmp_path, raw_amount="200")
        try:
            aggs = aggregate.aggregate_project(conn, info.project_id)
            aggregate.persist_period_totals(conn, info.project_id, aggs)
            before = conn.execute(
                """SELECT cross_check_status, cross_check_diff, evidence_id,
                          ab_status, ab_diff, control_status, control_diff,
                          verification_level
                   FROM period_totals WHERE period_id=? LIMIT 1""",
                (period_id,),
            ).fetchone()
            assert before["cross_check_status"] == "pending"
            assert before["evidence_id"] is None

            conn.execute(
                """CREATE TRIGGER block_aggregate_detection_runs
                   BEFORE INSERT ON detection_runs
                   WHEN NEW.run_kind='aggregate_validation'
                   BEGIN
                       SELECT RAISE(ABORT, 'synthetic detection coverage failure');
                   END"""
            )
            with pytest.raises(
                sqlite3.IntegrityError, match="synthetic detection coverage failure"
            ):
                crosscheck.run_crosscheck(
                    conn, info.project_id, [1], direction="downward"
                )

            after = conn.execute(
                """SELECT cross_check_status, cross_check_diff, evidence_id,
                          ab_status, ab_diff, control_status, control_diff,
                          verification_level
                   FROM period_totals WHERE period_id=? LIMIT 1""",
                (period_id,),
            ).fetchone()
            assert dict(after) == dict(before)
            assert conn.execute(
                """SELECT COUNT(*) AS c FROM crosscheck_results
                   WHERE project_id=? AND period_id=?""",
                (info.project_id, period_id),
            ).fetchone()["c"] == 0
            assert conn.execute(
                """SELECT COUNT(*) AS c FROM evidence
                   WHERE project_id=? AND kind='cross_check'""",
                (info.project_id,),
            ).fetchone()["c"] == 0
            assert conn.execute(
                """SELECT COUNT(*) AS c FROM detection_runs
                   WHERE project_id=? AND run_kind=?""",
                (info.project_id, coverage.AGGREGATE_VALIDATION),
            ).fetchone()["c"] == 0
        finally:
            conn.execute("DROP TRIGGER IF EXISTS block_aggregate_detection_runs")
            conn.close()

    def test_failed_rerun_invalidates_previous_success_for_current_and_reopened_connections(
        self, project_multi
    ):
        """已有成功重跑失败时，旧结果只能保留为历史，不能继续作为当前成功。"""
        from costguard.core.models import project as pm

        info, conn, report = project_multi
        workspace = Path(info.workspace_path)
        period_id = conn.execute(
            "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?",
            (info.project_id, report.period_no),
        ).fetchone()["id"]
        direction = conn.execute(
            "SELECT direction FROM settlement_periods WHERE id=?", (period_id,)
        ).fetchone()["direction"]
        aggregate.persist_period_totals(
            conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
        )
        period_nos = [row["period_no"] for row in conn.execute(
            "SELECT period_no FROM settlement_periods WHERE project_id=? ORDER BY period_no",
            (info.project_id,),
        ).fetchall()]
        first = crosscheck.run_crosscheck(
            conn, info.project_id, period_nos, direction=direction
        )[0]
        signature = run_contract.current_run_signature(conn, info.project_id)
        assert first.status == "match"
        assert first.verification_level == "sufficient"
        assert coverage.coverage_summary(
            conn, info.project_id, run_kind=coverage.AGGREGATE_VALIDATION
        )["status"] == coverage.COMPLETE
        assert conn.execute(
            "SELECT cross_check_status, verification_level, evidence_id "
            "FROM period_totals WHERE period_id=? LIMIT 1",
            (period_id,),
        ).fetchone()["cross_check_status"] == "match"

        conn.execute(
            """CREATE TRIGGER block_aggregate_detection_runs_r2
               BEFORE INSERT ON detection_runs
               WHEN NEW.run_kind='aggregate_validation'
               BEGIN
                   SELECT RAISE(ABORT, 'synthetic R2 detection coverage failure');
               END"""
        )
        try:
            with pytest.raises(
                sqlite3.IntegrityError, match="synthetic R2 detection coverage failure"
            ):
                crosscheck.run_crosscheck(
                    conn, info.project_id, [report.period_no], direction=direction
                )

            assert conn.in_transaction is False
            current_scope, scope_params = run_contract.current_scope(
                conn, info.project_id, "cr"
            )
            current_result = conn.execute(
                f"SELECT COUNT(*) AS c FROM crosscheck_results cr "
                f"WHERE cr.project_id=? AND {current_scope}",
                (info.project_id, *scope_params),
            ).fetchone()["c"]
            assert current_result == 0
            stale_result = conn.execute(
                "SELECT status, verification_level, run_signature, evidence_id "
                "FROM crosscheck_results WHERE project_id=? AND period_id=?",
                (info.project_id, period_id),
            ).fetchone()
            assert stale_result is not None
            assert stale_result["status"] == "invalidated"
            assert stale_result["verification_level"] == "insufficient"
            assert stale_result["run_signature"] != signature
            assert stale_result["evidence_id"] is None

            stale_total = conn.execute(
                "SELECT cross_check_status, verification_level, run_signature, evidence_id "
                "FROM period_totals WHERE period_id=? LIMIT 1",
                (period_id,),
            ).fetchone()
            assert stale_total["cross_check_status"] == "invalidated"
            assert stale_total["verification_level"] == "insufficient"
            assert stale_total["run_signature"] != signature
            assert stale_total["evidence_id"] is None

            history = conn.execute(
                "SELECT COUNT(*) AS c FROM evidence "
                "WHERE project_id=? AND kind='cross_check' AND run_signature<>?",
                (info.project_id, signature),
            ).fetchone()["c"]
            assert history >= 1
            assert coverage.coverage_summary(
                conn, info.project_id, run_kind=coverage.AGGREGATE_VALIDATION
            )["status"] != coverage.COMPLETE
        finally:
            conn.execute("DROP TRIGGER IF EXISTS block_aggregate_detection_runs_r2")

        _reopened, reopened = pm.open_project(workspace)
        try:
            reopened_scope, reopened_params = run_contract.current_scope(
                reopened, info.project_id, "cr"
            )
            assert reopened.execute(
                f"SELECT COUNT(*) AS c FROM crosscheck_results cr "
                f"WHERE cr.project_id=? AND {reopened_scope}",
                (info.project_id, *reopened_params),
            ).fetchone()["c"] == 0
            assert coverage.coverage_summary(
                reopened, info.project_id, run_kind=coverage.AGGREGATE_VALIDATION
            )["status"] != coverage.COMPLETE
        finally:
            reopened.close()

    def test_final_commit_failure_rolls_back_and_records_failed_coverage(
        self, project_multi
    ):
        """最终 COMMIT 被拒绝时，连接必须清理并记录失败覆盖。"""
        info, conn, report = project_multi
        aggregate.persist_period_totals(
            conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
        )
        direction = conn.execute(
            "SELECT direction FROM settlement_periods WHERE project_id=? AND period_no=?",
            (info.project_id, report.period_no),
        ).fetchone()["direction"]
        first = crosscheck.run_crosscheck(
            conn, info.project_id, [report.period_no], direction=direction
        )[0]
        assert first.verification_level == "sufficient"

        commit_count = 0

        def deny_batch_commit(action, arg1, _arg2, _db_name, _trigger_name):
            nonlocal commit_count
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
                commit_count += 1
                if commit_count == 2:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_batch_commit)
        try:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                crosscheck.run_crosscheck(
                    conn, info.project_id, [report.period_no], direction=direction
                )
            assert commit_count >= 2
            assert conn.in_transaction is False
            summary = coverage.coverage_summary(
                conn, info.project_id, run_kind=coverage.AGGREGATE_VALIDATION
            )
            assert summary["status"] == coverage.FAILED
            assert summary["failed_count"] == summary["expected_count"]
            assert summary["error_summary"]

            current_scope, scope_params = run_contract.current_scope(
                conn, info.project_id, "cr"
            )
            assert conn.execute(
                f"SELECT COUNT(*) AS c FROM crosscheck_results cr "
                f"WHERE cr.project_id=? AND {current_scope} AND cr.status='match' "
                f"AND cr.verification_level='sufficient'",
                (info.project_id, *scope_params),
            ).fetchone()["c"] == 0
        finally:
            conn.set_authorizer(None)
            conn.close()

    @pytest.mark.parametrize(
        "failure_target", ["evidence", "period_totals", "crosscheck_results"]
    )
    def test_failed_rerun_persistence_failure_does_not_expose_old_success(
        self, project_multi, failure_target
    ):
        """已有成功后业务持久化失败，旧结果不得继续作为当前成功。"""
        info, conn, report = project_multi
        period_id = conn.execute(
            "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?",
            (info.project_id, report.period_no),
        ).fetchone()["id"]
        direction = conn.execute(
            "SELECT direction FROM settlement_periods WHERE id=?", (period_id,)
        ).fetchone()["direction"]
        aggregate.persist_period_totals(
            conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
        )
        first = crosscheck.run_crosscheck(
            conn, info.project_id, [report.period_no], direction=direction
        )[0]
        assert first.verification_level == "sufficient"
        signature = run_contract.current_run_signature(conn, info.project_id)
        trigger_name = f"block_rerun_{failure_target}"
        if failure_target == "evidence":
            trigger_sql = f"""CREATE TRIGGER {trigger_name}
                AFTER INSERT ON evidence
                WHEN NEW.kind='cross_check'
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic rerun evidence failure');
                END"""
            error_message = "synthetic rerun evidence failure"
        elif failure_target == "period_totals":
            trigger_sql = f"""CREATE TRIGGER {trigger_name}
                AFTER UPDATE OF cross_check_status ON period_totals
                WHEN NEW.project_id={info.project_id} AND NEW.period_id={period_id}
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic rerun period_totals failure');
                END"""
            error_message = "synthetic rerun period_totals failure"
        else:
            trigger_sql = f"""CREATE TRIGGER {trigger_name}
                AFTER UPDATE ON crosscheck_results
                WHEN NEW.project_id={info.project_id} AND NEW.period_id={period_id}
                  AND NEW.run_signature='{signature}'
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic rerun crosscheck_results failure');
                END"""
            error_message = "synthetic rerun crosscheck_results failure"
        conn.execute(trigger_sql)
        try:
            with pytest.raises(sqlite3.IntegrityError, match=error_message):
                crosscheck.run_crosscheck(
                    conn, info.project_id, [report.period_no], direction=direction
                )

            assert conn.in_transaction is False
            current_result_scope, current_result_params = run_contract.current_scope(
                conn, info.project_id, "cr"
            )
            assert conn.execute(
                f"SELECT COUNT(*) AS c FROM crosscheck_results cr "
                f"WHERE cr.project_id=? AND cr.period_id=? AND {current_result_scope}",
                (info.project_id, period_id, *current_result_params),
            ).fetchone()["c"] == 0
            current_total_scope, current_total_params = run_contract.current_scope(
                conn, info.project_id, "pt"
            )
            assert conn.execute(
                f"SELECT COUNT(*) AS c FROM period_totals pt "
                f"WHERE pt.project_id=? AND pt.period_id=? AND {current_total_scope}",
                (info.project_id, period_id, *current_total_params),
            ).fetchone()["c"] == 0

            stale_result = conn.execute(
                "SELECT status, verification_level, run_signature, evidence_id "
                "FROM crosscheck_results WHERE project_id=? AND period_id=?",
                (info.project_id, period_id),
            ).fetchone()
            assert stale_result["status"] == "invalidated"
            assert stale_result["verification_level"] == "insufficient"
            assert stale_result["run_signature"] != signature
            assert stale_result["evidence_id"] is None
            stale_evidence = conn.execute(
                "SELECT summary, steps_json, run_signature FROM evidence "
                "WHERE project_id=? AND kind='cross_check' "
                "ORDER BY id DESC LIMIT 1",
                (info.project_id,),
            ).fetchone()
            assert stale_evidence["summary"].startswith("【已失效】")
            assert json.loads(stale_evidence["steps_json"])["invalidation"]["status"] == (
                "invalidated"
            )
            assert stale_evidence["run_signature"] != signature
            assert coverage.coverage_summary(
                conn, info.project_id, run_kind=coverage.AGGREGATE_VALIDATION
            )["status"] == coverage.FAILED
        finally:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")

    def test_invalidation_commit_retries_then_failed_coverage_hides_old_success(
        self, project_multi
    ):
        """失效化 COMMIT 一次性拒绝后重试，后续失败 coverage 不能复活旧成功。"""
        info, conn, report = project_multi
        period_id = conn.execute(
            "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?",
            (info.project_id, report.period_no),
        ).fetchone()["id"]
        direction = conn.execute(
            "SELECT direction FROM settlement_periods WHERE id=?", (period_id,)
        ).fetchone()["direction"]
        aggregate.persist_period_totals(
            conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
        )
        assert crosscheck.run_crosscheck(
            conn, info.project_id, [report.period_no], direction=direction
        )[0].verification_level == "sufficient"
        signature = run_contract.current_run_signature(conn, info.project_id)
        conn.execute(
            """CREATE TRIGGER block_r3_success_coverage
               BEFORE INSERT ON detection_runs
               WHEN NEW.run_kind='aggregate_validation' AND NEW.error_summary IS NULL
               BEGIN
                   SELECT RAISE(ABORT, 'synthetic R3 success coverage failure');
               END"""
        )
        commit_count = 0

        def reject_first_commit(action, arg1, _arg2, _db_name, _trigger_name):
            nonlocal commit_count
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
                commit_count += 1
                if commit_count == 1:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(reject_first_commit)
        try:
            with pytest.raises(
                sqlite3.IntegrityError, match="synthetic R3 success coverage failure"
            ):
                crosscheck.run_crosscheck(
                    conn, info.project_id, [report.period_no], direction=direction
                )

            # 失效化第一次提交被拒、第二次提交成功；失败 coverage 随后在
            # 独立事务中成功落库，因此会再产生一次 COMMIT。
            assert commit_count == 3
            assert conn.in_transaction is False
            current_result_scope, current_result_params = run_contract.current_scope(
                conn, info.project_id, "cr"
            )
            assert conn.execute(
                f"SELECT COUNT(*) AS c FROM crosscheck_results cr "
                f"WHERE cr.project_id=? AND {current_result_scope}",
                (info.project_id, *current_result_params),
            ).fetchone()["c"] == 0
            current_total_scope, current_total_params = run_contract.current_scope(
                conn, info.project_id, "pt"
            )
            assert conn.execute(
                f"SELECT COUNT(*) AS c FROM period_totals pt "
                f"WHERE pt.project_id=? AND pt.period_id=? AND {current_total_scope}",
                (info.project_id, period_id, *current_total_params),
            ).fetchone()["c"] == 0
            stale_result = conn.execute(
                "SELECT status, verification_level, run_signature, evidence_id "
                "FROM crosscheck_results WHERE project_id=? AND period_id=?",
                (info.project_id, period_id),
            ).fetchone()
            assert stale_result["status"] == "invalidated"
            assert stale_result["verification_level"] == "insufficient"
            assert stale_result["run_signature"] != signature
            assert stale_result["evidence_id"] is None
            stale_evidence = conn.execute(
                "SELECT summary, steps_json, run_signature FROM evidence "
                "WHERE project_id=? AND kind='cross_check' ORDER BY id DESC LIMIT 1",
                (info.project_id,),
            ).fetchone()
            assert stale_evidence["summary"].startswith("【已失效】")
            assert json.loads(stale_evidence["steps_json"])["invalidation"]["status"] == (
                "invalidated"
            )
            assert stale_evidence["run_signature"] != signature

            project_summary = build_project_summary(conn, info.project_id)
            assert project_summary.verification["periods_checked"] == 0
            assert project_summary.verification["status"] == run_contract.FAIL_CLOSED_STATUS
            assert project_summary.aggregate_coverage["status"] == coverage.FAILED
            assert project_summary.statuses["automatic_analysis"] == "failed"
        finally:
            conn.set_authorizer(None)
            conn.execute("DROP TRIGGER IF EXISTS block_r3_success_coverage")

        from costguard.core.models import project as pm

        _reopened, reopened = pm.open_project(Path(info.workspace_path))
        try:
            reopened_result_scope, reopened_result_params = run_contract.current_scope(
                reopened, info.project_id, "cr"
            )
            assert reopened.execute(
                f"SELECT COUNT(*) AS c FROM crosscheck_results cr "
                f"WHERE cr.project_id=? AND cr.period_id=? AND {reopened_result_scope}",
                (info.project_id, period_id, *reopened_result_params),
            ).fetchone()["c"] == 0
            reopened_total_scope, reopened_total_params = run_contract.current_scope(
                reopened, info.project_id, "pt"
            )
            assert reopened.execute(
                f"SELECT COUNT(*) AS c FROM period_totals pt "
                f"WHERE pt.project_id=? AND pt.period_id=? AND {reopened_total_scope}",
                (info.project_id, period_id, *reopened_total_params),
            ).fetchone()["c"] == 0
            reopened_summary = build_project_summary(reopened, info.project_id)
            assert reopened_summary.verification["periods_checked"] == 0
            assert reopened_summary.aggregate_coverage["status"] == coverage.FAILED
        finally:
            reopened.close()

    def test_invalidation_recovery_keeps_fail_closed_when_failure_coverage_unwritable(
        self, project_multi
    ):
        """失效化已重试成功但 FAILED coverage 不可写时，旧成功仍不能复活。"""
        info, conn, report = project_multi
        period_id = conn.execute(
            "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?",
            (info.project_id, report.period_no),
        ).fetchone()["id"]
        direction = conn.execute(
            "SELECT direction FROM settlement_periods WHERE id=?", (period_id,)
        ).fetchone()["direction"]
        aggregate.persist_period_totals(
            conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
        )
        assert crosscheck.run_crosscheck(
            conn, info.project_id, [report.period_no], direction=direction
        )[0].verification_level == "sufficient"
        signature = run_contract.current_run_signature(conn, info.project_id)
        conn.execute(
            """CREATE TRIGGER block_r3_all_coverage
               BEFORE INSERT ON detection_runs
               WHEN NEW.run_kind='aggregate_validation'
               BEGIN
                   SELECT RAISE(ABORT, 'synthetic R3 all coverage failure');
               END"""
        )
        commit_count = 0

        def reject_first_commit(action, arg1, _arg2, _db_name, _trigger_name):
            nonlocal commit_count
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
                commit_count += 1
                if commit_count == 1:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(reject_first_commit)
        try:
            with pytest.raises(
                sqlite3.IntegrityError, match="synthetic R3 all coverage failure"
            ):
                crosscheck.run_crosscheck(
                    conn, info.project_id, [report.period_no], direction=direction
                )
            # 失效化第一次提交被拒、第二次提交成功；coverage 的两次 INSERT
            # 都在写入阶段被 trigger 拒绝，因此不会再产生 COMMIT。
            assert commit_count == 2
            assert conn.in_transaction is False
            current_scope, scope_params = run_contract.current_scope(
                conn, info.project_id, "cr"
            )
            assert conn.execute(
                f"SELECT COUNT(*) AS c FROM crosscheck_results cr "
                f"WHERE cr.project_id=? AND {current_scope}",
                (info.project_id, *scope_params),
            ).fetchone()["c"] == 0
            stale_result = conn.execute(
                "SELECT status, verification_level, run_signature FROM crosscheck_results "
                "WHERE project_id=? AND period_id=?",
                (info.project_id, period_id),
            ).fetchone()
            assert stale_result["status"] == "invalidated"
            assert stale_result["verification_level"] == "insufficient"
            assert stale_result["run_signature"] != signature
            summary = coverage.coverage_summary(
                conn, info.project_id, run_kind=coverage.AGGREGATE_VALIDATION
            )
            assert summary["status"] == coverage.UNAVAILABLE
            assert summary["run_id"] is None
        finally:
            conn.set_authorizer(None)
            conn.execute("DROP TRIGGER IF EXISTS block_r3_all_coverage")

    def test_persistent_invalidation_commit_rejection_is_explicit_physical_boundary(
        self, project_multi
    ):
        """持续拒绝所有提交时必须进入可读取的运行级不可用边界。"""
        from costguard.core.models import project as pm

        info, conn, report = project_multi
        workspace = Path(info.workspace_path)
        period_id = conn.execute(
            "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?",
            (info.project_id, report.period_no),
        ).fetchone()["id"]
        direction = conn.execute(
            "SELECT direction FROM settlement_periods WHERE id=?", (period_id,)
        ).fetchone()["direction"]
        aggregate.persist_period_totals(
            conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
        )
        first = crosscheck.run_crosscheck(
            conn, info.project_id, [report.period_no], direction=direction
        )[0]
        old_evidence = conn.execute(
            "SELECT id FROM evidence WHERE project_id=? AND kind='cross_check' "
            "ORDER BY id DESC LIMIT 1",
            (info.project_id,),
        ).fetchone()["id"]
        commit_count = 0

        def reject_every_commit(action, arg1, _arg2, _db_name, _trigger_name):
            nonlocal commit_count
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
                commit_count += 1
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(reject_every_commit)
        try:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                crosscheck.run_crosscheck(
                    conn, info.project_id, [report.period_no], direction=direction
                )
            # 持续拒绝时失效化事务均回滚；实现不把一次失败调用伪造成新的结果。
            assert commit_count >= 2
            assert conn.in_transaction is False
            state = run_contract.get_fail_closed_state(conn, info.project_id)
            assert state is not None
            assert state["status"] == run_contract.FAIL_CLOSED_STATUS
            assert state["persisted"] is True
            result = conn.execute(
                "SELECT status, verification_level, evidence_id FROM crosscheck_results "
                "WHERE project_id=? AND period_id=?",
                (info.project_id, period_id),
            ).fetchone()
            # 原始历史行可保留，但不再进入任何当前读取面。
            assert result["status"] == first.status
            assert result["verification_level"] == first.verification_level
            assert result["evidence_id"] == old_evidence

            current_scope, scope_params = run_contract.current_scope(
                conn, info.project_id, "cr"
            )
            assert conn.execute(
                f"SELECT COUNT(*) AS c FROM crosscheck_results cr "
                f"WHERE cr.project_id=? AND cr.period_id=? AND {current_scope}",
                (info.project_id, period_id, *scope_params),
            ).fetchone()["c"] == 0
            coverage_summary = coverage.coverage_summary(
                conn, info.project_id, run_kind=coverage.AGGREGATE_VALIDATION
            )
            assert coverage_summary["fail_closed"] is True
            assert coverage_summary["availability_status"] == run_contract.FAIL_CLOSED_STATUS
            assert coverage_summary["status"] != coverage.COMPLETE

            project_summary = build_project_summary(conn, info.project_id)
            assert project_summary.run_availability["available"] is False
            assert project_summary.run_availability["status"] == run_contract.FAIL_CLOSED_STATUS
            assert project_summary.verification["status"] == run_contract.FAIL_CLOSED_STATUS
            assert project_summary.statuses["automatic_analysis"] == "failed"
        finally:
            conn.set_authorizer(None)

        # 侧车是项目级持久边界；重开连接仍必须拒绝旧成功。
        _reopened, reopened = pm.open_project(workspace)
        try:
            reopened_state = run_contract.get_fail_closed_state(
                reopened, info.project_id
            )
            assert reopened_state is not None
            assert reopened_state["persisted"] is True
            reopened_scope, reopened_params = run_contract.current_scope(
                reopened, info.project_id, "cr"
            )
            assert reopened.execute(
                f"SELECT COUNT(*) AS c FROM crosscheck_results cr "
                f"WHERE cr.project_id=? AND cr.period_id=? AND {reopened_scope}",
                (info.project_id, period_id, *reopened_params),
            ).fetchone()["c"] == 0
            reopened_coverage = coverage.coverage_summary(
                reopened, info.project_id, run_kind=coverage.AGGREGATE_VALIDATION
            )
            assert reopened_coverage["fail_closed"] is True
            assert reopened_coverage["status"] != coverage.COMPLETE
            reopened_summary = build_project_summary(reopened, info.project_id)
            assert reopened_summary.run_availability["available"] is False
            assert reopened_summary.statuses["automatic_analysis"] == "failed"
        finally:
            reopened.close()

    @pytest.mark.parametrize(
        "failure_target", ["crosscheck_results", "period_totals", "evidence", "detection_coverage"]
    )
    def test_invalidation_dml_failure_enters_fail_closed_boundary(
        self, project_multi, failure_target
    ):
        """失效化四类 DML 失败时，原始异常保留且旧成功不进入当前读取面。"""
        from costguard.core.models import project as pm

        info, conn, report = project_multi
        workspace = Path(info.workspace_path)
        period_id = conn.execute(
            "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?",
            (info.project_id, report.period_no),
        ).fetchone()["id"]
        direction = conn.execute(
            "SELECT direction FROM settlement_periods WHERE id=?", (period_id,)
        ).fetchone()["direction"]
        aggregate.persist_period_totals(
            conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
        )
        first = crosscheck.run_crosscheck(
            conn, info.project_id, [report.period_no], direction=direction
        )[0]
        assert first.verification_level == "sufficient"
        signature = run_contract.current_run_signature(conn, info.project_id)
        trigger_name = f"block_invalidation_{failure_target}"
        if failure_target == "crosscheck_results":
            trigger_sql = f"""CREATE TRIGGER {trigger_name}
                AFTER UPDATE OF run_signature ON crosscheck_results
                WHEN NEW.project_id={info.project_id} AND NEW.period_id={period_id}
                  AND NEW.run_signature='{run_contract.INVALIDATED_RUN_SIGNATURE}'
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic invalidation crosscheck_results failure');
                END"""
            error_message = "synthetic invalidation crosscheck_results failure"
        elif failure_target == "period_totals":
            trigger_sql = f"""CREATE TRIGGER {trigger_name}
                AFTER INSERT ON period_totals
                WHEN NEW.project_id={info.project_id} AND NEW.period_id={period_id}
                  AND NEW.run_signature='{run_contract.INVALIDATED_RUN_SIGNATURE}'
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic invalidation period_totals failure');
                END"""
            error_message = "synthetic invalidation period_totals failure"
        elif failure_target == "evidence":
            trigger_sql = f"""CREATE TRIGGER {trigger_name}
                AFTER UPDATE OF run_signature ON evidence
                WHEN NEW.project_id={info.project_id}
                  AND NEW.run_signature='{run_contract.INVALIDATED_RUN_SIGNATURE}'
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic invalidation evidence failure');
                END"""
            error_message = "synthetic invalidation evidence failure"
        else:
            trigger_sql = f"""CREATE TRIGGER {trigger_name}
                AFTER UPDATE OF run_signature ON detection_runs
                WHEN NEW.project_id={info.project_id}
                  AND NEW.run_kind='{coverage.AGGREGATE_VALIDATION}'
                  AND NEW.run_signature='{run_contract.INVALIDATED_RUN_SIGNATURE}'
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic invalidation detection coverage failure');
                END"""
            error_message = "synthetic invalidation detection coverage failure"
        conn.execute(trigger_sql)
        try:
            with pytest.raises(sqlite3.IntegrityError, match=error_message):
                crosscheck.run_crosscheck(
                    conn, info.project_id, [report.period_no], direction=direction
                )
            assert conn.in_transaction is False
            state = run_contract.get_fail_closed_state(conn, info.project_id)
            assert state is not None
            assert state["persisted"] is True

            current_scope, scope_params = run_contract.current_scope(
                conn, info.project_id, "cr"
            )
            assert conn.execute(
                f"SELECT COUNT(*) AS c FROM crosscheck_results cr "
                f"WHERE cr.project_id=? AND cr.period_id=? AND {current_scope}",
                (info.project_id, period_id, *scope_params),
            ).fetchone()["c"] == 0
            old_row = conn.execute(
                "SELECT run_signature, status, verification_level FROM crosscheck_results "
                "WHERE project_id=? AND period_id=?",
                (info.project_id, period_id),
            ).fetchone()
            # 失效化事务回滚时，旧行仍是原始成功；运行级边界负责阻断它。
            assert old_row["run_signature"] == signature
            assert old_row["status"] == "match"
            assert old_row["verification_level"] == "sufficient"

            project_summary = build_project_summary(conn, info.project_id)
            assert project_summary.run_availability["available"] is False
            assert project_summary.verification["status"] == run_contract.FAIL_CLOSED_STATUS
            assert project_summary.statuses["automatic_analysis"] == "failed"
            coverage_summary = coverage.coverage_summary(
                conn, info.project_id, run_kind=coverage.AGGREGATE_VALIDATION
            )
            assert coverage_summary["fail_closed"] is True
            assert coverage_summary["status"] != coverage.COMPLETE
        finally:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")

        _reopened, reopened = pm.open_project(workspace)
        try:
            reopened_scope, reopened_params = run_contract.current_scope(
                reopened, info.project_id, "cr"
            )
            assert reopened.execute(
                f"SELECT COUNT(*) AS c FROM crosscheck_results cr "
                f"WHERE cr.project_id=? AND cr.period_id=? AND {reopened_scope}",
                (info.project_id, period_id, *reopened_params),
            ).fetchone()["c"] == 0
            assert not run_contract.current_results_available(
                reopened, info.project_id
            )["available"]
        finally:
            reopened.close()

    def test_contract_switch_commit_failure_hides_old_success(self, project_multi):
        """输入签名变化但契约切换提交失败时，旧成功不得继续可见。"""
        from costguard.core.models import project as pm

        info, conn, report = project_multi
        workspace = Path(info.workspace_path)
        period_id = conn.execute(
            "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?",
            (info.project_id, report.period_no),
        ).fetchone()["id"]
        direction = conn.execute(
            "SELECT direction FROM settlement_periods WHERE id=?", (period_id,)
        ).fetchone()["direction"]
        aggregate.persist_period_totals(
            conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
        )
        assert crosscheck.run_crosscheck(
            conn, info.project_id, [report.period_no], direction=direction
        )[0].verification_level == "sufficient"
        old_signature = run_contract.current_run_signature(conn, info.project_id)
        with conn:
            conn.execute(
                "UPDATE source_files SET original_name=? WHERE project_id=?",
                ("changed-input.xlsx", info.project_id),
            )

        def reject_commit(action, arg1, _arg2, _db_name, _trigger_name):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(reject_commit)
        try:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                crosscheck.run_crosscheck(
                    conn, info.project_id, [report.period_no], direction=direction
                )
        finally:
            conn.set_authorizer(None)

        assert conn.in_transaction is False
        state = run_contract.get_fail_closed_state(conn, info.project_id)
        assert state is not None
        assert state["status"] == run_contract.FAIL_CLOSED_STATUS
        assert run_contract.current_run_signature(conn, info.project_id) == old_signature
        scope, params = run_contract.current_scope(conn, info.project_id, "cr")
        assert conn.execute(
            f"SELECT COUNT(*) FROM crosscheck_results cr WHERE cr.project_id=? AND {scope}",
            (info.project_id, *params),
        ).fetchone()[0] == 0
        assert coverage.coverage_summary(
            conn, info.project_id, run_kind=coverage.AGGREGATE_VALIDATION
        )["fail_closed"] is True
        summary = build_project_summary(conn, info.project_id)
        assert summary.run_availability["available"] is False
        assert summary.statuses["automatic_analysis"] == "failed"

        reopened_info, reopened = pm.open_project(workspace)
        try:
            assert not run_contract.current_results_available(
                reopened, reopened_info.project_id
            )["available"]
            reopened_scope, reopened_params = run_contract.current_scope(
                reopened, reopened_info.project_id, "cr"
            )
            assert reopened.execute(
                f"SELECT COUNT(*) FROM crosscheck_results cr WHERE cr.project_id=? AND {reopened_scope}",
                (reopened_info.project_id, *reopened_params),
            ).fetchone()[0] == 0
        finally:
            reopened.close()

    def test_successful_rerun_clears_fail_closed_state_and_is_current(self, project_multi):
        """完整成功的新运行提交后才清除边界，并恢复当前读取。"""
        info, conn, report = project_multi
        period_id = conn.execute(
            "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?",
            (info.project_id, report.period_no),
        ).fetchone()["id"]
        direction = conn.execute(
            "SELECT direction FROM settlement_periods WHERE id=?", (period_id,)
        ).fetchone()["direction"]
        aggregate.persist_period_totals(
            conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
        )
        period_nos = [row["period_no"] for row in conn.execute(
            "SELECT period_no FROM settlement_periods WHERE project_id=? ORDER BY period_no",
            (info.project_id,),
        ).fetchall()]
        first = crosscheck.run_crosscheck(
            conn, info.project_id, [report.period_no], direction=direction
        )[0]
        assert first.verification_level == "sufficient"
        run_contract.set_fail_closed_state(
            conn, info.project_id, reason="synthetic prior failed run"
        )
        assert not run_contract.current_results_available(conn, info.project_id)["available"]

        result = crosscheck.run_crosscheck(
            conn, info.project_id, period_nos, direction=direction
        )[0]
        assert result.verification_level == "sufficient"
        assert run_contract.get_fail_closed_state(conn, info.project_id) is None
        assert run_contract.current_results_available(conn, info.project_id)["available"]
        assert coverage.coverage_summary(
            conn, info.project_id, run_kind=coverage.AGGREGATE_VALIDATION
        )["status"] == coverage.COMPLETE

    def test_partial_rerun_invalidates_unrequested_current_periods(self, project_multi):
        """只重跑一期不得继续把其他期次的旧结果显示为当前结果。"""
        info, conn, report = project_multi
        period_nos = [row["period_no"] for row in conn.execute(
            "SELECT period_no FROM settlement_periods WHERE project_id=? ORDER BY period_no",
            (info.project_id,),
        ).fetchall()]
        aggregate.persist_period_totals(
            conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
        )
        crosscheck.run_crosscheck(conn, info.project_id, period_nos)
        crosscheck.run_crosscheck(conn, info.project_id, [report.period_no])
        summary = build_project_summary(conn, info.project_id)
        assert summary.verification["periods_unchecked"] == len(period_nos) - 1
        assert summary.aggregate_coverage["status"] == coverage.FAILED
        scope, params = run_contract.current_scope(conn, info.project_id, "cr")
        assert conn.execute(
            f"SELECT COUNT(*) FROM crosscheck_results cr WHERE cr.project_id=? AND {scope} "
            "AND cr.period_id != (SELECT id FROM settlement_periods "
            "WHERE project_id=? AND period_no=?)",
            (info.project_id, *params, info.project_id, report.period_no),
        ).fetchone()[0] == 0

    def test_missing_period_total_fails_closed_instead_of_succeeding(self, tmp_path):
        """关键 period_totals 回写缺行时不能产生完整校核结果。"""
        info, conn, period_id = _make_amount_case(tmp_path, raw_amount="200")
        try:
            aggregate.persist_period_totals(
                conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
            )
            with conn:
                conn.execute("DELETE FROM period_totals WHERE period_id=?", (period_id,))
            with pytest.raises(RuntimeError, match="period_totals"):
                crosscheck.run_crosscheck(conn, info.project_id, [1], direction="downward")
            assert conn.execute(
                "SELECT COUNT(*) FROM crosscheck_results WHERE project_id=?", (info.project_id,)
            ).fetchone()[0] == 0
            assert coverage.coverage_summary(
                conn, info.project_id, run_kind=coverage.AGGREGATE_VALIDATION
            )["status"] == coverage.FAILED
        finally:
            conn.close()

    def test_fail_closed_state_remains_when_success_cleanup_fails(
        self, project_multi, monkeypatch
    ):
        """清除运行级边界失败时不得返回成功或暴露新结果。"""
        info, conn, report = project_multi
        period_id = conn.execute(
            "SELECT id FROM settlement_periods WHERE project_id=? AND period_no=?",
            (info.project_id, report.period_no),
        ).fetchone()["id"]
        direction = conn.execute(
            "SELECT direction FROM settlement_periods WHERE id=?", (period_id,)
        ).fetchone()["direction"]
        aggregate.persist_period_totals(
            conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
        )
        assert crosscheck.run_crosscheck(
            conn, info.project_id, [report.period_no], direction=direction
        )[0].verification_level == "sufficient"

        real_clear = run_contract.clear_fail_closed_state

        def refuse_clear(*_args, **_kwargs):
            raise OSError("synthetic fail-closed cleanup failure")

        monkeypatch.setattr(run_contract, "clear_fail_closed_state", refuse_clear)
        with pytest.raises(OSError, match="synthetic fail-closed cleanup failure"):
            crosscheck.run_crosscheck(
                conn, info.project_id, [report.period_no], direction=direction
            )
        assert not run_contract.current_results_available(conn, info.project_id)["available"]
        scope, params = run_contract.current_scope(conn, info.project_id, "cr")
        assert conn.execute(
            f"SELECT COUNT(*) AS c FROM crosscheck_results cr "
            f"WHERE cr.project_id=? AND cr.period_id=? AND {scope}",
            (info.project_id, period_id, *params),
        ).fetchone()["c"] == 0
        monkeypatch.setattr(run_contract, "clear_fail_closed_state", real_clear)

    def _assert_crosscheck_persistence_failure(
        self, tmp_path, *, trigger_name, trigger_sql, error_message
    ):
        info, conn, period_id = _make_amount_case(tmp_path, raw_amount="200")
        try:
            aggs = aggregate.aggregate_project(conn, info.project_id)
            aggregate.persist_period_totals(conn, info.project_id, aggs)
            before = conn.execute(
                """SELECT cross_check_status, cross_check_diff, evidence_id,
                          ab_status, ab_diff, control_status, control_diff,
                          verification_level
                   FROM period_totals WHERE period_id=? LIMIT 1""",
                (period_id,),
            ).fetchone()
            assert before["cross_check_status"] == "pending"
            assert before["evidence_id"] is None
            conn.execute(trigger_sql)

            with pytest.raises(sqlite3.IntegrityError, match=error_message):
                crosscheck.run_crosscheck(
                    conn, info.project_id, [1], direction="downward"
                )

            after = conn.execute(
                """SELECT cross_check_status, cross_check_diff, evidence_id,
                          ab_status, ab_diff, control_status, control_diff,
                          verification_level
                   FROM period_totals WHERE period_id=? LIMIT 1""",
                (period_id,),
            ).fetchone()
            assert dict(after) == dict(before)
            assert conn.execute(
                """SELECT COUNT(*) AS c FROM crosscheck_results
                   WHERE project_id=? AND period_id=?""",
                (info.project_id, period_id),
            ).fetchone()["c"] == 0
            assert conn.execute(
                """SELECT COUNT(*) AS c FROM evidence
                   WHERE project_id=? AND kind='cross_check'""",
                (info.project_id,),
            ).fetchone()["c"] == 0

            run = conn.execute(
                """SELECT status, expected_json, failed_json, error_summary
                   FROM detection_runs
                   WHERE project_id=? AND run_kind=?
                   ORDER BY id DESC LIMIT 1""",
                (info.project_id, coverage.AGGREGATE_VALIDATION),
            ).fetchone()
            assert run is not None
            assert run["status"] == coverage.FAILED
            expected_paths = {
                f"period:{period_id}:path_a",
                f"period:{period_id}:path_b",
                f"period:{period_id}:path_c",
            }
            assert set(json.loads(run["expected_json"])) == expected_paths
            assert set(json.loads(run["failed_json"])) == expected_paths
            assert error_message in run["error_summary"]
        finally:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
            conn.close()

    def test_crosscheck_evidence_failure_after_partial_write_rolls_back_and_records_coverage(
        self, tmp_path
    ):
        """Evidence 已插入后失败时，整期业务写入和覆盖结果均回滚/失败。"""
        self._assert_crosscheck_persistence_failure(
            tmp_path,
            trigger_name="fail_crosscheck_evidence_after_insert",
            trigger_sql="""CREATE TRIGGER fail_crosscheck_evidence_after_insert
                AFTER INSERT ON evidence
                WHEN NEW.kind='cross_check'
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic crosscheck evidence failure');
                END""",
            error_message="synthetic crosscheck evidence failure",
        )

    def test_crosscheck_period_totals_failure_after_partial_write_rolls_back_and_records_coverage(
        self, tmp_path
    ):
        """period_totals 已被触发更新后失败时，部分结果不得提交。"""
        info, conn, period_id = _make_amount_case(tmp_path, raw_amount="200")
        try:
            aggs = aggregate.aggregate_project(conn, info.project_id)
            aggregate.persist_period_totals(conn, info.project_id, aggs)
            conn.execute(
                f"""CREATE TRIGGER fail_crosscheck_period_totals_after_update
                    AFTER UPDATE OF cross_check_status ON period_totals
                    WHEN NEW.period_id={period_id}
                    BEGIN
                        SELECT RAISE(ABORT, 'synthetic period_totals failure');
                    END"""
            )
            self._assert_crosscheck_persistence_failure_from_existing_project(
                conn, info, period_id, "fail_crosscheck_period_totals_after_update",
                "synthetic period_totals failure",
            )
        finally:
            conn.execute("DROP TRIGGER IF EXISTS fail_crosscheck_period_totals_after_update")
            conn.close()

    def test_crosscheck_results_failure_after_partial_write_rolls_back_and_records_coverage(
        self, tmp_path
    ):
        """crosscheck_results 已写入前序表后失败时，整期结果不得半成功。"""
        info, conn, period_id = _make_amount_case(tmp_path, raw_amount="200")
        try:
            aggs = aggregate.aggregate_project(conn, info.project_id)
            aggregate.persist_period_totals(conn, info.project_id, aggs)
            conn.execute(
                f"""CREATE TRIGGER fail_crosscheck_results_after_insert
                    AFTER INSERT ON crosscheck_results
                    WHEN NEW.project_id={info.project_id} AND NEW.period_id={period_id}
                    BEGIN
                        SELECT RAISE(ABORT, 'synthetic crosscheck_results failure');
                    END"""
            )
            self._assert_crosscheck_persistence_failure_from_existing_project(
                conn, info, period_id, "fail_crosscheck_results_after_insert",
                "synthetic crosscheck_results failure",
            )
        finally:
            conn.execute("DROP TRIGGER IF EXISTS fail_crosscheck_results_after_insert")
            conn.close()

    def _assert_crosscheck_persistence_failure_from_existing_project(
        self, conn, info, period_id, trigger_name, error_message
    ):
        before = conn.execute(
            """SELECT cross_check_status, cross_check_diff, evidence_id,
                      ab_status, ab_diff, control_status, control_diff,
                      verification_level
               FROM period_totals WHERE period_id=? LIMIT 1""",
            (period_id,),
        ).fetchone()
        assert before["cross_check_status"] == "pending"
        assert before["evidence_id"] is None
        with pytest.raises(sqlite3.IntegrityError, match=error_message):
            crosscheck.run_crosscheck(conn, info.project_id, [1], direction="downward")
        after = conn.execute(
            """SELECT cross_check_status, cross_check_diff, evidence_id,
                      ab_status, ab_diff, control_status, control_diff,
                      verification_level
               FROM period_totals WHERE period_id=? LIMIT 1""",
            (period_id,),
        ).fetchone()
        assert dict(after) == dict(before)
        assert conn.execute(
            """SELECT COUNT(*) AS c FROM crosscheck_results
               WHERE project_id=? AND period_id=?""",
            (info.project_id, period_id),
        ).fetchone()["c"] == 0
        assert conn.execute(
            """SELECT COUNT(*) AS c FROM evidence
               WHERE project_id=? AND kind='cross_check'""",
            (info.project_id,),
        ).fetchone()["c"] == 0
        run = conn.execute(
            """SELECT status, expected_json, failed_json, error_summary
               FROM detection_runs
               WHERE project_id=? AND run_kind=? ORDER BY id DESC LIMIT 1""",
            (info.project_id, coverage.AGGREGATE_VALIDATION),
        ).fetchone()
        assert run is not None
        assert run["status"] == coverage.FAILED
        expected_paths = {
            f"period:{period_id}:path_a",
            f"period:{period_id}:path_b",
            f"period:{period_id}:path_c",
        }
        assert set(json.loads(run["expected_json"])) == expected_paths
        assert set(json.loads(run["failed_json"])) == expected_paths
        assert error_message in run["error_summary"]

    def test_aggregate_validation_failure_is_recorded(self, tmp_path):
        """不存在或歧义期次不得没有覆盖率记录。"""
        info, conn, _period_id = _make_amount_case(tmp_path)
        try:
            with pytest.raises(ValueError):
                crosscheck.run_crosscheck(conn, info.project_id, [999], direction="downward")
            summary = coverage.coverage_summary(
                conn,
                info.project_id,
                run_kind=coverage.AGGREGATE_VALIDATION,
            )
            assert summary["status"] == coverage.FAILED
            assert summary["expected_count"] == 3
            assert summary["failed_count"] == 3
        finally:
            conn.close()

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
        # 校核只回写已形成的聚合结果；先保存本次修改后的聚合底稿。
        aggregate.persist_period_totals(
            conn, info.project_id, aggregate.aggregate_project(conn, info.project_id)
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
