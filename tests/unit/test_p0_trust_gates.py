"""P0/P1 可信度闸门回归。

这些测试刻意构造数值可以彼此对上的输入，验证系统不会把同源复算、重复
明细或负数调整误报为项目级绿色通过；同时检查每条校核路径都留下可回读
的来源范围。
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import openpyxl
from tests.unit.test_review_gates import _import_project, _make_book


def _duplicate_book(path: Path) -> None:
    _make_book(path)
    wb = openpyxl.load_workbook(path)
    ws = wb[wb.sheetnames[0]]
    # 在两个控制行之前复制一条明细；同步控制值使 A/B/C 数值仍能碰巧相等。
    ws.insert_rows(4, 1)
    for col, value in enumerate(("A1", "项目A（重复）", 2, 100, 200), 1):
        ws.cell(4, col, value=value)
    ws.cell(5, 5, value=700)
    ws.cell(6, 5, value=700)
    wb.save(path)


def _negative_adjustment_book(path: Path) -> None:
    _make_book(path)
    wb = openpyxl.load_workbook(path)
    ws = wb[wb.sheetnames[0]]
    # 合法与否不能由程序臆断；即便控制值同步为 -100，也必须进入人工复核。
    ws.cell(3, 3, value=-1)
    ws.cell(3, 5, value=-300)
    ws.cell(4, 5, value=-100)
    ws.cell(5, 5, value=-100)
    wb.save(path)


def _all_missing_amount_book(path: Path) -> None:
    _make_book(path)
    wb = openpyxl.load_workbook(path)
    ws = wb[wb.sheetnames[0]]
    ws.cell(2, 5).value = None
    ws.cell(3, 5).value = None
    # 控制行也不提供可冒充的“0”金额。
    ws.cell(4, 5).value = None
    ws.cell(5, 5).value = None
    wb.save(path)


def _internal_blank_title_book(path: Path) -> None:
    """数据区内部的空行/分部标题不能被默认当作已证明的连续明细。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "第1期明细"
    for col, value in enumerate(("项目编码", "项目名称", "工程量", "综合单价", "合价"), 1):
        ws.cell(1, col, value)
    ws.append(("A1", "项目A", 1, 100, 100))
    ws.append((None, None, None, None, None))
    ws.append((None, "分部二", None, None, None))
    ws.append(("A2", "项目B", 2, 200, 400))
    ws.append((None, "合计", None, None, 500))
    wb.save(path)


def test_clean_paths_record_independent_scope_and_evidence(tmp_path: Path):
    from jiadun.core.engine import crosscheck

    _info, conn, _report, period_id = _import_project(tmp_path)
    try:
        result = crosscheck.check_period(conn, period_id)
        assert result.verification_level == "sufficient", result.notes
        assert result.path_scopes["path_a"]["source_kind"] == "line_items"
        assert result.path_scopes["path_b"]["source_kind"] == "raw_cells"
        assert result.path_scopes["path_a"]["sheets"]
        assert result.path_scopes["path_b"]["sheets"]
        payload = json.loads(crosscheck.make_evidence(1, result))
        scope_step = next(step for step in payload["steps"] if step["step"] == "路径来源范围")
        assert scope_step["result"]["path_a"]["filters"]
        assert scope_step["result"]["path_b"]["filters"]
    finally:
        conn.close()


