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
