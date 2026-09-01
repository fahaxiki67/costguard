"""统一摘要模型测试。"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from jiadun.core.anomalies import coverage
from jiadun.core.contracts import run_contract
from jiadun.core.models import project as project_model
from jiadun.core.reporting import build_project_summary, build_report_model
from jiadun.core.reporting.state import direction_state, project_state
from jiadun.core.reporting.summary import _risk, _statuses, _verification


def test_report_model_has_one_shared_summary_and_no_approval_upgrade(tmp_path: Path):
    info = project_model.create_project("摘要模型测试", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    summary = build_project_summary(conn, info.project_id)
    model = build_report_model(conn, info.project_id)
    assert summary.as_dict() == model.management_summary.as_dict()
    assert model.project_summary is model.management_summary.project_summary
    assert summary.data_cutoff is None
    assert summary.data_cutoff_status == "not_available"
    assert summary.statuses["business_confirmation"] == "not_requested"
    assert summary.statuses["approval"] == "not_requested"
    assert summary.detection_coverage["status"] == "not_started"
    assert summary.aggregate_coverage["status"] == "not_started"
    assert summary.pending["manifest_status"] == "not_available"
    assert summary.version_chain["status"] == "not_started"
    assert summary.version_chain["version_count"] == 0
    assert summary.historical_price_assets["status"] == "not_available"
    assert summary.historical_price_assets["review_only"] is True
    assert summary.statuses["project_status"] == "不可形成项目结论"
    assert summary.statuses["project_status_code"] == "cannot_conclude"
    assert summary.statuses["period_status"] == "校核不充分"
    conn.close()


def test_summary_prioritizes_run_unavailable_over_historical_success(tmp_path: Path):
    """摘要必须明确当前结果不可用，不能把旧签名成功显示为当前成功。"""
    info = project_model.create_project("摘要不可用边界", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction) "
            "VALUES (?, 1, '第1期', 'downward')",
            (info.project_id,),
        )
        run_contract.ensure_run_contract(conn, info.project_id)
        run_contract.set_fail_closed_state(
            conn,
            info.project_id,
            reason="synthetic database is not writable",
        )

        summary = build_project_summary(conn, info.project_id)

        assert summary.run_availability["available"] is False
        assert summary.run_availability["status"] == run_contract.FAIL_CLOSED_STATUS
        assert summary.verification["status"] == run_contract.FAIL_CLOSED_STATUS
        assert summary.statuses["automatic_analysis"] == "failed"
        assert summary.statuses["project_status"] == "不可形成项目结论"
        assert summary.statuses["project_status_code"] == "cannot_conclude"
        assert summary.aggregate_coverage["fail_closed"] is True
        assert summary.aggregate_coverage["status"] != "complete"
    finally:
        conn.close()


def test_verification_rejects_stored_copy_content_drift(tmp_path: Path):
    """摘要读取层必须校验解析器实际消费的存储副本内容。"""
    info = project_model.create_project("源文件副本漂移", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    stored = tmp_path / "stored.xlsx"
    stored.write_bytes(b"source-before")
    digest = run_contract.sha256_file(stored)
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name, sha256,
                   size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'stored.xlsx', ?, ?, 'xlsx', '2026')""",
            (info.project_id, str(stored), str(stored), digest, stored.stat().st_size),
        )
        run_contract.ensure_run_contract(conn, info.project_id)
        stored.write_bytes(b"source-after")

        verification = _verification(conn, info.project_id)

        assert verification["status"] != "sufficient"
        assert any("存储副本内容" in gap for gap in verification["evidence_gaps"])
    finally:
        conn.close()


def test_summary_never_marks_findings_or_unchecked_periods_complete():
    complete_coverage = {"status": coverage.COMPLETE}
    pending = {
        "sheets": 0,
        "matches": 0,
        "anomalies": 0,
        "manifest_status": "not_available",
    }
    risk = {}

    findings = _statuses(
        {"status": "findings", "periods_unchecked": 0},
        risk,
        pending,
        complete_coverage,
        complete_coverage,
        source_files=1,
        period_count=1,
    )
    assert findings["automatic_analysis"] == "partial"

    unchecked = _statuses(
        {"status": "sufficient", "periods_unchecked": 1},
        risk,
        pending,
        complete_coverage,
        complete_coverage,
        source_files=1,
        period_count=2,
    )
    assert unchecked["automatic_analysis"] == "partial"
    assert unchecked["period_status"] == "校核不充分"
    assert unchecked["period_status_code"] == "insufficient"
    assert unchecked["project_status"] == "有条件结论"
    assert unchecked["project_status_code"] == "conditional"