def test_c_control_wraps_source_evidence_for_current_run_without_mutating_source(
    tmp_path: Path,
):
    """C 控制值保留原始来源，同时用当前运行包装后再进入摘要。"""
    from jiadun.core.contracts import run_contract
    from jiadun.core.engine import aggregate, crosscheck
    from jiadun.core.reporting import build_project_summary

    info, conn, _report, period_id = _import_project(tmp_path)
    try:
        aggregate.persist_period_totals(
            conn,
            info.project_id,
            aggregate.aggregate_project(conn, info.project_id, direction="upward"),
        )
        grand = conn.execute(
            """SELECT id, flags_json FROM line_items
               WHERE period_id=? AND json_extract(flags_json, '$.grand_total')=1""",
            (period_id,),
        ).fetchone()
        assert grand is not None
        source_id = int(json.loads(grand["flags_json"])["source_evidence_id"])
        source_before = dict(
            conn.execute(
                """SELECT scope, run_id, run_signature, sources_json, steps_json
                   FROM evidence WHERE id=?""",
                (source_id,),
            ).fetchone()
        )
        assert source_before["scope"] == "source"
        assert source_before["run_id"] is None
        assert source_before["run_signature"] is None

        result = crosscheck.run_crosscheck(
            conn, info.project_id, [1], direction="upward"
        )[0]
        current = run_contract.get_current_contract(conn, info.project_id)
        assert current is not None
        assert result.c_control_evidence_id is not None
        assert result.c_control_evidence_id != source_id

        wrapper = conn.execute(
            """SELECT kind, scope, run_id, run_signature, sources_json, steps_json
               FROM evidence WHERE id=?""",
            (result.c_control_evidence_id,),
        ).fetchone()
        assert wrapper is not None
        assert (wrapper["kind"], wrapper["scope"]) == ("line_item_source", "current")
        assert (wrapper["run_id"], wrapper["run_signature"]) == (
            current.run_id,
            current.signature,
        )
        wrapper_sources = json.loads(wrapper["sources_json"])
        control_source = next(
            item for item in wrapper_sources if item.get("line_item_id") == grand["id"]
        )
        assert control_source["source_evidence_id"] == source_id
        assert control_source["file_id"] is not None
        assert control_source["sheet_id"] is not None
        assert control_source["row"] == 5
        assert control_source["col"] == 5
        assert control_source["raw_value"] == "500"
        assert control_source["raw_cell_value"] == "500"
        assert control_source["raw_cell_is_formula"] is False
        wrapper_steps = json.loads(wrapper["steps_json"])
        assert wrapper_steps[0]["step"] == "C 控制来源包装"
        assert source_id in wrapper_steps[0]["source_evidence_ids"]

        source_after = dict(
            conn.execute(
                """SELECT scope, run_id, run_signature, sources_json, steps_json
                   FROM evidence WHERE id=?""",
                (source_id,),
            ).fetchone()
        )
        assert source_after == source_before

        summary = build_project_summary(conn, info.project_id)
        assert summary.verification["evidence_complete"] is True
        assert summary.verification["status"] == "sufficient"
    finally:
        conn.close()


def test_c_control_blocks_when_line_item_locator_cannot_reach_raw_cells(
    tmp_path: Path,
):
    """C 不能用清洗金额替代缺失的原始控制单元格。"""
    from jiadun.core.engine import crosscheck

    _info, conn, _report, period_id = _import_project(tmp_path)
    try:
        grand = conn.execute(
            """SELECT id FROM line_items
               WHERE period_id=? AND json_extract(flags_json, '$.grand_total')=1""",
            (period_id,),
        ).fetchone()
        assert grand is not None
        conn.execute(
            "UPDATE line_items SET amount_evid=? WHERE id=?",
            (json.dumps({"raw": "500", "value": "500", "row": 5, "col": 99}), grand["id"]),
        )

        result = crosscheck.check_period(conn, period_id)

        assert result.raw_subtotal is None
        assert result.control_status == "not_available"
        assert result.c_independence_level == "not_proven"
        assert result.verification_level == "insufficient"
        assert any("无法独立定位" in note for note in result.notes)
    finally:
        conn.close()


def test_c_control_marks_multiple_grand_candidates_not_proven(tmp_path: Path):
    """多个合计候选不能通过清洗投影或求和强行形成 C 控制值。"""
    from jiadun.core.engine import crosscheck

    _info, conn, _report, period_id = _import_project(
        tmp_path, two_grand_totals=True
    )
    try:
        result = crosscheck.check_period(conn, period_id)

        assert result.control_status == "not_available"
        assert result.c_independence_level == "not_proven"
        assert result.verification_level == "insufficient"
        assert any("控制值不唯一" in note for note in result.notes)
    finally:
        conn.close()


def test_duplicate_detail_blocks_green_even_when_totals_are_balanced(tmp_path: Path):
    from jiadun.core.engine import crosscheck, settlement_io
    from jiadun.core.models import project as pm

    src = tmp_path / "duplicate.xlsx"
    _duplicate_book(src)
    info = pm.create_project("重复明细闸门", tmp_path / "ws")
    info, conn = pm.open_project(info.workspace_path)
    try:
        settlement_io.import_settlement_file(
            conn, info.project_id, Path(info.workspace_path), src, direction="upward"
        )
        period_id = conn.execute(
            "SELECT id FROM settlement_periods WHERE project_id=?", (info.project_id,)
        ).fetchone()["id"]
        result = crosscheck.check_period(conn, period_id)
        assert result.status == "match"
        assert result.control_status == "match"
        assert result.verification_level != "sufficient"
        assert any("重复" in note for note in result.notes)
    finally:
        conn.close()


