"""运行契约、成果失效和统一 Finding 的行为测试。"""

import json
from pathlib import Path

from costguard.core.contracts import run_contract
from costguard.core.evidence.finding import Finding
from costguard.core.models import project as project_model


def _project(tmp_path: Path):
    info = project_model.create_project("运行契约测试", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    return info, conn


def test_run_contract_is_stable_and_changes_when_scope_changes(tmp_path):
    info, conn = _project(tmp_path)
    try:
        first = run_contract.ensure_run_contract(conn, info.project_id)
        same = run_contract.ensure_run_contract(conn, info.project_id)
        assert same.signature == first.signature
        assert same.contract_id == first.contract_id
        assert first.components["schema_version"] >= 9
        assert first.components["rules"]["rule_ids"]
        assert run_contract.current_run_signature(conn, info.project_id) == first.signature

        conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction) VALUES (?,?,?,?)",
            (info.project_id, 1, "第1期", "downward"),
        )
        changed = run_contract.ensure_run_contract(conn, info.project_id)
        assert changed.signature != first.signature
        assert conn.execute(
            "SELECT invalidated_at FROM run_contracts WHERE id=?", (first.contract_id,)
        ).fetchone()["invalidated_at"]
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM run_contracts WHERE project_id=?", (info.project_id,)
        ).fetchone()["c"] == 2
    finally:
        conn.close()


def test_export_registry_detects_file_change_and_contract_staleness(tmp_path):
    info, conn = _project(tmp_path)
    try:
        active = run_contract.ensure_run_contract(conn, info.project_id)
        export_path = tmp_path / "审核底稿.xlsx"
        export_path.write_bytes(b"first export")
        export_id = run_contract.register_export(
            conn, info.project_id, "excel_workbook", export_path,
            run_signature=active.signature, metadata={"test": True},
        )
        current = run_contract.export_status(conn, info.project_id, "excel_workbook")
        assert current[0]["id"] == export_id
        assert current[0]["status"] == "current"

        export_path.write_bytes(b"edited outside application")
        assert run_contract.export_status(conn, info.project_id, "excel_workbook")[0]["status"] == "changed"

        conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction) VALUES (?,?,?,?)",
            (info.project_id, 1, "第1期", "upward"),
        )
        run_contract.ensure_run_contract(conn, info.project_id)
        stale = run_contract.export_status(conn, info.project_id, "excel_workbook")
        assert stale[0]["status"] == "stale"
        assert export_path.exists(), "失效登记不应删除原导出文件"
    finally:
        conn.close()


def test_fail_closed_state_persists_across_connections_and_clears_safely(tmp_path):
    """运行级不可用边界必须落到项目侧车，并可由重开连接读取。"""
    info, conn = _project(tmp_path)
    state_path = None
    try:
        state = run_contract.set_fail_closed_state(
            conn,
            info.project_id,
            reason="synthetic database commit failure",
            run_signature="synthetic-signature",
        )
        state_path = run_contract.fail_closed_state_path(conn, info.project_id)

        assert state["status"] == run_contract.FAIL_CLOSED_STATUS
        assert state["persisted"] is True
        assert state["persistence"] == "sidecar"
        assert state_path.is_file()
        assert not run_contract.current_results_available(conn, info.project_id)["available"]
    finally:
        conn.close()

    _reopened, reopened = project_model.open_project(Path(info.workspace_path))
    try:
        availability = run_contract.current_results_available(reopened, info.project_id)
        assert availability["available"] is False
        assert availability["status"] == run_contract.FAIL_CLOSED_STATUS
        assert availability["persisted"] is True
        run_contract.clear_fail_closed_state(reopened, info.project_id)
        assert run_contract.current_results_available(reopened, info.project_id)["available"]
        assert not state_path.exists()
    finally:
        reopened.close()


def test_fail_closed_state_reports_process_only_fallback_when_sidecar_is_unwritable(
    tmp_path, monkeypatch
):
    """侧车无法写入时，同进程仍阻断读取，并明确报告跨进程限制。"""
    info, conn = _project(tmp_path)

    def refuse_sidecar(*_args, **_kwargs):
        raise OSError("synthetic sidecar permission failure")

    monkeypatch.setattr(run_contract, "_write_fail_closed_state_file", refuse_sidecar)
    try:
        state = run_contract.set_fail_closed_state(
            conn, info.project_id, reason="synthetic unavailable boundary"
        )
        assert state["persisted"] is False
        assert state["persistence"] == "process"
        assert state["physical_limitations"]
        assert "当前进程" in "".join(state["physical_limitations"])

        _reopened, reopened = project_model.open_project(Path(info.workspace_path))
        try:
            assert not run_contract.current_results_available(
                reopened, info.project_id
            )["available"]
        finally:
            reopened.close()
    finally:
        run_contract.clear_fail_closed_state(conn, info.project_id)
        conn.close()


def test_compatibility_import_uses_the_implementation_module_object():
    """兼容入口与实际模块必须共享属性，故障注入不能打到分叉引用。"""
    import costguard.core.run_contract as compatibility

    assert compatibility is run_contract


def test_finding_has_stable_identity_and_lossless_fields():
    details = {
        "raw_quantity": "2",
        "raw_unit_price": "100",
        "confidence": "high",
        "detection_mode": "rule",
        "impact": "需复核金额",
        "limitations": ["需要回查原表"],
    }
    first = Finding("demo_rule", "high", "line_item", 7, "示例问题", details)
    same = Finding("demo_rule", "high", "line_item", 7, "示例问题", details)

    assert first.finding_id == same.finding_id
    assert first.fingerprint == same.fingerprint
    assert first.confidence == "high"
    assert first.detection_mode == "rule"
    assert first.raw_values["raw_quantity"] == "2"
    assert first.impact == "需复核金额"
    assert first.limitations == ["需要回查原表"]
    assert json.loads(json.dumps(first.as_record(), ensure_ascii=False, default=str))["finding_id"] == first.finding_id