def test_unknown_verification_level_is_fail_closed(tmp_path: Path):
    """数据库中的未来/未知校核级别不能落入绿色 sufficient 分支。"""
    info = project_model.create_project("未知校核级别", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        period_id = conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction) "
            "VALUES (?, 1, '第1期', 'downward')",
            (info.project_id,),
        ).lastrowid
        active = run_contract.ensure_run_contract(conn, info.project_id)
        conn.execute(
            """INSERT INTO crosscheck_results(
                   project_id, period_id, verification_level, status,
                   checked_at, run_signature, run_id
               ) VALUES (?, ?, 'future_level', 'match', '2026', ?, ?)""",
            (info.project_id, period_id, active.signature, active.run_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] in {"not_started", "insufficient"}
        assert verification["status"] != "sufficient"
        assert verification["period_status_code"] == "insufficient"
        assert verification["period_status_code"] == "insufficient"
        assert verification["unknown_levels"] == {"future_level": 1}
    finally:
        conn.close()


def test_verification_requires_current_evidence_for_sufficient_level(tmp_path: Path):
    """伪造 sufficient/match 结果但缺主 Evidence 时必须降为不充分。"""
    info = project_model.create_project("校核证据闸门", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        period_id = conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction) "
            "VALUES (?, 1, '第1期', 'downward')",
            (info.project_id,),
        ).lastrowid
        active = run_contract.ensure_run_contract(conn, info.project_id)
        conn.execute(
            """INSERT INTO crosscheck_results(
                   project_id, period_id, verification_level, status,
                   control_status, coverage_proof_status,
                   checked_at, run_signature, run_id)
               VALUES (?, ?, 'sufficient', 'match', 'match', 'complete',
                       '2026', ?, ?)""",
            (info.project_id, period_id, active.signature, active.run_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["evidence_complete"] is False
        assert verification["evidence_gaps"]
        assert verification["status"] == "insufficient"
        assert verification["period_status_code"] == "insufficient"
    finally:
        conn.close()


def test_verification_requires_run_id_and_signature_pair(tmp_path: Path):
    """只伪造结果表签名、保留当前 run_id 时也必须退出当前读取面。"""
    info, conn, period_id, _sheet_id, _proof_id, _proof_evidence_id, _proof = _coverage_gate_fixture(tmp_path)
    try:
        active = run_contract.get_current_contract(conn, info.project_id)
        assert active is not None
        conn.execute(
            "UPDATE crosscheck_results SET run_signature='forged-signature' "
            "WHERE project_id=? AND period_id=?",
            (info.project_id, period_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] in {"not_started", "insufficient"}
        assert verification["status"] != "sufficient"
        assert verification["period_status_code"] == "insufficient"
        assert verification["periods_checked"] == 0
        assert verification["periods_unchecked"] == 1
    finally:
        conn.close()


def test_verification_rechecks_c_control_source_amount(tmp_path: Path):
    """C Evidence 即使保留真实定位，金额被篡改也不能保持 sufficient。"""
    info, conn, period_id, _sheet_id, _proof_id, _proof_evidence_id, _proof = _coverage_gate_fixture(tmp_path)
    try:
        control_id = conn.execute(
            "SELECT c_control_evidence_id FROM crosscheck_results "
            "WHERE project_id=? AND period_id=?",
            (info.project_id, period_id),
        ).fetchone()[0]
        source = json.loads(
            conn.execute("SELECT sources_json FROM evidence WHERE id=?", (control_id,)).fetchone()[0]
        )
        source[0]["raw_value"] = "999"
        source[0]["value"] = "999"
        source[0]["normalized_value"] = "999"
        conn.execute(
            "UPDATE evidence SET sources_json=? WHERE id=?",
            (json.dumps(source, ensure_ascii=False), control_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert verification["evidence_complete"] is False
        assert any("C 控制来源金额与原始单元格不一致" in gap for gap in verification["evidence_gaps"])
    finally:
        conn.close()


@pytest.mark.parametrize("field", ["control_diff", "diff_ab"])
def test_verification_rejects_nonfinite_path_difference(tmp_path: Path, field: str):
    """NaN/Infinity 只能表示非法存储值，不能穿透 Decimal 状态闸门。"""
    info, conn, period_id, _sheet_id, _proof_id, _proof_evidence_id, _proof = _coverage_gate_fixture(tmp_path)
    try:
        conn.execute(
            f"UPDATE crosscheck_results SET {field}='NaN' WHERE project_id=? AND period_id=?",
            (info.project_id, period_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert verification["evidence_complete"] is False
    finally:
        conn.close()


def test_verification_rejects_detail_row_as_c_control_source(tmp_path: Path):
    """普通明细行即使金额碰巧等于 A，也不能冒充 C 小计/合计控制行。"""
    info, conn, period_id, _sheet_id, _proof_id, _proof_evidence_id, _proof = _coverage_gate_fixture(tmp_path)
    try:
        control_id = conn.execute(
            "SELECT c_control_evidence_id FROM crosscheck_results "
            "WHERE project_id=? AND period_id=?",
            (info.project_id, period_id),
        ).fetchone()[0]
        source = json.loads(
            conn.execute("SELECT sources_json FROM evidence WHERE id=?", (control_id,)).fetchone()[0]
        )
        line_item_id = source[0]["line_item_id"]
        conn.execute(
            "UPDATE line_items SET flags_json='{}' WHERE id=?",
            (line_item_id,),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert any("不是小计/合计行" in gap for gap in verification["evidence_gaps"])
    finally:
        conn.close()


def test_verification_does_not_trust_mutable_c_control_flags(tmp_path: Path):
    """普通明细行即使被 UPDATE 成 subtotal/grand_total 也不能刷绿。"""
    info, conn, period_id, sheet_id, _proof_id, _proof_evidence_id, _proof = _coverage_gate_fixture(tmp_path)
    try:
        control_id = conn.execute(
            "SELECT c_control_evidence_id FROM crosscheck_results "
            "WHERE project_id=? AND period_id=?",
            (info.project_id, period_id),
        ).fetchone()[0]
        detail_id = conn.execute(
            """INSERT INTO line_items(
                   period_id, sheet_id, code, name, quantity, unit_price, amount, flags_json)
               VALUES (?, ?, 'A-1', '明细项', '1', '10', '10', ?) RETURNING id""",
            (period_id, sheet_id, json.dumps({"row": 2})),
        ).fetchone()[0]
        source = json.loads(
            conn.execute("SELECT sources_json FROM evidence WHERE id=?", (control_id,)).fetchone()[0]
        )
        source[0]["line_item_id"] = detail_id
        source[0]["row"] = 2
        conn.execute(
            "UPDATE evidence SET sources_json=? WHERE id=?",
            (json.dumps(source, ensure_ascii=False), control_id),
        )
        first = _verification(conn, info.project_id)
        assert first["status"] == "insufficient"
        assert any("不可变逐行分类" in gap for gap in first["evidence_gaps"])

        conn.execute(
            "UPDATE line_items SET flags_json=? WHERE id=?",
            (json.dumps({"row": 2, "subtotal": True, "grand_total": True}), detail_id),
        )
        second = _verification(conn, info.project_id)
        assert second["status"] == "insufficient"
        assert any("不可变逐行分类" in gap for gap in second["evidence_gaps"])
    finally:
        conn.close()


def test_verification_rejects_source_scoped_c_control_evidence(tmp_path: Path):
    """原始 scope 且无运行身份的 Evidence 不能充当当前 C 控制证明。"""
    info, conn, period_id, _sheet_id, _proof_id, _proof_evidence_id, _proof = _coverage_gate_fixture(tmp_path)
    try:
        control_id = conn.execute(
            "SELECT c_control_evidence_id FROM crosscheck_results "
            "WHERE project_id=? AND period_id=?",
            (info.project_id, period_id),
        ).fetchone()[0]
        source_json = conn.execute(
            "SELECT sources_json FROM evidence WHERE id=?", (control_id,)
        ).fetchone()[0]
        source_evidence_id = conn.execute(
            """INSERT INTO evidence(
                   project_id, kind, summary, sources_json, created_at,
                   scope, run_id, run_signature)
               VALUES (?, 'line_item_source', '旧原始来源', ?, '2026', 'source', NULL, NULL)
               RETURNING id""",
            (info.project_id, source_json),
        ).fetchone()[0]
        conn.execute(
            "UPDATE crosscheck_results SET c_control_evidence_id=? "
            "WHERE project_id=? AND period_id=?",
            (source_evidence_id, info.project_id, period_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert any("未绑定当前 Run Contract" in gap for gap in verification["evidence_gaps"])
    finally:
        conn.close()


def test_verification_rejects_nonempty_raw_row_outside_coverage_range(tmp_path: Path):
    """表头后的新增非空原始行必须使旧窄范围证明退出 sufficient。"""
    info, conn, period_id, sheet_id, _proof_id, _proof_evidence_id, _proof = _coverage_gate_fixture(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="raw sheet grid immutable"):
            conn.execute("UPDATE raw_sheets SET n_rows=4 WHERE id=?", (sheet_id,))
        # 模拟 v43 之前的旧库/绕过数据库触发器：读取层仍必须识别越界原始行。
        conn.execute("DROP TRIGGER trg_raw_cells_grid_scope_guard")
        conn.execute(
            """INSERT INTO raw_cells(sheet_id, row, col, raw_value, cached_value)
               VALUES (?, 4, ?, ?, ?)""",
            (sheet_id, 1, "B-EXTRA", "B-EXTRA"),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert any("非空原始行未被覆盖" in gap for gap in verification["evidence_gaps"])
        assert period_id in {
            int(row["period_id"])
            for row in conn.execute(
                "SELECT period_id FROM crosscheck_results WHERE project_id=?",
                (info.project_id,),
            ).fetchall()
        }
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("original_name", "forged.xlsx"),
        ("original_path", "/forged/source.xlsx"),
        ("imported_at", "2099-01-01T00:00:00"),
    ],
)
def test_verification_rejects_source_file_identity_drift_if_trigger_is_bypassed(
    tmp_path: Path, field: str, value: str
):
    """旧库/绕过 v43 触发器改写文件身份时，读取层仍须降级。"""
    info, conn, _period_id, _sheet_id, _proof_id, _proof_evidence_id, _proof = _coverage_gate_fixture(tmp_path)
    try:
        conn.execute("DROP TRIGGER trg_source_files_identity_immutable_update")
        conn.execute(
            f"UPDATE source_files SET {field}=? WHERE project_id=?",
            (value, info.project_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] in {"insufficient", "unavailable"}
        assert any("源文件身份与当前 Run Contract 不一致" in gap for gap in verification["evidence_gaps"])
    finally:
        conn.close()


def test_verification_rejects_unbound_source_file(tmp_path: Path):
    """新增但未纳入当前合同的源文件不能继续沿用旧充分结果。"""
    info, conn, _period_id, _sheet_id, _proof_id, _proof_evidence_id, _proof = (
        _coverage_gate_fixture(tmp_path)
    )
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name, sha256,
                   size_bytes, file_type, imported_at)
               VALUES (?, '/extra.xlsx', '/extra.xlsx', 'extra.xlsx', ?, 1, 'xlsx', '2026')""",
            (info.project_id, f"extra-digest-{info.project_id}"),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] in {"insufficient", "unavailable"}
        assert verification["evidence_complete"] is False
        assert any("未纳入 Run Contract" in gap for gap in verification["evidence_gaps"])
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "伪造后的期次标题"),
        ("contract_party", "伪造合同方"),
        ("tax_mode", "incl_tax"),
        ("note", "伪造备注"),
    ],
)
def test_verification_rejects_period_scope_drift(
    tmp_path: Path, field: str, value: str
):
    """期次业务元数据漂移时，旧 A/B/C 结果不得继续显示充分。"""
    info, conn, period_id, _sheet_id, _proof_id, _proof_evidence_id, _proof = (
        _coverage_gate_fixture(tmp_path)
    )
    try:
        conn.execute(
            f"UPDATE settlement_periods SET {field}=? WHERE id=?", (value, period_id)
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert any("settlement_periods 期次快照" in gap for gap in verification["evidence_gaps"])
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("n_rows", 4),
        ("n_cols", 6),
        ("sheet_name", "伪造Sheet名"),
        ("merged_ranges_json", '["A1:B1"]'),
    ],
)
def test_verification_rejects_sheet_scope_drift(
    tmp_path: Path, field: str, value: object
):
    """Sheet 名称、网格边界和结构元数据漂移时必须降级。"""
    info, conn, _period_id, sheet_id, _proof_id, _proof_evidence_id, _proof = (
        _coverage_gate_fixture(tmp_path)
    )
    try:
        conn.execute("DROP TRIGGER trg_raw_sheets_grid_immutable_update")
        conn.execute(f"UPDATE raw_sheets SET {field}=? WHERE id=?", (value, sheet_id))
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert any("raw_sheets 范围快照" in gap for gap in verification["evidence_gaps"])
    finally:
        conn.close()


def test_verification_rejects_raw_cell_content_drift(tmp_path: Path):
    """即使绕过旧库 raw_cells 触发器，原始网格内容漂移也必须可见。"""
    info, conn, _period_id, sheet_id, _proof_id, _proof_evidence_id, _proof = (
        _coverage_gate_fixture(tmp_path)
    )
    try:
        conn.execute("DROP TRIGGER trg_raw_cells_immutable_update")
        conn.execute(
            "UPDATE raw_cells SET raw_value=?, cached_value=? WHERE sheet_id=? AND row=2 AND col=1",
            ("伪造编码", "伪造编码", sheet_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert any("raw_sheets 范围快照" in gap for gap in verification["evidence_gaps"])
    finally:
        conn.close()


def test_verification_rejects_line_item_drift_after_current_run(tmp_path: Path):
    """当前运行后直接新增清单行，不能继续复用旧 A/B/C 结果。"""
    info, conn, period_id, sheet_id, _proof_id, _proof_evidence_id, _proof = _coverage_gate_fixture(tmp_path)
    try:
        conn.execute(
            """INSERT INTO line_items(
                   period_id, sheet_id, code, name, quantity, unit_price, amount, flags_json)
               VALUES (?, ?, 'NEW', '新增真实明细', '1', '999', '999', '{}')""",
            (period_id, sheet_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert any("line_items 数据指纹与当前 Run Contract 不一致" in gap for gap in verification["evidence_gaps"])
    finally:
        conn.close()


def test_verification_rejects_latest_header_reinterpretation(tmp_path: Path):
    """追加伪造最新表头并配套伪造 proof，不能改变当前运行的字段语义。"""
    from jiadun.core.parsing import coverage_proof
    from jiadun.core.parsing.header_detect import HeaderDetection

    info, conn, period_id, sheet_id, proof_id, proof_evidence_id, _proof = _coverage_gate_fixture(tmp_path)
    try:
        active = run_contract.get_current_contract(conn, info.project_id)
        assert active is not None
        conn.execute(
            """INSERT INTO table_headers(
                   sheet_id, header_row_lo, header_row_hi, col_map_json, confidence, needs_review,
                   data_row_start, data_row_end, data_range_status, data_range_method)
               VALUES (?, 1, 1, ?, 1, 0, 2, 3, 'confirmed', 'manual_confirmation')""",
            (sheet_id, json.dumps({
                "code": 1, "name": 2, "quantity": 3, "unit_price": 4, "amount": 4,
            })),
        )
        cells = {
            (int(row["row"]), int(row["col"])): str(row["raw_value"] or "")
            for row in conn.execute(
                "SELECT row, col, raw_value FROM raw_cells WHERE sheet_id=?",
                (sheet_id,),
            ).fetchall()
            if row["raw_value"] not in (None, "")
        }
        det = HeaderDetection(
            sheet_index=0,
            header_row_lo=1,
            header_row_hi=1,
            col_map={"code": 1, "name": 2, "quantity": 3, "unit_price": 4, "amount": 4},
            confidence=1,
            needs_review=False,
        )
        spoof_proof, spoof_rows = coverage_proof.build_sheet_coverage_proof(
            cells, det, 3, data_range=(2, 3)
        )
        source = conn.execute(
            "SELECT file_id, batch_id FROM sheet_coverage_proofs WHERE id=?",
            (proof_id,),
        ).fetchone()
        coverage_proof.persist_sheet_coverage_proof(
            conn,
            project_id=info.project_id,
            file_id=source["file_id"],
            batch_id=source["batch_id"],
            sheet_id=sheet_id,
            period_id=period_id,
            direction="upward",
            proof=spoof_proof,
            rows=spoof_rows,
            run_signature=active.signature,
            run_id=active.run_id,
            evidence_id=proof_evidence_id,
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert any("table_headers 映射数量与当前 Run Contract 不一致" in gap for gap in verification["evidence_gaps"])
    finally:
        conn.close()


def test_verification_rejects_swapped_row_semantics_in_new_proof(tmp_path: Path):
    """交换明细与合计分类但保持金额/计数自洽时，仍须回到原始网格判定。"""
    from jiadun.core.parsing import coverage_proof

    info, conn, period_id, sheet_id, proof_id, proof_evidence_id, _proof = _coverage_gate_fixture(tmp_path)
    try:
        active = run_contract.get_current_contract(conn, info.project_id)
        assert active is not None
        source = conn.execute(
            "SELECT file_id, batch_id FROM sheet_coverage_proofs WHERE id=?",
            (proof_id,),
        ).fetchone()
        swapped = coverage_proof.SheetCoverageProof(
            raw_row_start=2,
            raw_row_end=3,
            raw_col_start=1,
            raw_col_end=5,
            raw_data_row_count=2,
            classified_row_count=2,
            counts={"detail": 1, "grand_total": 1},
            raw_amount_total="10",
            detail_amount_total="10",
            business_rows_used=1,
            proof_status="complete",
            ab_row_set_status="same_row_set",
            ab_row_set_hash=hashlib.sha256(b"[3]").hexdigest(),
            ab_independence_level="shared_extractor",
        )
        coverage_proof.persist_sheet_coverage_proof(
            conn,
            project_id=info.project_id,
            file_id=source["file_id"],
            batch_id=source["batch_id"],
            sheet_id=sheet_id,
            period_id=period_id,
            direction="upward",
            proof=swapped,
            rows=[
                coverage_proof.RowClassification(
                    row_number=2,
                    class_code="grand_total",
                    reason_code="grand_total_label",
                    source_range={"row_start": 2, "row_end": 2, "col_start": 1, "col_end": 5},
                    raw_values={
                        "code": "A-1", "name": "明细项", "quantity": "1",
                        "unit_price": "10", "amount": "10",
                    },
                    effective_amount="10",
                ),
                coverage_proof.RowClassification(
                    row_number=3,
                    class_code="detail",
                    reason_code="mapped_detail_row",
                    source_range={"row_start": 3, "row_end": 3, "col_start": 1, "col_end": 5},
                    raw_values={"name": "合计", "amount": "10"},
                    effective_amount="10",
                    participates_in_a=True,
                    participates_in_b=True,
                ),
            ],
            run_signature=active.signature,
            run_id=active.run_id,
            evidence_id=proof_evidence_id,
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert any("分类与原始网格不一致" in gap for gap in verification["evidence_gaps"])
        assert any("不可变逐行分类与选择规则不一致" in gap for gap in verification["evidence_gaps"])
    finally:
        conn.close()


def test_verification_rejects_forged_row_source_range(tmp_path: Path):
    """逐行来源定位被改成虚构行列时，Evidence 链必须失效。"""
    from jiadun.core.parsing import coverage_proof

    info, conn, period_id, sheet_id, proof_id, proof_evidence_id, _proof = _coverage_gate_fixture(tmp_path)
    try:
        active = run_contract.get_current_contract(conn, info.project_id)
        assert active is not None
        source = conn.execute(
            "SELECT file_id, batch_id FROM sheet_coverage_proofs WHERE id=?",
            (proof_id,),
        ).fetchone()
        forged = coverage_proof.SheetCoverageProof(
            raw_row_start=2,
            raw_row_end=3,
            raw_col_start=1,
            raw_col_end=5,
            raw_data_row_count=2,
            classified_row_count=2,
            counts={"detail": 1, "grand_total": 1},
            raw_amount_total="10",
            detail_amount_total="10",
            business_rows_used=1,
            proof_status="complete",
            ab_row_set_status="same_row_set",
            ab_row_set_hash=hashlib.sha256(b"[2]").hexdigest(),
            ab_independence_level="shared_extractor",
        )
        coverage_proof.persist_sheet_coverage_proof(
            conn,
            project_id=info.project_id,
            file_id=source["file_id"],
            batch_id=source["batch_id"],
            sheet_id=sheet_id,
            period_id=period_id,
            direction="upward",
            proof=forged,
            rows=[
                coverage_proof.RowClassification(
                    row_number=2,
                    class_code="detail",
                    reason_code="mapped_detail_row",
                    source_range={"row_start": 99, "row_end": 99, "col_start": 99, "col_end": 99},
                    raw_values={
                        "code": "A-1", "name": "明细项", "quantity": "1",
                        "unit_price": "10", "amount": "10",
                    },
                    calculated_amount="10",
                    effective_amount="10",
                    participates_in_a=True,
                    participates_in_b=True,
                ),
                coverage_proof.RowClassification(
                    row_number=3,
                    class_code="grand_total",
                    reason_code="grand_total_label",
                    source_range={"row_start": 3, "row_end": 3, "col_start": 1, "col_end": 5},
                    raw_values={"name": "合计", "amount": "10"},
                    effective_amount="10",
                ),
            ],
            run_signature=active.signature,
            run_id=active.run_id,
            evidence_id=proof_evidence_id,
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert any("来源范围与原始网格不一致" in gap for gap in verification["evidence_gaps"])
    finally:
        conn.close()


def test_verification_rejects_unrelated_current_evidence(tmp_path: Path):
    """当前运行的无关 Evidence 不能冒充该期 cross_check 证明。"""
    from jiadun.core.evidence import evidence as evidence_api

    info = project_model.create_project("校核证据类型闸门", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        active = run_contract.ensure_run_contract(conn, info.project_id)
        unrelated = evidence_api.add_evidence(
            conn,
            info.project_id,
            "source_probe",
            "当前运行但无关的证据",
            sources=[{"period_id": period_id}],
            run_signature=active.signature,
            run_id=active.run_id,
            scope="current",
        )
        conn.execute(
            """INSERT INTO crosscheck_results(
                   project_id, period_id, verification_level, status,
                   control_status, coverage_proof_status, evidence_id,
                   checked_at, run_signature, run_id)
               VALUES (?, ?, 'sufficient', 'match', 'not_available', 'unproven',
                       ?, '2026', ?, ?)""",
            (info.project_id, period_id, unrelated, active.signature, active.run_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["evidence_complete"] is False
        assert "cross_check" in " ".join(verification["evidence_gaps"])
        assert verification["status"] == "insufficient"
    finally:
        conn.close()


def test_verification_rejects_unrelated_c_control_evidence_kind(tmp_path: Path):
    """C 控制值即使指向当前期次，也必须来自明细行来源 Evidence。"""
    from jiadun.core.evidence import evidence as evidence_api

    info = project_model.create_project("C 控制证据类型闸门", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        active = run_contract.ensure_run_contract(conn, info.project_id)
        main = evidence_api.add_evidence(
            conn,
            info.project_id,
            "cross_check",
            "当前期次 A/B/C 主证据",
            sources=[{"period_id": period_id}],
            run_signature=active.signature,
            run_id=active.run_id,
            scope="current",
        )
        wrong_kind = evidence_api.add_evidence(
            conn,
            info.project_id,
            "source_probe",
            "同一期但不是 C 控制来源",
            sources=[{"period_id": period_id}],
            run_signature=active.signature,
            run_id=active.run_id,
            scope="current",
        )
        conn.execute(
            """INSERT INTO crosscheck_results(
                   project_id, period_id, verification_level, status,
                   control_status, c_control_evidence_id, coverage_proof_status,
                   evidence_id, checked_at, run_signature, run_id)
               VALUES (?, ?, 'sufficient', 'match', 'match', ?, 'unproven',
                       ?, '2026', ?, ?)""",
            (info.project_id, period_id, wrong_kind, main, active.signature, active.run_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["evidence_complete"] is False
        assert "line_item_source" in " ".join(verification["evidence_gaps"])
        assert verification["status"] == "insufficient"
    finally:
        conn.close()


def _coverage_gate_fixture(tmp_path: Path):
    """建立一个只含一张结算 Sheet 的最小完整覆盖证明场景。"""
    from jiadun.core.evidence import evidence as evidence_api
    from jiadun.core.parsing import coverage_proof

    info = project_model.create_project("覆盖证明期次闸门", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    period_id = conn.execute(
        """INSERT INTO settlement_periods(project_id, period_no, title, direction)
           VALUES (?, 1, '第1期', 'upward') RETURNING id""",
        (info.project_id,),
    ).fetchone()[0]
    stored = tmp_path / "source.xlsx"
    stored.write_bytes(b"synthetic coverage fixture source")
    stored_digest = hashlib.sha256(stored.read_bytes()).hexdigest()
    source_file_id = conn.execute(
        """INSERT INTO source_files(
               project_id, original_path, stored_path, original_name, sha256,
               size_bytes, file_type, imported_at)
           VALUES (?, '/source.xlsx', ?, 'source.xlsx', ?, ?, 'xlsx', '2026')
           RETURNING id""",
        (info.project_id, str(stored), stored_digest, stored.stat().st_size),
    ).fetchone()[0]
    batch_id = conn.execute(
        """INSERT INTO parse_batches(file_id, parser, parsed_at, status)
           VALUES (?, 'test', '2026', 'ok') RETURNING id""",
        (source_file_id,),
    ).fetchone()[0]
    sheet_id = conn.execute(
        """INSERT INTO raw_sheets(
               batch_id, sheet_index, sheet_name, n_rows, n_cols)
           VALUES (?, 0, '第1期明细', 3, 5) RETURNING id""",
        (batch_id,),
    ).fetchone()[0]
    conn.execute("UPDATE raw_sheets SET period_id=? WHERE id=?", (period_id, sheet_id))
    conn.executemany(
        """INSERT INTO raw_cells(sheet_id, row, col, raw_value, cached_value)
           VALUES (?, ?, ?, ?, ?)""",
        [
            (sheet_id, 2, 1, "A-1", "A-1"),
            (sheet_id, 2, 2, "明细项", "明细项"),
            (sheet_id, 2, 3, "1", "1"),
            (sheet_id, 2, 4, "10", "10"),
            (sheet_id, 2, 5, "10", "10"),
            (sheet_id, 3, 1, "", ""),
            (sheet_id, 3, 2, "合计", "合计"),
            (sheet_id, 3, 3, "", ""),
            (sheet_id, 3, 4, "", ""),
            (sheet_id, 3, 5, "10", "10"),
        ],
    )
    conn.execute(
        """INSERT INTO table_headers(
               sheet_id, header_row_lo, header_row_hi, col_map_json, confidence, needs_review,
               data_row_start, data_row_end, data_range_status, data_range_method)
           VALUES (?, 1, 1, ?, 1, 0, 2, 3, 'confirmed', 'manual_confirmation')""",
        (sheet_id, json.dumps({
            "code": 1, "name": 2, "quantity": 3, "unit_price": 4, "amount": 5,
        })),
    )
    control_line_item_id = conn.execute(
        """INSERT INTO line_items(
               period_id, sheet_id, code, name, quantity, unit_price, amount, flags_json)
           VALUES (?, ?, '', '合计', NULL, NULL, '10', ?) RETURNING id""",
        (period_id, sheet_id, json.dumps({
            "subtotal": True, "grand_total": True, "row": 3,
        })),
    ).fetchone()[0]
    # 清单行属于 A 路径数据指纹，必须在当前 Run Contract 形成前写入；
    # 后续控制 Evidence/覆盖证明都绑定同一份完整输入快照。
    active = run_contract.ensure_run_contract(conn, info.project_id)
    main_evidence_id = evidence_api.add_evidence(
        conn,
        info.project_id,
        "cross_check",
        "第1期 A/B/C 主证据",
        sources=[{"period_id": period_id, "period": 1, "direction": "upward"}],
        run_signature=active.signature,
        run_id=active.run_id,
        scope="current",
    )
    proof_evidence_id = evidence_api.add_evidence(
        conn,
        info.project_id,
        "sheet_coverage_proof",
        "第1期 Sheet 覆盖证明",
        sources=[{
            "sheet_id": sheet_id,
            "period_id": period_id,
            "period": 1,
            "direction": "upward",
        }],
        run_signature=active.signature,
        run_id=active.run_id,
        scope="current",
    )
    control_evidence_id = evidence_api.add_evidence(
        conn,
        info.project_id,
        "line_item_source",
        "第1期 C 控制来源",
        sources=[{
            "file_id": source_file_id,
            "file": "source.xlsx",
            "sheet_id": sheet_id,
            "sheet": "第1期明细",
            "period_id": period_id,
            "period": 1,
            "direction": "upward",
            "field": "amount",
            "line_item_id": control_line_item_id,
            "row": 3,
            "col": 5,
            "raw_value": "10",
            "value": "10",
            "selection_rule": "唯一合计级行（flags.grand_total=true）",
        }],
        run_signature=active.signature,
        run_id=active.run_id,
        scope="current",
    )
    proof = coverage_proof.SheetCoverageProof(
        raw_row_start=2,
        raw_row_end=3,
        raw_col_start=1,
        raw_col_end=5,
        raw_data_row_count=2,
        classified_row_count=2,
        counts={"detail": 1, "grand_total": 1},
        raw_amount_total="10",
        detail_amount_total="10",
        business_rows_used=1,
        proof_status="complete",
        ab_row_set_hash=hashlib.sha256(b"[2]").hexdigest(),
    )
    proof_id = coverage_proof.persist_sheet_coverage_proof(
        conn,
        project_id=info.project_id,
        file_id=source_file_id,
        batch_id=batch_id,
        sheet_id=sheet_id,
        period_id=period_id,
        direction="upward",
        proof=proof,
        rows=[coverage_proof.RowClassification(
            row_number=2,
            class_code="detail",
            reason_code="mapped_detail_row",
            source_range={"row_start": 2, "row_end": 2, "col_start": 1, "col_end": 5},
            raw_values={
                "code": "A-1", "name": "明细项", "quantity": "1",
                "unit_price": "10", "amount": "10",
            },
            calculated_amount="10",
            effective_amount="10",
            participates_in_a=True,
            participates_in_b=True,
        ), coverage_proof.RowClassification(
            row_number=3,
            class_code="grand_total",
            reason_code="grand_total_label",
            source_range={"row_start": 3, "row_end": 3, "col_start": 1, "col_end": 5},
            raw_values={"name": "合计", "amount": "10"},
            effective_amount="10",
        )],
        run_signature=active.signature,
        run_id=active.run_id,
        evidence_id=proof_evidence_id,
    )
    conn.execute(
        """INSERT INTO crosscheck_results(
               project_id, period_id, verification_level, status,
               detail_rows, pending_sheets, classified_detail_rows, business_rows_used,
               path_a_total, path_b_total, raw_subtotal, diff_ab, ab_status,
               control_status, control_diff, c_control_evidence_id,
               coverage_proof_status, evidence_id, checked_at, run_signature, run_id)
           VALUES (?, ?, 'sufficient', 'match', 1, 0, 1, 1, '10', '10', '10', '0', 'match', 'match', '0', ?, 'complete',
                   ?, '2026', ?, ?)""",
        (
            info.project_id,
            period_id,
            control_evidence_id,
            main_evidence_id,
            active.signature,
            active.run_id,
        ),
    )
    conn.commit()
    return info, conn, period_id, sheet_id, proof_id, proof_evidence_id, proof


def test_verification_requires_complete_current_proof_per_period_and_sheet(tmp_path: Path):
    """覆盖证明的状态、类型、来源和期次/Sheet 粒度缺一不可。"""
    info, conn, period_id, sheet_id, proof_id, proof_evidence_id, proof = _coverage_gate_fixture(tmp_path)
    try:
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "sufficient"
        assert verification["evidence_complete"] is True

        main_evidence_id = conn.execute(
            "SELECT id FROM evidence WHERE project_id=? AND kind='cross_check' ORDER BY id DESC LIMIT 1",
            (info.project_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE evidence SET sources_json=? WHERE id=?",
            (json.dumps([{"period_id": period_id, "period": 1, "direction": "downward"}]), main_evidence_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert "对应期次或方向" in " ".join(verification["evidence_gaps"])
        conn.execute(
            "UPDATE evidence SET sources_json=?, scope='human' WHERE id=?",
            (json.dumps([{"period_id": period_id, "period": 1, "direction": "upward"}]), main_evidence_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert "主 Evidence 非当前自动范围" in " ".join(verification["evidence_gaps"])
        conn.execute(
            "UPDATE evidence SET scope='current' WHERE id=?", (main_evidence_id,)
        )

        # C 控制不可用时，即使 A/B 主 Evidence 与逐 Sheet 覆盖证明完整，
        # 也不得把结果表中伪造的 sufficient 直接提升为项目充分校核。
        conn.execute(
            "UPDATE crosscheck_results SET control_status='not_available', "
            "control_diff=NULL, c_control_evidence_id=NULL "
            "WHERE project_id=? AND period_id=?",
            (info.project_id, period_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert "C 控制状态不是 match" in " ".join(verification["evidence_gaps"])
        conn.execute(
            "UPDATE crosscheck_results SET control_status='match', control_diff='0', "
            "c_control_evidence_id=(SELECT id FROM evidence WHERE project_id=? "
            "AND kind='line_item_source' ORDER BY id DESC LIMIT 1) "
            "WHERE project_id=? AND period_id=?",
            (info.project_id, info.project_id, period_id),
        )

        conn.execute(
            "UPDATE crosscheck_results SET status='diff' WHERE project_id=? AND period_id=?",
            (info.project_id, period_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert "A/B 状态不是 match" in " ".join(verification["evidence_gaps"])
        conn.execute(
            "UPDATE crosscheck_results SET status='match' WHERE project_id=? AND period_id=?",
            (info.project_id, period_id),
        )

        conn.execute(
            "UPDATE evidence SET sources_json=? WHERE id=(SELECT c_control_evidence_id "
            "FROM crosscheck_results WHERE project_id=? AND period_id=?)",
            (json.dumps([{"period_id": period_id, "direction": "upward"}]), info.project_id, period_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert "缺少文件、Sheet、行列" in " ".join(verification["evidence_gaps"])

        # 结果表的 coverage 状态本身也不能被伪造为 unproven 后继续沿用
        # sufficient；项目级读取必须独立执行 fail-closed 闸门。
        conn.execute(
            "UPDATE crosscheck_results SET coverage_proof_status='unproven' "
            "WHERE project_id=? AND period_id=?",
            (info.project_id, period_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert "状态不是 complete" in " ".join(verification["evidence_gaps"])
        conn.execute(
            "UPDATE crosscheck_results SET coverage_proof_status='complete' "
            "WHERE project_id=? AND period_id=?",
            (info.project_id, period_id),
        )

        # 覆盖证明是不可变快照；用追加一份异常状态快照模拟损坏/未完成
        # 证明，再追加原始 complete 快照恢复当前读取面，不能直接 UPDATE。
        from dataclasses import replace

        from jiadun.core.parsing import coverage_proof

        bad_proof = replace(proof, proof_status="unproven", proof_reason=["synthetic"])
        bad_proof_id = coverage_proof.persist_sheet_coverage_proof(
            conn,
            project_id=info.project_id,
            file_id=conn.execute(
                "SELECT file_id FROM sheet_coverage_proofs WHERE id=?", (proof_id,)
            ).fetchone()[0],
            batch_id=conn.execute(
                "SELECT batch_id FROM sheet_coverage_proofs WHERE id=?", (proof_id,)
            ).fetchone()[0],
            sheet_id=sheet_id,
            period_id=period_id,
            direction="upward",
            proof=bad_proof,
            rows=[],
            run_signature=run_contract.get_current_contract(conn, info.project_id).signature,
            run_id=run_contract.get_current_contract(conn, info.project_id).run_id,
            evidence_id=proof_evidence_id,
        )
        assert bad_proof_id > proof_id
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert "覆盖证明未完成" in " ".join(verification["evidence_gaps"])

        proof_row = conn.execute(
            "SELECT file_id, batch_id FROM sheet_coverage_proofs WHERE id=?", (proof_id,)
        ).fetchone()
        proof_id = coverage_proof.persist_sheet_coverage_proof(
            conn,
            project_id=info.project_id,
            file_id=proof_row["file_id"],
            batch_id=proof_row["batch_id"],
            sheet_id=sheet_id,
            period_id=period_id,
            direction="upward",
            proof=proof,
            rows=[],
            run_signature=run_contract.get_current_contract(conn, info.project_id).signature,
            run_id=run_contract.get_current_contract(conn, info.project_id).run_id,
            evidence_id=proof_evidence_id,
        )
        conn.execute(
            "UPDATE evidence SET kind='source_probe' WHERE id=?",
            (proof_evidence_id,),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert "Evidence 类型错误" in " ".join(verification["evidence_gaps"])

        conn.execute(
            "UPDATE evidence SET kind='sheet_coverage_proof', sources_json=? WHERE id=?",
            (json.dumps([{"sheet_id": sheet_id, "period_id": period_id + 99}]), proof_evidence_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert "未定位到对应期次和 Sheet" in " ".join(verification["evidence_gaps"])
    finally:
        conn.close()


def test_verification_rejects_proof_values_not_matching_raw_cells(tmp_path: Path):
    """不能同时伪造覆盖证明和 A/B 数字把错误原始值变成 sufficient。"""
    from dataclasses import replace

    from jiadun.core.contracts import run_contract
    from jiadun.core.parsing import coverage_proof

    info, conn, period_id, sheet_id, proof_id, proof_evidence_id, proof = _coverage_gate_fixture(tmp_path)
    try:
        active = run_contract.get_current_contract(conn, info.project_id)
        assert active is not None
        fake = replace(proof, raw_amount_total="999", detail_amount_total="999")
        fake_id = coverage_proof.persist_sheet_coverage_proof(
            conn,
            project_id=info.project_id,
            file_id=conn.execute(
                "SELECT file_id FROM sheet_coverage_proofs WHERE id=?", (proof_id,)
            ).fetchone()[0],
            batch_id=conn.execute(
                "SELECT batch_id FROM sheet_coverage_proofs WHERE id=?", (proof_id,)
            ).fetchone()[0],
            sheet_id=sheet_id,
            period_id=period_id,
            direction="upward",
            proof=fake,
            rows=[coverage_proof.RowClassification(
                row_number=2,
                class_code="detail",
                reason_code="mapped_detail_row",
                source_range={"row_start": 2, "row_end": 2, "col_start": 1, "col_end": 5},
                raw_values={
                    "code": "A-1", "name": "明细项", "quantity": "1",
                    "unit_price": "999", "amount": "999",
                },
                calculated_amount="999",
                effective_amount="999",
                participates_in_a=True,
                participates_in_b=True,
            )],
            run_signature=active.signature,
            run_id=active.run_id,
            evidence_id=proof_evidence_id,
        )
        assert fake_id > proof_id
        conn.execute(
            """UPDATE crosscheck_results
               SET path_a_total='999', path_b_total='999', diff_ab='0', control_diff='0'
               WHERE project_id=? AND period_id=?""",
            (info.project_id, period_id),
        )
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert verification["evidence_complete"] is False
        assert "原始单元格不一致" in " ".join(verification["evidence_gaps"])
    finally:
        conn.close()


def test_verification_ignores_historical_coverage_snapshot_after_latest_rebuild(tmp_path: Path):
    """同一运行追加新证明后，旧快照不能反向阻断当前完整证明。"""
    from dataclasses import replace

    from jiadun.core.parsing import coverage_proof

    info, conn, period_id, sheet_id, proof_id, proof_evidence_id, proof = _coverage_gate_fixture(tmp_path)
    try:
        active = run_contract.get_current_contract(conn, info.project_id)
        assert active is not None
        stale = replace(proof, raw_amount_total="999", detail_amount_total="999")
        stale_id = coverage_proof.persist_sheet_coverage_proof(
            conn,
            project_id=info.project_id,
            file_id=conn.execute(
                "SELECT file_id FROM sheet_coverage_proofs WHERE id=?", (proof_id,)
            ).fetchone()[0],
            batch_id=conn.execute(
                "SELECT batch_id FROM sheet_coverage_proofs WHERE id=?", (proof_id,)
            ).fetchone()[0],
            sheet_id=sheet_id,
            period_id=period_id,
            direction="upward",
            proof=stale,
            rows=[coverage_proof.RowClassification(
                row_number=2,
                class_code="detail",
                reason_code="mapped_detail_row",
                source_range={"row_start": 2, "row_end": 2, "col_start": 1, "col_end": 5},
                raw_values={
                    "code": "A-1", "name": "明细项", "quantity": "1",
                    "unit_price": "999", "amount": "999",
                },
                calculated_amount="999",
                effective_amount="999",
                participates_in_a=True,
                participates_in_b=True,
            )],
            run_signature=active.signature,
            run_id=active.run_id,
            evidence_id=proof_evidence_id,
        )
        assert stale_id > proof_id
        restored_id = coverage_proof.persist_sheet_coverage_proof(
            conn,
            project_id=info.project_id,
            file_id=conn.execute(
                "SELECT file_id FROM sheet_coverage_proofs WHERE id=?", (proof_id,)
            ).fetchone()[0],
            batch_id=conn.execute(
                "SELECT batch_id FROM sheet_coverage_proofs WHERE id=?", (proof_id,)
            ).fetchone()[0],
            sheet_id=sheet_id,
            period_id=period_id,
            direction="upward",
            proof=proof,
            rows=[coverage_proof.RowClassification(
                row_number=2,
                class_code="detail",
                reason_code="mapped_detail_row",
                source_range={"row_start": 2, "row_end": 2, "col_start": 1, "col_end": 5},
                raw_values={
                    "code": "A-1", "name": "明细项", "quantity": "1",
                    "unit_price": "10", "amount": "10",
                },
                calculated_amount="10",
                effective_amount="10",
                participates_in_a=True,
                participates_in_b=True,
            ), coverage_proof.RowClassification(
                row_number=3,
                class_code="grand_total",
                reason_code="grand_total_label",
                source_range={"row_start": 3, "row_end": 3, "col_start": 1, "col_end": 5},
                    raw_values={"name": "合计", "amount": "10"},
                    effective_amount="10",
            )],
            run_signature=active.signature,
            run_id=active.run_id,
            evidence_id=proof_evidence_id,
        )
        assert restored_id > stale_id
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "sufficient"
        assert verification["evidence_complete"] is True
        assert not any("999" in gap for gap in verification["evidence_gaps"])
    finally:
        conn.close()


def test_raw_grid_evidence_is_immutable_after_import(tmp_path: Path):
    """原始网格一旦落库，直接 SQL 改写/删除也不能制造伪来源。"""
    info, conn, _period_id, sheet_id, _proof_id, _proof_evidence_id, _proof = _coverage_gate_fixture(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="raw cell immutable"):
            conn.execute(
                "UPDATE raw_cells SET raw_value='999' WHERE sheet_id=? AND row=2 AND col=5",
                (sheet_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="raw cell immutable"):
            conn.execute(
                "DELETE FROM raw_cells WHERE sheet_id=? AND row=2 AND col=5",
                (sheet_id,),
            )
    finally:
        conn.close()


def test_complete_coverage_proof_with_unresolved_reason_is_not_sufficient(tmp_path: Path):
    """完整证明不能携带解析器声明的未解决原因。"""
    from dataclasses import replace

    from jiadun.core.parsing import coverage_proof

    info, conn, period_id, sheet_id, proof_id, proof_evidence_id, proof = _coverage_gate_fixture(tmp_path)
    try:
        active = run_contract.get_current_contract(conn, info.project_id)
        assert active is not None
        forged = replace(proof, proof_status="complete", proof_reason=["unresolved_rows_present"])
        forged_id = coverage_proof.persist_sheet_coverage_proof(
            conn,
            project_id=info.project_id,
            file_id=conn.execute(
                "SELECT file_id FROM sheet_coverage_proofs WHERE id=?", (proof_id,)
            ).fetchone()[0],
            batch_id=conn.execute(
                "SELECT batch_id FROM sheet_coverage_proofs WHERE id=?", (proof_id,)
            ).fetchone()[0],
            sheet_id=sheet_id,
            period_id=period_id,
            direction="upward",
            proof=forged,
            rows=[coverage_proof.RowClassification(
                row_number=2,
                class_code="detail",
                reason_code="mapped_detail_row",
                source_range={"row_start": 2, "row_end": 2, "col_start": 1, "col_end": 5},
                raw_values={
                    "code": "A-1", "name": "明细项", "quantity": "1",
                    "unit_price": "10", "amount": "10",
                },
                calculated_amount="10",
                effective_amount="10",
                participates_in_a=True,
                participates_in_b=True,
            )],
            run_signature=active.signature,
            run_id=active.run_id,
            evidence_id=proof_evidence_id,
        )
        assert forged_id > proof_id
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        assert "完整覆盖证明仍保留未解决原因" in " ".join(verification["evidence_gaps"])
    finally:
        conn.close()


def test_complete_coverage_proof_rejects_inconsistent_row_flags(tmp_path: Path):
    """逐行待确认/解析失败标志与分类不一致时必须降级。"""
    from dataclasses import replace

    from jiadun.core.parsing import coverage_proof

    info, conn, period_id, sheet_id, proof_id, proof_evidence_id, proof = _coverage_gate_fixture(tmp_path)
    try:
        active = run_contract.get_current_contract(conn, info.project_id)
        assert active is not None
        forged = replace(proof, proof_status="complete", proof_reason=[])
        forged_id = coverage_proof.persist_sheet_coverage_proof(
            conn,
            project_id=info.project_id,
            file_id=conn.execute(
                "SELECT file_id FROM sheet_coverage_proofs WHERE id=?", (proof_id,)
            ).fetchone()[0],
            batch_id=conn.execute(
                "SELECT batch_id FROM sheet_coverage_proofs WHERE id=?", (proof_id,)
            ).fetchone()[0],
            sheet_id=sheet_id,
            period_id=period_id,
            direction="upward",
            proof=forged,
            rows=[coverage_proof.RowClassification(
                row_number=2,
                class_code="detail",
                reason_code="mapped_detail_row",
                source_range={"row_start": 2, "row_end": 2, "col_start": 1, "col_end": 5},
                raw_values={
                    "code": "A-1", "name": "明细项", "quantity": "1",
                    "unit_price": "10", "amount": "10",
                },
                calculated_amount="10",
                effective_amount="10",
                participates_in_a=True,
                participates_in_b=True,
                is_pending=True,
                is_parse_failed=True,
            )],
            run_signature=active.signature,
            run_id=active.run_id,
            evidence_id=proof_evidence_id,
        )
        assert forged_id > proof_id
        verification = _verification(conn, info.project_id)
        assert verification["status"] == "insufficient"
        gaps = " ".join(verification["evidence_gaps"])
        assert "待确认标志与分类不一致" in gaps
        assert "解析失败标志与分类不一致" in gaps
    finally:
        conn.close()


def test_unknown_finding_lifecycle_is_not_processed(tmp_path: Path):
    """未来/损坏生命周期码必须显示为待确认，而不能按 legacy resolved 关闭。"""
    from jiadun.core.evidence.finding_lifecycle import lifecycle_status

    info = project_model.create_project("未知 Finding 生命周期", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        active = run_contract.ensure_run_contract(conn, info.project_id)
        anomaly_id = conn.execute(
            """INSERT INTO anomalies(
                   project_id, rule_id, severity, subject_type, subject_id, message,
                   status, lifecycle_status, created_at, run_signature, run_id)
               VALUES (?, 'future', 'high', 'project', ?, '未来状态',
                       'resolved', 'future_code', '2026', ?, ?) RETURNING id""",
            (info.project_id, info.project_id, active.signature, active.run_id),
        ).fetchone()[0]
        row = conn.execute("SELECT * FROM anomalies WHERE id=?", (anomaly_id,)).fetchone()
        assert lifecycle_status(row) == "unknown"
        risk = _risk(conn, info.project_id)
        assert risk["lifecycle_status_counts"] == {"unknown": 1}
        assert risk["status"]["processed"] == 0
        assert risk["status"]["pending"] == 1
        assert risk["pending_severity"]["high"] == 1
    finally:
        conn.close()


def test_project_state_requires_direction_snapshot():
    """有期次但缺少方向快照时，纯状态 API 也必须 fail-closed。"""
    result = project_state(
        source_files=1,
        period_count=1,
        run_available=True,
        current_periods_checked=1,
        period_code="sufficient",
        direction_states={},
        detection_complete=True,
        aggregate_complete=True,
        pending_count=0,
        manifest_blocked=False,
    )
    assert result["code"] == "conditional"
    assert "direction_scope_incomplete" in result["reason_codes"]


def test_unknown_direction_cannot_be_complete_project_scope():
    """未确认方向不能因局部数字完整而变成完整有效或可形成结论。"""
    direction = direction_state(
        direction="unknown",
        periods_total=1,
        periods_checked=1,
        sufficient=1,
        findings=0,
        insufficient=0,
        run_available=True,
        coverage_complete=True,
    )
    assert direction["code"] == "none"
    result = project_state(
        source_files=1,
        period_count=1,
        run_available=True,
        current_periods_checked=1,
        period_code="sufficient",
        direction_states={"unknown": direction},
        detection_complete=True,
        aggregate_complete=True,
        pending_count=0,
        manifest_blocked=False,
    )
    assert result["code"] == "conditional"
    assert "direction_scope_incomplete" in result["reason_codes"]

    malformed = project_state(
        source_files=1,
        period_count=1,
        run_available=True,
        current_periods_checked=1,
        period_code="sufficient",
        direction_states={"unknown": {"code": "complete"}},
        detection_complete=True,
        aggregate_complete=True,
        pending_count=0,
        manifest_blocked=False,
    )
    assert malformed["code"] == "conditional"
    assert "direction_scope_incomplete" in malformed["reason_codes"]


def test_parse_failed_sheet_blocks_project_conclusion():
    """持久化的解析失败 Sheet 不能在完整局部结果下形成项目结论。"""
    result = _statuses(
        {"status": "sufficient", "periods_total": 1, "periods_checked": 1, "periods_unchecked": 0},
        {},
        {"sheets": 0, "matches": 0, "anomalies": 0, "manifest_status": "not_available"},
        {"status": "complete"},
        {"status": "complete"},
        source_files=1,
        period_count=1,
        run_availability={"available": True},
        direction_states={"upward": {"code": "complete", "label": "完整有效"}},
        sheet_states=[{"sheet_id": 1, "code": "parse_failed", "label": "解析失败"}],
    )
    assert result["project_status_code"] == "cannot_conclude"
    assert "sheet_parse_failed" in result["project_status_reason_codes"]


def test_read_only_summary_has_no_manifest_or_derived_flag_writes(tmp_path: Path):
    """项目列表的只读摘要不得在 SQLite mode=ro 下触发任何隐式写入。"""
    import hashlib
    import sqlite3

    from jiadun.core.parsing.import_manifest import ManifestEntrySpec, create_manifest

    info = project_model.create_project("摘要只读路径", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    db = Path(info.workspace_path) / "project.db"
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO line_items(
                   period_id, code, name, unit, quantity, unit_price, amount, flags_json
               ) VALUES (?, 'A-1', '只读测试项', 'm3', '2', '3', '5', '{}')""",
            (period_id,),
        )
        payload = b"readonly manifest source"
        digest = hashlib.sha256(payload).hexdigest()
        stored = tmp_path / "source.xlsx"
        stored.write_bytes(payload)
        conn.execute(
            """INSERT INTO source_files(
                       project_id, original_path, stored_path, original_name, sha256,
                       size_bytes, file_type, imported_at
                   ) VALUES (?, '/source.xlsx', ?, 'source.xlsx', ?, ?, 'xlsx', '2026')""",
                (info.project_id, str(stored), digest, len(payload)),
        )
        create_manifest(
            conn,
            info.project_id,
            "readonly-manifest",
            [ManifestEntrySpec("one", expected_name="source.xlsx", expected_sha256=digest)],
        )
        active = run_contract.ensure_run_contract(conn, info.project_id)
        before = conn.execute(
            "SELECT status FROM import_manifests WHERE project_id=?", (info.project_id,)
        ).fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    ro = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    try:
        model = build_report_model(ro, info.project_id, read_only=True)
        summary = model.project_summary
        assert summary.run_id == active.run_id
        assert summary.pending["manifest_status"] in {"complete", "incomplete", "mismatch", "not_available"}
    finally:
        ro.close()

    check = sqlite3.connect(db)
    try:
        assert check.execute(
            "SELECT status FROM import_manifests WHERE project_id=?", (info.project_id,)
        ).fetchone()[0] == before
        assert check.execute(
            "SELECT flags_json FROM line_items WHERE period_id=?", (period_id,)
        ).fetchone()[0] == "{}"
    finally:
        check.close()


def test_partial_coverage_cannot_clear_pending_fail_closed_state(tmp_path: Path):
    info = project_model.create_project("部分覆盖清除门控", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        contract = run_contract.ensure_run_contract(conn, info.project_id)
        run_contract.set_fail_closed_state(conn, info.project_id, reason="partial run")
        run_id = coverage.record_detection_run(
            conn,
            info.project_id,
            coverage.coverage_from_values(["a", "b"], ["a"]),
            run_signature=contract.signature,
            run_kind=coverage.AGGREGATE_VALIDATION,
        )
        run_contract.defer_fail_closed_state_clear(
            conn,
            info.project_id,
            run_signature=contract.signature,
            coverage_run_id=run_id,
            coverage_run_kind=coverage.AGGREGATE_VALIDATION,
        )
        assert not run_contract.current_results_available(conn, info.project_id)["available"]
    finally:
        conn.close()


def test_declarative_complete_coverage_cannot_clear_without_business_results(tmp_path: Path):
    """项目有期次时，形状正确的 coverage 不能替代真实 A/B/C 结果。"""
    info = project_model.create_project("声明式覆盖不能清除", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        with conn:
            conn.executemany(
                """INSERT INTO settlement_periods(project_id, period_no, title, direction)
                   VALUES (?, ?, ?, ?)""",
                [
                    (info.project_id, 1, "第1期", "upward"),
                    (info.project_id, 2, "第2期", "downward"),
                ],
            )
        contract = run_contract.ensure_run_contract(conn, info.project_id)
        run_contract.set_fail_closed_state(conn, info.project_id, reason="proof binding")
        expected = list(
            run_contract._aggregate_expected_coverage_keys(conn, info.project_id)
        )
        with pytest.raises(ValueError, match="只能由 run_crosscheck"):
            coverage.record_detection_run(
                conn,
                info.project_id,
                coverage.coverage_from_values(expected, expected),
                run_signature=contract.signature,
                run_kind=coverage.AGGREGATE_VALIDATION,
            )
        with conn:
            cursor = conn.execute(
                """INSERT INTO detection_runs(
                       project_id, run_signature, run_kind, started_at, completed_at,
                       status, expected_json, executed_json, skipped_json, failed_json,
                       critical_failed_json, error_summary, metadata_json)
                   VALUES (?, ?, ?, '2026', '2026', 'complete', ?, ?, '{}', '{}', '[]', NULL, '{}')""",
                (
                    info.project_id,
                    contract.signature,
                    coverage.AGGREGATE_VALIDATION,
                    json.dumps(expected),
                    json.dumps(expected),
                ),
            )
        run_id = int(cursor.lastrowid)

        with pytest.raises(RuntimeError, match="完整成功运行证明"):
            run_contract.clear_fail_closed_state(
                conn,
                info.project_id,
                run_signature=contract.signature,
                coverage_run_id=run_id,
                coverage_run_kind=coverage.AGGREGATE_VALIDATION,
            )
        assert not run_contract.current_results_available(conn, info.project_id)["available"]
    finally:
        conn.close()


def test_fail_closed_recovery_kind_is_fixed(tmp_path: Path):
    info = project_model.create_project("固定恢复依据", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        with pytest.raises(ValueError, match="只能由 aggregate_validation"):
            run_contract.set_fail_closed_state(
                conn,
                info.project_id,
                reason="invalid recovery kind",
                recovery_run_kind=coverage.ANOMALY_DETECTION,
            )
        assert run_contract.get_fail_closed_state(conn, info.project_id) is None
    finally:
        conn.close()


def test_complete_coverage_clears_pending_state_after_outer_commit(tmp_path: Path):
    """完整成功覆盖应在外层提交后清除 pending，不得因字段读取崩溃。"""
    info = project_model.create_project("完整覆盖清除门控", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        contract = run_contract.ensure_run_contract(conn, info.project_id)
        run_contract.set_fail_closed_state(
            conn, info.project_id, run_signature=contract.signature, reason="pending success"
        )
        run_id = coverage.record_detection_run(
            conn,
            info.project_id,
            coverage.coverage_from_values(["a", "b"], ["a", "b"]),
            run_signature=contract.signature,
            run_kind=coverage.AGGREGATE_VALIDATION,
        )
        run_contract.defer_fail_closed_state_clear(
            conn,
            info.project_id,
            run_signature=contract.signature,
            coverage_run_id=run_id,
            coverage_run_kind=coverage.AGGREGATE_VALIDATION,
        )

        availability = run_contract.current_results_available(conn, info.project_id)

        assert availability["available"] is True
        assert run_contract.get_fail_closed_state(conn, info.project_id) is None
        assert not run_contract.fail_closed_state_path(conn, info.project_id).exists()
    finally:
        conn.close()


def test_clear_requires_strict_current_latest_complete_proof(tmp_path: Path):
    """无证明、伪造额外执行项和旧 complete 行均不能解除边界。"""
    info = project_model.create_project("严格覆盖证明", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        contract = run_contract.ensure_run_contract(conn, info.project_id)
        run_contract.set_fail_closed_state(
            conn, info.project_id, run_signature=contract.signature, reason="proof probe"
        )
        first_id = coverage.record_detection_run(
            conn,
            info.project_id,
            coverage.coverage_from_values(["a"], ["a"]),
            run_signature=contract.signature,
            run_kind=coverage.AGGREGATE_VALIDATION,
        )
        conn.execute(
            "UPDATE detection_runs SET executed_json=? WHERE id=?",
            ('["a", "forged"]', first_id),
        )
        with pytest.raises(RuntimeError, match="完整成功运行证明"):
            run_contract.clear_fail_closed_state(conn, info.project_id)
        with pytest.raises(RuntimeError, match="完整成功运行证明"):
            run_contract.clear_fail_closed_state(
                conn,
                info.project_id,
                run_signature=contract.signature,
                coverage_run_id=first_id,
                coverage_run_kind=coverage.AGGREGATE_VALIDATION,
            )

        second_id = coverage.record_detection_run(
            conn,
            info.project_id,
            coverage.coverage_from_values(["a"], []),
            run_signature=contract.signature,
            run_kind=coverage.AGGREGATE_VALIDATION,
        )
        assert second_id > first_id
        with pytest.raises(RuntimeError, match="完整成功运行证明"):
            run_contract.clear_fail_closed_state(
                conn,
                info.project_id,
                run_signature=contract.signature,
                coverage_run_id=first_id,
                coverage_run_kind=coverage.AGGREGATE_VALIDATION,
            )
        assert not run_contract.current_results_available(conn, info.project_id)["available"]
    finally:
        conn.close()


def test_corrupt_fail_closed_sidecar_requires_success_proof_to_clear(tmp_path: Path):
    info = project_model.create_project("损坏状态清除门控", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    try:
        contract = run_contract.ensure_run_contract(conn, info.project_id)
        run_contract.set_fail_closed_state(conn, info.project_id, reason="corrupt probe")
        state_path = run_contract.fail_closed_state_path(conn, info.project_id)
        state_path.write_text("{broken", encoding="utf-8")
        assert run_contract.get_fail_closed_state(conn, info.project_id)["corrupt"] is True
        with pytest.raises(RuntimeError, match="完整成功运行证明"):
            run_contract.clear_fail_closed_state(conn, info.project_id)
        assert state_path.exists()
        run_id = coverage.record_detection_run(
            conn,
            info.project_id,
            coverage.coverage_from_values(["a"], ["a"]),
            run_signature=contract.signature,
            run_kind=coverage.AGGREGATE_VALIDATION,
        )
        run_contract.clear_fail_closed_state(
            conn,
            info.project_id,
            run_signature=contract.signature,
            coverage_run_id=run_id,
            coverage_run_kind=coverage.AGGREGATE_VALIDATION,
        )
        assert not state_path.exists()
    finally:
        conn.close()
