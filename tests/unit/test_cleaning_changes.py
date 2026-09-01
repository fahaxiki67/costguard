"""清洗变更事件的证据、审计和不改原值测试。"""

from pathlib import Path

from jiadun.core.cleaning.changes import decide_change, list_changes, propose_change
from jiadun.core.contracts import run_contract
from jiadun.core.models import project as project_model


def test_cleaning_change_lifecycle_keeps_business_value_untouched(tmp_path: Path):
    info = project_model.create_project("清洗事件测试", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    first = run_contract.ensure_run_contract(conn, info.project_id)
    change = propose_change(
        conn,
        info.project_id,
        subject_type="line_item",
        subject_id=99,
        field_name="name",
        before="原名称",
        proposed="标准名称",
        actor="复核人",
        reason="同一来源文件中存在明确的空格差异",
    )
    assert change.status == "proposed"
    decided = decide_change(
        conn,
        info.project_id,
        change.change_id,
        status="accepted",
        actor="负责人",
        reason="接受建议，待人工按流程处理",
    )
    assert decided.status == "accepted"
    current = run_contract.get_current_contract(conn, info.project_id)
    assert current is not None and current.run_id != first.run_id
    assert run_contract.ensure_run_contract(conn, info.project_id).run_id == current.run_id
    bindings = conn.execute(
        """SELECT c.run_signature, c.run_id, e.run_signature AS evidence_signature,
                  e.run_id AS evidence_run_id, a.run_signature AS audit_signature,
                  a.run_id AS audit_run_id
           FROM cleaning_changes c
           JOIN evidence e ON e.id=c.evidence_id
           JOIN audit_log a ON a.id=c.audit_id
           WHERE c.id=?""",
        (change.change_id,),
    ).fetchone()
    assert tuple(bindings) == (
        current.signature, current.run_id,
        current.signature, current.run_id,
        current.signature, current.run_id,
    )
    assert list_changes(conn, info.project_id)[0].proposed == "标准名称"
    assert conn.execute(
        "SELECT COUNT(*) FROM line_items WHERE id=?", (99,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE project_id=? AND kind LIKE 'cleaning_change%'",
        (info.project_id,),
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE project_id=? AND action LIKE '%cleaning_change'",
        (info.project_id,),
    ).fetchone()[0] == 2
    conn.close()