def test_negative_adjustment_requires_human_review_even_when_totals_are_balanced(tmp_path: Path):
    from jiadun.core.engine import crosscheck, settlement_io
    from jiadun.core.models import project as pm

    src = tmp_path / "negative-adjustment.xlsx"
    _negative_adjustment_book(src)
    info = pm.create_project("负数调整闸门", tmp_path / "ws")
    info, conn = pm.open_project(info.workspace_path)
    try:
        settlement_io.import_settlement_file(
            conn, info.project_id, Path(info.workspace_path), src, direction="upward"
        )
        period_id = conn.execute(
            "SELECT id FROM settlement_periods WHERE project_id=?", (info.project_id,)
        ).fetchone()["id"]
        result = crosscheck.check_period(conn, period_id)
        assert result.status == "match"
        assert result.control_status == "match"
        assert result.verification_level != "sufficient"
        assert any("负数" in note or "调整" in note for note in result.notes)
    finally:
        conn.close()


def test_reimporting_same_source_does_not_duplicate_canonical_rows(tmp_path: Path):
    """相同文件重复选择不能把同一批明细再次追加到当前期次。"""
    from jiadun.core.engine import aggregate, crosscheck, settlement_io

    info, conn, _report, period_id = _import_project(tmp_path)
    source = tmp_path / "第1期结算.xlsx"
    workspace = Path(info.workspace_path)
    try:
        before = {
            "items": conn.execute(
                "SELECT COUNT(*) AS c FROM line_items WHERE period_id=?", (period_id,)
            ).fetchone()["c"],
            "sheets": conn.execute(
                "SELECT COUNT(*) AS c FROM raw_sheets WHERE period_id=?", (period_id,)
            ).fetchone()["c"],
        }
        settlement_io.import_settlement_file(
            conn, info.project_id, workspace, source, direction="upward"
        )
        after = {
            "items": conn.execute(
                "SELECT COUNT(*) AS c FROM line_items WHERE period_id=?", (period_id,)
            ).fetchone()["c"],
            "sheets": conn.execute(
                "SELECT COUNT(*) AS c FROM raw_sheets WHERE period_id=?", (period_id,)
            ).fetchone()["c"],
        }
        assert after == before
        result = crosscheck.check_period(conn, period_id)
        assert result.path_a_total == Decimal("500")
        assert result.control_status == "match"
        aggs = aggregate.aggregate_project(conn, info.project_id, direction="upward")
        assert sum((item.cum_amount or Decimal("0") for item in aggs), Decimal("0")) == Decimal("500")
    finally:
        conn.close()


def test_missing_raw_amount_is_not_persisted_as_zero_in_coverage_proof(tmp_path: Path):
    from jiadun.core.engine import settlement_io
    from jiadun.core.models import project as pm

    src = tmp_path / "missing-amount.xlsx"
    _all_missing_amount_book(src)
    info = pm.create_project("缺失金额闸门", tmp_path / "ws")
    info, conn = pm.open_project(info.workspace_path)
    try:
        settlement_io.import_settlement_file(
            conn, info.project_id, Path(info.workspace_path), src, direction="upward"
        )
        proof = conn.execute(
            "SELECT raw_amount_total FROM sheet_coverage_proofs WHERE project_id=?",
            (info.project_id,),
        ).fetchone()
        assert proof is not None
        assert proof["raw_amount_total"] is None
    finally:
        conn.close()


def test_internal_blank_or_title_rows_block_green_even_when_totals_match(tmp_path):
    """内部空白/标题行的排除必须进入复核状态，不能仅凭 A/B/C 数字放行。"""
    from jiadun.core.engine import crosscheck, settlement_io
    from jiadun.core.models import project as pm

    src = tmp_path / "内部空白标题.xlsx"
    _internal_blank_title_book(src)
    info = pm.create_project("内部空白标题闸门", tmp_path / "ws")
    info, conn = pm.open_project(info.workspace_path)
    try:
        report = settlement_io.import_settlement_file(
            conn, info.project_id, Path(info.workspace_path), src, direction="upward"
        )
        period_id = conn.execute(
            "SELECT id FROM settlement_periods WHERE project_id=?", (info.project_id,)
        ).fetchone()["id"]
        result = crosscheck.check_period(conn, period_id)

        assert report.status == "partial"
        assert result.status == "match"
        assert result.control_status == "match"
        assert result.verification_level == "insufficient"
        assert result.excluded_blank_rows >= 1
        assert result.excluded_title_rows >= 1
        assert any("空白" in note or "标题" in note for note in result.notes)
    finally:
        conn.close()


def test_anonymized_golden_registry_is_present_and_explicit(tmp_path: Path):
    # 真实案例尚未由用户提供时也必须有受控入口，不能用不存在的目录冒充已覆盖。
    from scripts.golden_regression import load_registry

    registry = Path(__file__).resolve().parents[1] / "anonymized_golden_cases" / "cases.json"
    data = load_registry(registry)
    assert data["schema_version"] == 1
    assert isinstance(data["cases"], list) and data["cases"]
    assert all(case.get("case_kind") == "sanitized_real" for case in data["cases"])
    assert all(case.get("availability") in {"available", "not_available"} for case in data["cases"])
