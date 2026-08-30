"""权威批次清单与到件状态测试。"""

import hashlib
from pathlib import Path

from costguard.core.contracts import run_contract
from costguard.core.models import project as project_model
from costguard.core.parsing.import_manifest import (
    ManifestEntrySpec,
    assess_manifest,
    bind_manifest_entry,
    create_manifest,
)


def _project(tmp_path: Path):
    info = project_model.create_project("批次清单测试", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    return info, conn


def _source(conn, project_id: int, name: str, content: bytes) -> int:
    digest = hashlib.sha256(content).hexdigest()
    cur = conn.execute(
        """INSERT INTO source_files(
               project_id, original_path, stored_path, original_name, sha256,
               size_bytes, file_type, imported_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (project_id, f"/{name}", f"/{name}", name, digest, len(content), "xlsx", "2026-08-31"),
    )
    return int(cur.lastrowid)


def test_manifest_does_not_reverse_generate_missing_items(tmp_path):
    info, conn = _project(tmp_path)
    file_id = _source(conn, info.project_id, "已收到.xlsx", b"received")
    digest = conn.execute("SELECT sha256 FROM source_files WHERE id=?", (file_id,)).fetchone()[0]
    entry_id = None
    manifest_id = create_manifest(
        conn,
        info.project_id,
        "handoff-20260831",
        [
            ManifestEntrySpec("received", expected_name="已收到.xlsx", expected_sha256=digest),
            ManifestEntrySpec("missing", expected_name="应到但缺失.xlsx"),
        ],
        source="项目移交清单",
        declared_by="业务经办人",
    )
    summary = assess_manifest(conn, info.project_id)
    entry_id = next(item["id"] for item in summary["entries"] if item["logical_key"] == "missing")
    assert manifest_id == summary["manifest_id"]
    assert summary["authoritative"] is True
    assert summary["status"] == "incomplete"
    assert summary["present"] == 1
    assert summary["missing"] == 1
    assert entry_id is not None
    # 当前收到文件不能反向增添清单条目。
    assert conn.execute(
        "SELECT COUNT(*) FROM import_manifest_entries WHERE manifest_id=?", (manifest_id,)
    ).fetchone()[0] == 2
    conn.close()


def test_manual_binding_is_atomic_and_audited(tmp_path):
    info, conn = _project(tmp_path)
    file_id = _source(conn, info.project_id, "需要人工绑定.xlsx", b"bind")
    create_manifest(
        conn,
        info.project_id,
        "manual-bind",
        [ManifestEntrySpec("one", expected_name="需要人工绑定.xlsx")],
    )
    entry_id = conn.execute(
        "SELECT e.id FROM import_manifest_entries e JOIN import_manifests m ON m.id=e.manifest_id "
        "WHERE m.project_id=?", (info.project_id,)
    ).fetchone()[0]
    bind_manifest_entry(
        conn, info.project_id, entry_id, file_id, actor="复核人", reason="文件名与移交单逐项核对"
    )
    summary = assess_manifest(conn, info.project_id)
    assert summary["status"] == "complete"
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE project_id=? AND action='bind_manifest_entry'",
        (info.project_id,),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE project_id=? AND kind='manifest_binding'",
        (info.project_id,),
    ).fetchone()[0] == 1
    current = run_contract.get_current_contract(conn, info.project_id)
    evidence = conn.execute(
        "SELECT run_signature FROM evidence WHERE project_id=? AND kind='manifest_binding'",
        (info.project_id,),
    ).fetchone()
    assert current is not None
    assert evidence["run_signature"] == current.signature
    conn.close()


def test_new_manifest_invalidates_existing_run_scope(tmp_path):
    info, conn = _project(tmp_path)
    first = run_contract.ensure_run_contract(conn, info.project_id)
    create_manifest(
        conn,
        info.project_id,
        "new-authoritative-scope",
        [ManifestEntrySpec("one", expected_name="待收到.xlsx")],
    )
    current = run_contract.get_current_contract(conn, info.project_id)
    assert current is not None
    assert current.signature != first.signature
    assert conn.execute(
        "SELECT invalidated_at FROM run_contracts WHERE signature=?", (first.signature,)
    ).fetchone()[0] is not None
    conn.close()
