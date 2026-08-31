"""独立复核后补充的 P0 闸门回归测试。

这些测试专门覆盖“数值碰巧一致但证据条件不充分”的反例，避免只用全量
pytest 通过来替代业务验收。
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest


def _make_book(
    path: Path,
    *,
    two_grand_totals: bool = False,
    single_detail: bool = False,
    missing_detail_amount: bool = False,
) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "第1期明细"
    for col, value in enumerate(("项目编码", "项目名称", "工程量", "综合单价", "合价"), 1):
        ws.cell(1, col, value)
    detail_rows = [("A1", "项目A", 2, 100, 200)]
    if not single_detail:
        detail_rows.append(("A2", "项目B", 1, 300, 300))
    if missing_detail_amount:
        code, name, quantity, unit_price, _amount = detail_rows[0]
        detail_rows[0] = (code, name, quantity, unit_price, None)
    for row in detail_rows:
        ws.append(row)
    control_total = 200 if single_detail else 500
    ws.append((None, "本页小计", None, None, control_total))
    ws.append((None, "合计", None, None, control_total))
    if two_grand_totals:
        ws.append((None, "总计", None, None, control_total))
    wb.save(path)


def _import_project(
    tmp_path: Path,
    *,
    two_grand_totals: bool = False,
    single_detail: bool = False,
    missing_detail_amount: bool = False,
):
    from jiadun.core.engine import settlement_io
    from jiadun.core.models import project as pm

    info = pm.create_project("复核闸门", tmp_path / "ws")
    info, conn = pm.open_project(Path(info.workspace_path))
    src = tmp_path / "第1期结算.xlsx"
    _make_book(
        src,
        two_grand_totals=two_grand_totals,
        single_detail=single_detail,
        missing_detail_amount=missing_detail_amount,
    )
    report = settlement_io.import_settlement_file(
        conn, info.project_id, Path(info.workspace_path), src, direction="upward"
    )
    period_id = conn.execute(
        "SELECT id FROM settlement_periods WHERE project_id=?", (info.project_id,)
    ).fetchone()["id"]
    return info, conn, report, period_id


def test_automatic_import_persists_range_evidence(tmp_path):
    _info, conn, _report, period_id = _import_project(tmp_path)
    try:
        row = conn.execute(
            """SELECT data_row_start, data_row_end, data_range_status,
                      data_range_method, data_range_evidence_json
               FROM table_headers WHERE sheet_id=(
                   SELECT sheet_id FROM line_items WHERE period_id=? LIMIT 1
               )""",
            (period_id,),
        ).fetchone()
        assert row is not None
        assert (row["data_row_start"], row["data_row_end"]) == (2, 5)
        assert row["data_range_status"] == "inferred"
        assert row["data_range_method"] == "last_non_empty_row"
        evidence = json.loads(row["data_range_evidence_json"])
        assert evidence["data_range"] == [2, 5]
        assert evidence["method"] == "last_non_empty_row"
    finally:
        conn.close()


def test_unproven_range_blocks_green_even_when_a_b_c_match(tmp_path):
    from jiadun.core.engine import crosscheck

    _info, conn, _report, period_id = _import_project(tmp_path)
    try:
        conn.execute(
            """UPDATE table_headers SET data_row_start=NULL, data_row_end=NULL,
                      data_range_status='unproven', data_range_method='unknown'"""
        )
        result = crosscheck.check_period(conn, period_id)
        assert result.status == "match"
        assert result.control_status == "match"
        assert result.range_unproven_sheets == 1
        assert result.verification_level == "insufficient"
        assert any("取数范围完整性无法证明" in note for note in result.notes)
    finally:
        conn.close()


def test_missing_raw_amount_cannot_be_sufficient_when_control_matches(tmp_path):
    from jiadun.core.engine import crosscheck

    _info, conn, _report, period_id = _import_project(
        tmp_path, single_detail=True, missing_detail_amount=True
    )
    try:
        conn.execute(
            "UPDATE line_items SET amount=NULL, quantity='2', unit_price='100' "
            "WHERE period_id=? AND json_extract(flags_json, '$.subtotal') IS NULL",
            (period_id,),
        )
        result = crosscheck.check_period(conn, period_id)
        assert result.path_a_total == Decimal("200")
        assert result.path_b_total == Decimal("200")
        assert result.raw_subtotal == Decimal("200")
        assert result.diff_ab == Decimal("0")
        assert result.control_status == "match"
        assert result.verification_level == "insufficient"
        assert result.amount_source == "calculated"
        assert any("原始金额缺失" in note for note in result.notes)
    finally:
        conn.close()


@pytest.mark.parametrize(("column", "value"), [
    ("hidden_rows_json", "[3]"),
    ("hidden_cols_json", "[5]"),
])
def test_hidden_visibility_blocks_inferred_range(tmp_path, column, value):
    """自动推断不能把隐藏行/列当作已证明完整；必须人工确认范围。"""
    from jiadun.core.engine import crosscheck

    _info, conn, _report, period_id = _import_project(tmp_path)
    try:
        conn.execute(f"UPDATE raw_sheets SET {column}=?", (value,))
        result = crosscheck.check_period(conn, period_id)
        assert result.range_unproven_sheets == 1
        assert result.verification_level == "insufficient"
        assert any("取数范围完整性无法证明" in note for note in result.notes)
    finally:
        conn.close()


def test_persisted_overall_status_is_not_match_when_range_unproven(tmp_path):
    """旧 period_totals 读取面也不能把证据不足写成整体 match。"""
    from jiadun.core.engine import aggregate, crosscheck

    info, conn, _report, period_id = _import_project(tmp_path)
    try:
        conn.execute("UPDATE raw_sheets SET hidden_rows_json='[3]'")
        aggs = aggregate.aggregate_project(conn, info.project_id, direction="upward")
        aggregate.persist_period_totals(conn, info.project_id, aggs)
        result = crosscheck.run_crosscheck(
            conn, info.project_id, [1], direction="upward"
        )[0]
        row = conn.execute(
            """SELECT cross_check_status, verification_level
               FROM period_totals WHERE period_id=? LIMIT 1""",
            (period_id,),
        ).fetchone()
        assert result.verification_level == "insufficient"
        assert row["verification_level"] == "insufficient"
        assert row["cross_check_status"] != "match"
    finally:
        conn.close()


def test_confirmed_non_settlement_sheet_is_not_pending(tmp_path):
    from jiadun.core.engine import crosscheck, settlement_io

    info, conn, _report, period_id = _import_project(tmp_path)
    try:
        batch_id = conn.execute(
            "SELECT id FROM parse_batches WHERE file_id=(SELECT source_file_id FROM settlement_periods WHERE id=?)",
            (period_id,),
        ).fetchone()["id"]
        sheet_id = conn.execute(
            """INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name, n_rows, n_cols)
               VALUES (?,?,?,?,?)""",
            (batch_id, 99, "汇总控制表", 3, 3),
        ).lastrowid
        assert settlement_io.pending_sheet_count(conn, info.project_id) == 1
        before = crosscheck.check_period(conn, period_id)
        assert before.pending_sheets == 1

        settlement_io.confirm_sheet_non_settlement_role(
            conn, info.project_id, int(sheet_id), "复核人", "settlement_summary",
            "人工核对：该表为控制汇总，仅存证，不进入明细结算模型",
        )
        assert settlement_io.pending_sheet_count(conn, info.project_id) == 0
        after = crosscheck.check_period(conn, period_id)
        assert after.pending_sheets == 0
        assert after.verification_level == "sufficient"
        assert conn.execute(
            "SELECT COUNT(*) c FROM evidence WHERE project_id=? AND kind='sheet_role_confirmed'",
            (info.project_id,),
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_crosscheck_result_survives_reopen(tmp_path):
    from jiadun.core.engine import aggregate, crosscheck
    from jiadun.core.models import project as pm

    info, conn, _report, period_id = _import_project(tmp_path)
    workspace = Path(info.workspace_path)
    try:
        aggregate.persist_period_totals(
            conn,
            info.project_id,
            aggregate.aggregate_project(conn, info.project_id, direction="upward"),
        )
        result = crosscheck.run_crosscheck(
            conn, info.project_id, [1], direction="upward"
        )[0]
        stored = conn.execute(
            """SELECT verification_level, status, detail_rows,
                      excluded_subtotal_rows, pending_sheets, range_unproven_sheets,
                      evidence_id FROM crosscheck_results WHERE period_id=?""",
            (period_id,),
        ).fetchone()
        assert stored is not None
        assert stored["verification_level"] == result.verification_level
        assert stored["status"] == "match"
        assert stored["detail_rows"] == result.detail_rows
        assert stored["excluded_subtotal_rows"] == result.excluded_subtotal_rows
        assert stored["pending_sheets"] == result.pending_sheets == 0
        assert stored["range_unproven_sheets"] == result.range_unproven_sheets == 0
        assert stored["evidence_id"] is not None
    finally:
        conn.close()

    _reopened, reopened = pm.open_project(workspace)
    try:
        row = reopened.execute(
            "SELECT verification_level, evidence_id FROM crosscheck_results WHERE period_id=?",
            (period_id,),
        ).fetchone()
        assert row is not None
        assert row["verification_level"] == result.verification_level
        assert row["evidence_id"] is not None
    finally:
        reopened.close()


def test_period_totals_carries_run_verification_level(tmp_path):
    from jiadun.core.engine import aggregate, crosscheck

    info, conn, _report, period_id = _import_project(tmp_path)
    try:
        aggs = aggregate.aggregate_project(conn, info.project_id, direction="upward")
        aggregate.persist_period_totals(conn, info.project_id, aggs)
        result = crosscheck.run_crosscheck(
            conn, info.project_id, [1], direction="upward"
        )[0]
        row = conn.execute(
            "SELECT cross_check_status, verification_level FROM period_totals WHERE period_id=? LIMIT 1",
            (period_id,),
        ).fetchone()
        assert row is not None
        assert row["verification_level"] == result.verification_level
        if result.verification_level == "sufficient":
            assert row["cross_check_status"] == "match"
        else:
            assert row["cross_check_status"] != "match"
    finally:
        conn.close()


def test_multiple_grand_totals_make_control_unavailable(tmp_path):
    from jiadun.core.engine import crosscheck

    _info, conn, _report, period_id = _import_project(tmp_path, two_grand_totals=True)
    try:
        result = crosscheck.check_period(conn, period_id)
        assert result.control_status == "not_available"
        assert result.verification_level == "insufficient"
        assert any("控制值不唯一" in note for note in result.notes)
    finally:
        conn.close()


def test_reaggregate_invalidates_persisted_crosscheck_result(tmp_path):
    from jiadun.core.engine import aggregate, crosscheck

    info, conn, _report, period_id = _import_project(tmp_path)
    try:
        aggregate.persist_period_totals(
            conn,
            info.project_id,
            aggregate.aggregate_project(conn, info.project_id, direction="upward"),
        )
        crosscheck.run_crosscheck(conn, info.project_id, [1], direction="upward")
        assert conn.execute(
            "SELECT COUNT(*) c FROM crosscheck_results WHERE period_id=?", (period_id,)
        ).fetchone()["c"] == 1
        aggs = aggregate.aggregate_project(conn, info.project_id, direction="upward")
        aggregate.persist_period_totals(conn, info.project_id, aggs)
        assert conn.execute(
            "SELECT COUNT(*) c FROM crosscheck_results WHERE period_id=?", (period_id,)
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_empty_match_reason_does_not_partially_write(tmp_path):
    from jiadun.core.evidence.audit import AuditReasonRequiredError
    from jiadun.core.matching import matching

    info, conn, _report, _period_id = _import_project(tmp_path)
    try:
        groups = matching.match_items(conn, info.project_id, direction="upward")
        matching.save_matches(conn, info.project_id, groups)
        match_id = conn.execute(
            "SELECT id FROM matches WHERE project_id=? LIMIT 1", (info.project_id,)
        ).fetchone()["id"]
        with pytest.raises(AuditReasonRequiredError):
            matching.confirm_match(conn, info.project_id, match_id, "user", " ")
        row = conn.execute("SELECT status FROM matches WHERE id=?", (match_id,)).fetchone()
        assert row["status"] == "pending"
        assert conn.execute(
            "SELECT COUNT(*) c FROM evidence WHERE project_id=? AND kind='match_confirmation'",
            (info.project_id,),
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE project_id=? AND action='confirm_match'",
            (info.project_id,),
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_business_fallbacks_hide_unknown_codes():
    from jiadun.core.anomalies.rules import _dir_label
    from jiadun.ui.labels import rule_zh

    assert _dir_label("upward") == "[对上结算] "
    assert _dir_label("downward") == "[对下结算] "
    assert rule_zh("new_rule_v99") == "其他审核问题"
