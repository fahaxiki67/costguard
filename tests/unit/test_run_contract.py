"""运行契约、成果失效和统一 Finding 的行为测试。"""

import json
import sqlite3
from pathlib import Path

import pytest

from jiadun.core.anomalies import coverage
from jiadun.core.contracts import run_contract
from jiadun.core.evidence.finding import Finding
from jiadun.core.models import project as project_model


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
        assert first.components["product_id"] == "jiadun"
        assert first.components["product_name"] == "价盾"
        assert first.components["rules"]["rule_ids"]
        assert first.components["cleaning_rule_version"]
        assert first.components["matching_rule_version"]
        assert first.components["anomaly_rule_version"]
        assert first.components["decimal_precision"] == 34
        assert first.components["decimal_rounding"] == "ROUND_HALF_UP"
        assert first.components["decimal_scale"] == "0.01"
        assert first.components["amount_unit"] == "unknown"
        assert first.components["alias_library_version"]
        assert first.components["human_confirmation_snapshot"] == []
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


def test_contract_input_gaps_use_canonical_component_values(tmp_path):
    """Decimal/set 经 JSON 持久化后仍应与同一规则快照语义相等。"""
    info, conn = _project(tmp_path)
    try:
        active = run_contract.ensure_run_contract(conn, info.project_id)

        assert run_contract.contract_input_gaps(
            conn, info.project_id, active
        ) == []
    finally:
        conn.close()


def test_source_copy_content_drift_invalidates_current_contract(tmp_path):
    """解析器实际读取的存储副本被覆盖时，当前运行不得继续有效。"""
    info, conn = _project(tmp_path)
    stored = tmp_path / "stored.xlsx"
    stored.write_bytes(b"original-content")
    digest = run_contract.sha256_file(stored)
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'stored.xlsx', ?, ?, 'xlsx', '2026')""",
            (info.project_id, str(stored), str(stored), digest, stored.stat().st_size),
        )
        active = run_contract.ensure_run_contract(conn, info.project_id)
        assert run_contract.source_file_contract_gaps(conn, info.project_id, active) == []

        stored.write_bytes(b"tampered-content")
        gaps = run_contract.source_file_contract_gaps(conn, info.project_id, active)
        assert any("存储副本内容" in gap for gap in gaps)
    finally:
        conn.close()


def test_source_copy_drift_removes_old_rows_from_current_scope(tmp_path):
    """源副本内容漂移时，详情读取面不得继续返回旧运行记录。"""
    info, conn = _project(tmp_path)
    stored = tmp_path / "stored.xlsx"
    stored.write_bytes(b"original-content")
    digest = run_contract.sha256_file(stored)
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'stored.xlsx', ?, ?, 'xlsx', '2026')""",
            (info.project_id, str(stored), str(stored), digest, stored.stat().st_size),
        )
        active = run_contract.ensure_run_contract(conn, info.project_id)
        with conn:
            conn.execute(
                """INSERT INTO matches(
                       project_id, group_key, item_ids_json, level, method,
                       score, status, run_signature, run_id)
                   VALUES (?, 'drift-sentinel', '[]', 'probable', 'test',
                           0.5, 'pending', ?, ?)""",
                (info.project_id, active.signature, active.run_id),
            )
        stored.write_bytes(b"tampered-content")

        availability = run_contract.current_results_available(conn, info.project_id)
        assert availability["available"] is False
        scope, params = run_contract.current_scope(conn, info.project_id, "m")
        count = conn.execute(
            f"SELECT COUNT(*) AS c FROM matches m WHERE m.project_id=? AND {scope}",
            (info.project_id, *params),
        ).fetchone()["c"]
        assert count == 0
        for alias in ("cr", "pt", "m", "a", "e"):
            assert run_contract.current_scope(
                conn, info.project_id, alias
            ) == ("1=0", ())
    finally:
        conn.close()


def test_source_metadata_must_match_copy_at_contract_creation(tmp_path):
    """初次建合同必须同时核验登记 SHA-256 和文件大小。"""
    info, conn = _project(tmp_path)
    stored = tmp_path / "stored.xlsx"
    stored.write_bytes(b"content")
    actual_digest = run_contract.sha256_file(stored)
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'stored.xlsx', ?, ?, 'xlsx', '2026')""",
            (
                info.project_id,
                str(stored),
                str(stored),
                "not-a-sha256",
                1,
            ),
        )
        with pytest.raises(RuntimeError, match="源文件"):
            run_contract.ensure_run_contract(conn, info.project_id)
        availability = run_contract.current_results_available(conn, info.project_id)
        assert availability["available"] is False
        assert "SHA-256" in (availability["reason"] or "")

        conn.execute("DELETE FROM source_files WHERE project_id=?", (info.project_id,))
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'stored.xlsx', ?, ?, 'xlsx', '2026')""",
            (
                info.project_id,
                str(stored),
                str(stored),
                actual_digest,
                1,
            ),
        )
        with pytest.raises(RuntimeError, match="源文件"):
            run_contract.ensure_run_contract(conn, info.project_id)
        availability = run_contract.current_results_available(conn, info.project_id)
        assert availability["available"] is False
        assert "文件大小" in (availability["reason"] or "")
    finally:
        conn.close()


def test_non_integer_source_size_is_fail_closed_for_initial_and_existing_contract(
    tmp_path,
):
    """6.9 等可被 int() 截断的大小值不得形成当前合同。"""
    info, conn = _project(tmp_path)
    stored = tmp_path / "stored.xlsx"
    stored.write_bytes(b"123456")
    digest = run_contract.sha256_file(stored)
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'stored.xlsx', ?, ?, 'xlsx', '2026')""",
            (info.project_id, str(stored), str(stored), digest, 6.9),
        )
        with pytest.raises(RuntimeError, match="文件大小无效"):
            run_contract.ensure_run_contract(conn, info.project_id)
        assert run_contract.current_results_available(
            conn, info.project_id
        )["available"] is False

        conn.execute("DELETE FROM source_files WHERE project_id=?", (info.project_id,))
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'stored.xlsx', ?, ?, 'xlsx', '2026')""",
            (info.project_id, str(stored), str(stored), digest, stored.stat().st_size),
        )
        active = run_contract.ensure_run_contract(conn, info.project_id)
        conn.execute("DROP TRIGGER trg_source_files_identity_immutable_update")
        conn.execute(
            "UPDATE source_files SET size_bytes=? WHERE project_id=?",
            (6.9, info.project_id),
        )
        gaps = run_contract.source_file_contract_gaps(
            conn, info.project_id, active
        )
        assert any("文件大小无效" in gap for gap in gaps)
        assert run_contract.current_results_available(
            conn, info.project_id
        )["available"] is False
    finally:
        conn.close()


def test_source_copy_path_redirection_invalidates_current_contract(tmp_path):
    """存储副本被重定向到另一文件时，旧运行必须退出 current。"""
    info, conn = _project(tmp_path)
    stored = tmp_path / "stored.xlsx"
    redirected = tmp_path / "redirected.xlsx"
    stored.write_bytes(b"original-content")
    redirected.write_bytes(b"original-content")
    digest = run_contract.sha256_file(stored)
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'stored.xlsx', ?, ?, 'xlsx', '2026')""",
            (info.project_id, str(stored), str(stored), digest, stored.stat().st_size),
        )
        active = run_contract.ensure_run_contract(conn, info.project_id)
        conn.execute("DROP TRIGGER trg_source_files_identity_immutable_update")
        conn.execute(
            "UPDATE source_files SET stored_path=? WHERE project_id=?",
            (str(redirected), info.project_id),
        )
        gaps = run_contract.source_file_contract_gaps(conn, info.project_id, active)
        assert any("存储副本路径" in gap for gap in gaps)
    finally:
        conn.close()


def test_source_copy_path_relocation_creates_new_run_when_content_is_unchanged(tmp_path):
    """受控项目搬迁只改变副本路径时，旧结果失效并形成新运行。"""
    info, conn = _project(tmp_path)
    stored = tmp_path / "stored.xlsx"
    relocated = tmp_path / "relocated.xlsx"
    stored.write_bytes(b"same-content")
    relocated.write_bytes(b"same-content")
    digest = run_contract.sha256_file(stored)
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'stored.xlsx', ?, ?, 'xlsx', '2026')""",
            (info.project_id, str(stored), str(stored), digest, stored.stat().st_size),
        )
        first = run_contract.ensure_run_contract(conn, info.project_id)
        conn.execute("DROP TRIGGER trg_source_files_identity_immutable_update")
        conn.execute(
            "UPDATE source_files SET stored_path=? WHERE project_id=?",
            (str(relocated), info.project_id),
        )

        second = run_contract.ensure_run_contract(conn, info.project_id)

        assert second.run_id != first.run_id
        assert second.signature != first.signature
        assert conn.execute(
            "SELECT invalidated_at FROM run_contracts WHERE id=?",
            (first.contract_id,),
        ).fetchone()[0]
        assert run_contract.source_file_contract_gaps(
            conn, info.project_id, second
        ) == []
    finally:
        conn.close()


def test_legacy_contract_without_stored_path_is_not_current_but_can_be_rebound(tmp_path):
    """升级旧合同先降级当前读取，再由受控重跑写入副本路径。"""
    info, conn = _project(tmp_path)
    stored = tmp_path / "stored.xlsx"
    stored.write_bytes(b"legacy-content")
    digest = run_contract.sha256_file(stored)
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'stored.xlsx', ?, ?, 'xlsx', '2026')""",
            (info.project_id, str(stored), str(stored), digest, stored.stat().st_size),
        )
        active = run_contract.ensure_run_contract(conn, info.project_id)
        legacy_components = json.loads(
            conn.execute(
                "SELECT components_json FROM run_contracts WHERE id=?",
                (active.contract_id,),
            ).fetchone()[0]
        )
        legacy_components["source_files"][0].pop("stored_path", None)
        legacy_signature = run_contract.compute_run_signature(legacy_components)
        conn.execute("DROP TRIGGER trg_run_contracts_immutable_update")
        conn.execute(
            "UPDATE run_contracts SET signature=?, components_json=? WHERE id=?",
            (
                legacy_signature,
                json.dumps(legacy_components, ensure_ascii=False),
                active.contract_id,
            ),
        )
        legacy = run_contract.get_current_contract(conn, info.project_id)
        assert legacy is not None
        assert any(
            "缺少存储副本路径" in gap
            for gap in run_contract.source_file_contract_gaps(
                conn, info.project_id, legacy
            )
        )

        rebound = run_contract.ensure_run_contract(conn, info.project_id)

        assert rebound.run_id != legacy.run_id
        assert "stored_path" in rebound.components["source_files"][0]
        assert run_contract.source_file_contract_gaps(
            conn, info.project_id, rebound
        ) == []
    finally:
        conn.close()


def test_missing_source_copy_invalidates_current_contract(tmp_path):
    """已登记的存储副本消失时，当前运行必须 fail-closed。"""
    info, conn = _project(tmp_path)
    stored = tmp_path / "stored.xlsx"
    stored.write_bytes(b"original-content")
    digest = run_contract.sha256_file(stored)
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'stored.xlsx', ?, ?, 'xlsx', '2026')""",
            (info.project_id, str(stored), str(stored), digest, stored.stat().st_size),
        )
        active = run_contract.ensure_run_contract(conn, info.project_id)
        stored.unlink()
        gaps = run_contract.source_file_contract_gaps(conn, info.project_id, active)
        assert any("存储副本不存在" in gap for gap in gaps)
    finally:
        conn.close()


def test_missing_source_copy_at_contract_creation_is_fail_closed(tmp_path):
    """合同建立时副本已缺失时不得建立当前运行。"""
    info, conn = _project(tmp_path)
    missing = tmp_path / "missing.xlsx"
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'missing.xlsx', ?, 1, 'xlsx', '2026')""",
            (
                info.project_id,
                str(missing),
                str(missing),
                "a" * 64,
            ),
        )
        with pytest.raises(RuntimeError, match="源文件"):
            run_contract.ensure_run_contract(conn, info.project_id)

        availability = run_contract.current_results_available(
            conn, info.project_id
        )

        assert availability["available"] is False
        assert "存储副本" in (availability["reason"] or "")
    finally:
        conn.close()


def test_non_file_source_copy_at_contract_creation_is_fail_closed(tmp_path):
    """存储路径指向目录等不可读取对象时，合同不得建立当前运行。"""
    info, conn = _project(tmp_path)
    stored_directory = tmp_path / "stored-directory"
    stored_directory.mkdir()
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'stored-directory.xlsx', ?, 1, 'xlsx', '2026')""",
            (
                info.project_id,
                str(stored_directory),
                str(stored_directory),
                "a" * 64,
            ),
        )
        with pytest.raises(RuntimeError, match="源文件"):
            run_contract.ensure_run_contract(conn, info.project_id)
        availability = run_contract.current_results_available(conn, info.project_id)
        assert availability["available"] is False
        assert any(
            "存储副本不存在或不可读取" in gap
            for gap in run_contract.source_file_contract_gaps(
                conn, info.project_id, run_contract.get_current_contract(conn, info.project_id)
            )
        )
    finally:
        conn.close()


def test_invalid_stored_copy_path_at_contract_creation_is_fail_closed(tmp_path):
    """初次建合同遇到 NUL 等非法路径时，必须返回结构化不可用而非抛出路径异常。"""
    info, conn = _project(tmp_path)
    invalid_path = "\x00invalid-source.xlsx"
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'invalid-source.xlsx', ?, 1, 'xlsx', '2026')""",
            (info.project_id, invalid_path, invalid_path, "a" * 64),
        )
        with pytest.raises(RuntimeError, match="源文件"):
            run_contract.ensure_run_contract(conn, info.project_id)
        availability = run_contract.current_results_available(conn, info.project_id)
        assert availability["available"] is False
        assert "路径" in (availability["reason"] or "")
    finally:
        conn.close()


@pytest.mark.parametrize("stored_sha256", [None, "", "not-a-sha256"])
def test_invalid_stored_copy_hash_in_legacy_contract_is_fail_closed(
    tmp_path, stored_sha256
):
    """旧合同的存储副本哈希缺失、为空或格式非法时必须降级。"""
    info, conn = _project(tmp_path)
    stored = tmp_path / "stored.xlsx"
    stored.write_bytes(b"legacy-content")
    digest = run_contract.sha256_file(stored)
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'stored.xlsx', ?, ?, 'xlsx', '2026')""",
            (info.project_id, str(stored), str(stored), digest, stored.stat().st_size),
        )
        active = run_contract.ensure_run_contract(conn, info.project_id)
        components = json.loads(
            conn.execute(
                "SELECT components_json FROM run_contracts WHERE id=?",
                (active.contract_id,),
            ).fetchone()[0]
        )
        components["source_files"][0]["stored_sha256"] = stored_sha256
        legacy_signature = run_contract.compute_run_signature(components)
        conn.execute("DROP TRIGGER trg_run_contracts_immutable_update")
        conn.execute(
            "UPDATE run_contracts SET signature=?, components_json=? WHERE id=?",
            (
                legacy_signature,
                json.dumps(components, ensure_ascii=False),
                active.contract_id,
            ),
        )

        legacy = run_contract.get_current_contract(conn, info.project_id)
        gaps = run_contract.source_file_contract_gaps(conn, info.project_id, legacy)
        availability = run_contract.current_results_available(conn, info.project_id)

        assert any("缺少有效的存储副本 SHA-256" in gap for gap in gaps)
        assert availability["available"] is False
        with pytest.raises(RuntimeError, match="源文件身份不一致"):
            run_contract.ensure_run_contract(conn, info.project_id)
    finally:
        conn.close()


def test_register_export_cannot_create_current_row_when_source_copy_is_missing(tmp_path):
    """直接调用成果登记 API 也必须先通过源文件副本闸门。"""
    info, conn = _project(tmp_path)
    missing = tmp_path / "missing.xlsx"
    export_path = tmp_path / "export.xlsx"
    export_path.write_bytes(b"synthetic export")
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'missing.xlsx', ?, 1, 'xlsx', '2026')""",
            (info.project_id, str(missing), str(missing), "a" * 64),
        )
        with pytest.raises(RuntimeError, match="源文件"):
            run_contract.register_export(
                conn, info.project_id, "excel_workbook", export_path
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM export_runs WHERE project_id=?",
            (info.project_id,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_symlink_loop_is_reported_as_unavailable(tmp_path):
    """存储副本符号链接循环必须降级为 unavailable，不得让状态刷新抛异常。"""
    info, conn = _project(tmp_path)
    stored = tmp_path / "stored.xlsx"
    loop_a = tmp_path / "loop-a"
    loop_b = tmp_path / "loop-b"
    stored.write_bytes(b"original-content")
    digest = run_contract.sha256_file(stored)
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'stored.xlsx', ?, ?, 'xlsx', '2026')""",
            (info.project_id, str(stored), str(stored), digest, stored.stat().st_size),
        )
        run_contract.ensure_run_contract(conn, info.project_id)
        try:
            loop_a.symlink_to(loop_b)
            loop_b.symlink_to(loop_a)
        except OSError as exc:
            pytest.skip(f"当前环境无法创建符号链接（Windows 需管理员或开发者模式）：{exc}")
        conn.execute("DROP TRIGGER trg_source_files_identity_immutable_update")
        conn.execute(
            "UPDATE source_files SET stored_path=? WHERE project_id=?",
            (str(loop_a), info.project_id),
        )

        availability = run_contract.current_results_available(conn, info.project_id)

        assert availability["available"] is False
        assert availability["status"] == run_contract.FAIL_CLOSED_STATUS
        assert "路径" in (availability["reason"] or "") or "存储副本" in (
            availability["reason"] or ""
        )
    finally:
        conn.close()


def test_run_id_is_propagated_to_detection_and_evidence(tmp_path):
    from jiadun.core.evidence import evidence as evidence_api

    info, conn = _project(tmp_path)
    try:
        active = run_contract.ensure_run_contract(conn, info.project_id)
        evidence_id = evidence_api.add_evidence(
            conn, info.project_id, "synthetic_current", "当前运行证据",
            run_signature=active.signature, run_id=active.run_id,
        )
        row = conn.execute(
            "SELECT run_signature, run_id, scope FROM evidence WHERE id=?",
            (evidence_id,),
        ).fetchone()
        assert (row["run_signature"], row["run_id"], row["scope"]) == (
            active.signature, active.run_id, "current"
        )
        human_id = evidence_api.add_evidence(
            conn, info.project_id, "synthetic_human", "人工确认证据",
            run_signature=active.signature, run_id=active.run_id, scope="human",
        )
        assert conn.execute(
            "SELECT scope FROM evidence WHERE id=?", (human_id,)
        ).fetchone()["scope"] == "human"
    finally:
        conn.close()


def test_run_contract_does_not_reactivate_historical_same_signature(tmp_path):
    """输入恢复到旧签名时也必须创建新的不可变运行合同。"""
    info, conn = _project(tmp_path)
    try:
        first = run_contract.ensure_run_contract(conn, info.project_id)
        conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction) VALUES (?,?,?,?)",
            (info.project_id, 1, "第1期", "downward"),
        )
        second = run_contract.ensure_run_contract(conn, info.project_id)
        assert second.signature != first.signature

        conn.execute("DELETE FROM settlement_periods WHERE project_id=?", (info.project_id,))
        third = run_contract.ensure_run_contract(conn, info.project_id)

        assert third.signature == first.signature
        assert third.contract_id not in {first.contract_id, second.contract_id}
        assert conn.execute(
            "SELECT invalidated_at FROM run_contracts WHERE id=?", (first.contract_id,)
        ).fetchone()["invalidated_at"]
        assert conn.execute(
            "SELECT COUNT(*) FROM run_contracts WHERE project_id=?", (info.project_id,)
        ).fetchone()[0] == 3
        assert run_contract.get_current_contract(conn, info.project_id).contract_id == third.contract_id
    finally:
        conn.close()


def test_run_contract_database_guard_rejects_mutation_and_delete(tmp_path):
    """数据库触发器拒绝就地改写或删除历史合同，只允许单向失效。"""
    info, conn = _project(tmp_path)
    try:
        active = run_contract.ensure_run_contract(conn, info.project_id)
        with pytest.raises(sqlite3.IntegrityError, match="run_contract immutable"):
            conn.execute(
                "UPDATE run_contracts SET components_json='{}' WHERE id=?",
                (active.contract_id,),
            )
        # 当前合同仍可按生命周期单向失效；后续 ensure 会创建新行。
        conn.execute(
            "UPDATE run_contracts SET invalidated_at='2026-09-01T00:00:00' WHERE id=?",
            (active.contract_id,),
        )
        assert conn.execute(
            "SELECT invalidated_at FROM run_contracts WHERE id=?", (active.contract_id,)
        ).fetchone()[0] == "2026-09-01T00:00:00"
        with pytest.raises(sqlite3.IntegrityError, match="run_contract immutable"):
            conn.execute(
                "UPDATE run_contracts SET invalidated_at=NULL WHERE id=?",
                (active.contract_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="run_contract immutable"):
            conn.execute("DELETE FROM run_contracts WHERE id=?", (active.contract_id,))
        with pytest.raises(sqlite3.IntegrityError, match="run_contract immutable"):
            conn.execute(
                "UPDATE run_contracts SET invalidated_at='' WHERE id=?",
                (active.contract_id,),
            )
    finally:
        conn.close()


def test_result_replacement_keeps_crosscheck_and_period_total_history(tmp_path):
    """按期次唯一的当前行重跑时，旧 A/B/C 与聚合快照必须可追溯。"""
    info, conn = _project(tmp_path)
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'upward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        active = run_contract.ensure_run_contract(conn, info.project_id)
        conn.execute(
            """INSERT INTO crosscheck_results(
                   project_id, period_id, verification_level, status,
                   path_a_total, path_b_total, notes_json, checked_at,
                   run_signature, run_id
               ) VALUES (?, ?, 'insufficient', 'pending', '100.00', '99.00',
                         '[\"旧范围\"]', '2026-09-01T00:00:00', ?, ?)""",
            (info.project_id, period_id, active.signature, active.run_id),
        )
        conn.execute(
            """INSERT INTO period_totals(
                   project_id, period_id, item_key, amount_sum,
                   cross_check_status, run_signature, run_id
               ) VALUES (?, ?, 'code:A', '100.00', 'pending', ?, ?)""",
            (info.project_id, period_id, active.signature, active.run_id),
        )

        conn.execute(
            "UPDATE crosscheck_results SET status='match', path_a_total='101.00' "
            "WHERE project_id=? AND period_id=?",
            (info.project_id, period_id),
        )
        conn.execute(
            "UPDATE period_totals SET amount_sum='101.00', cross_check_status='match' "
            "WHERE project_id=? AND period_id=? AND item_key='code:A'",
            (info.project_id, period_id),
        )

        cr_history = conn.execute(
            """SELECT source_id, archive_reason, snapshot_json
                 FROM crosscheck_result_history WHERE project_id=?""",
            (info.project_id,),
        ).fetchone()
        assert cr_history["archive_reason"] == "replaced"
        assert json.loads(cr_history["snapshot_json"])["path_a_total"] == "100.00"
        pt_history = conn.execute(
            """SELECT source_id, archive_reason, snapshot_json
                 FROM period_total_history WHERE project_id=?""",
            (info.project_id,),
        ).fetchone()
        assert pt_history["archive_reason"] == "replaced"
        assert json.loads(pt_history["snapshot_json"])["amount_sum"] == "100.00"

        conn.execute(
            "DELETE FROM crosscheck_results WHERE project_id=? AND period_id=?",
            (info.project_id, period_id),
        )
        conn.execute(
            "DELETE FROM period_totals WHERE project_id=? AND period_id=? AND item_key='code:A'",
            (info.project_id, period_id),
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM crosscheck_result_history WHERE project_id=?",
            (info.project_id,),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM period_total_history WHERE project_id=?",
            (info.project_id,),
        ).fetchone()[0] == 2
        history_row = conn.execute(
            "SELECT history_id, snapshot_json FROM crosscheck_result_history "
            "WHERE project_id=? ORDER BY history_id LIMIT 1",
            (info.project_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="crosscheck result history immutable"):
            conn.execute(
                "UPDATE crosscheck_result_history SET snapshot_json='{}' WHERE history_id=?",
                (history_row["history_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="crosscheck result history immutable"):
            conn.execute(
                "DELETE FROM crosscheck_result_history WHERE history_id=?",
                (history_row["history_id"],),
            )
        total_history_row = conn.execute(
            "SELECT history_id FROM period_total_history WHERE project_id=? "
            "ORDER BY history_id LIMIT 1",
            (info.project_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="period total history immutable"):
            conn.execute(
                "UPDATE period_total_history SET snapshot_json='{}' WHERE history_id=?",
                (total_history_row["history_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="period total history immutable"):
            conn.execute(
                "DELETE FROM period_total_history WHERE history_id=?",
                (total_history_row["history_id"],),
            )
    finally:
        conn.close()


def test_current_scope_uses_run_id_when_signature_repeats(tmp_path):
    """相同输入签名的历史运行不得重新进入当前读取面。"""
    info, conn = _project(tmp_path)
    try:
        first = run_contract.ensure_run_contract(conn, info.project_id)
        conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction) VALUES (?,?,?,?)",
            (info.project_id, 1, "第1期", "downward"),
        )
        second = run_contract.ensure_run_contract(conn, info.project_id)
        conn.execute("DELETE FROM settlement_periods WHERE project_id=?", (info.project_id,))
        third = run_contract.ensure_run_contract(conn, info.project_id)
        assert third.signature == first.signature
        with conn:
            conn.execute(
                """INSERT INTO matches(
                           project_id, group_key, item_ids_json, level, method,
                           score, status, run_signature, run_id)
                       VALUES (?, 'old', '[]', 'probable', 'test', 0.5,
                               'pending', ?, ?)""",
                (info.project_id, first.signature, first.run_id),
            )
            conn.execute(
                """INSERT INTO matches(
                           project_id, group_key, item_ids_json, level, method,
                           score, status, run_signature, run_id)
                       VALUES (?, 'current', '[]', 'probable', 'test', 0.5,
                               'pending', ?, ?)""",
                (info.project_id, third.signature, third.run_id),
            )
        scope, params = run_contract.current_scope(conn, info.project_id, "m")
        count = conn.execute(
            f"SELECT COUNT(*) AS c FROM matches m WHERE m.project_id=? AND {scope}",
            (info.project_id, *params),
        ).fetchone()["c"]
        assert count == 1
        assert conn.execute(
            "SELECT group_key FROM matches WHERE run_id=?", (third.run_id,)
        ).fetchone()["group_key"] == "current"
        assert second.run_id != third.run_id
    finally:
        conn.close()


def test_current_evidence_scope_keeps_source_and_excludes_historical_run(tmp_path):
    """Evidence 当前读取面同时识别来源证据与当前 run，排除旧运行。"""
    from jiadun.core.evidence import evidence as evidence_api

    info, conn = _project(tmp_path)
    try:
        first = run_contract.ensure_run_contract(conn, info.project_id)
        source_id = evidence_api.add_evidence(
            conn, info.project_id, "source_probe", "原始来源证据"
        )
        old_id = evidence_api.add_evidence(
            conn,
            info.project_id,
            "current_probe",
            "旧运行证据",
            run_signature=first.signature,
            run_id=first.run_id,
            scope="current",
        )
        conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction) "
            "VALUES (?, 1, '第1期', 'downward')",
            (info.project_id,),
        )
        second = run_contract.ensure_run_contract(conn, info.project_id)
        new_id = evidence_api.add_evidence(
            conn,
            info.project_id,
            "current_probe",
            "当前运行证据",
            run_signature=second.signature,
            run_id=second.run_id,
            scope="current",
        )
        scope, params = run_contract.current_scope(conn, info.project_id, "e")
        ids = {
            int(row["id"]) for row in conn.execute(
                f"SELECT e.id FROM evidence e WHERE e.project_id=? AND {scope}",
                (info.project_id, *params),
            )
        }
        assert source_id in ids
        assert new_id in ids
        assert old_id not in ids
    finally:
        conn.close()


def test_legacy_crosscheck_evidence_cannot_enter_current_source_scope(tmp_path):
    """迁移前无签名的校核 Evidence 必须保持历史，不得伪装成来源证据。"""
    from jiadun.core.evidence import evidence as evidence_api

    info, conn = _project(tmp_path)
    try:
        active = run_contract.ensure_run_contract(conn, info.project_id)
        legacy_id = evidence_api.add_evidence(
            conn, info.project_id, "cross_check", "迁移前旧校核证据",
            scope="source",
        )
        source_id = evidence_api.add_evidence(
            conn, info.project_id, "source_probe", "原始来源证据"
        )
        scope, params = run_contract.current_scope(conn, info.project_id, "e")
        ids = {
            int(row["id"]) for row in conn.execute(
                f"SELECT e.id FROM evidence e WHERE e.project_id=? AND {scope}",
                (info.project_id, *params),
            )
        }
        assert source_id in ids
        assert legacy_id not in ids
        conn.execute(
            "UPDATE evidence SET scope='historical', historical_reason=? WHERE id=?",
            ("历史结果——当前数据或运行契约已经变化，不参与当前结论", legacy_id),
        )
        assert conn.execute(
            "SELECT scope, historical_reason FROM evidence WHERE id=?", (legacy_id,)
        ).fetchone()["scope"] == "historical"
        assert active.run_id
    finally:
        conn.close()


def test_current_anomaly_scope_keeps_legacy_stale_out_but_allows_unbound_review(tmp_path):
    """异常兼容入口允许新 NULL/NULL 待复核行，但排除迁移标记的旧结果。"""
    info, conn = _project(tmp_path)
    try:
        active = run_contract.ensure_run_contract(conn, info.project_id)
        with conn:
            fresh = conn.execute(
                """INSERT INTO anomalies(
                       project_id, rule_id, severity, subject_type, subject_id,
                       message, status, created_at, run_signature, run_id)
                   VALUES (?, 'manual_probe', 'medium', 'project', ?, '新待复核', 'open', '2026', NULL, NULL)""",
                (info.project_id, info.project_id),
            ).lastrowid
            stale = conn.execute(
                """INSERT INTO anomalies(
                       project_id, rule_id, severity, subject_type, subject_id,
                       message, status, created_at, run_signature, run_id)
                   VALUES (?, 'legacy_probe', 'medium', 'project', ?, '旧历史', 'open', '2025', ?, NULL)""",
                (info.project_id, info.project_id, run_contract.LEGACY_STALE_SIGNATURE),
            ).lastrowid
            null_history = conn.execute(
                """INSERT INTO anomalies(
                       project_id, rule_id, severity, subject_type, subject_id,
                       message, status, lifecycle_status, created_at, run_signature, run_id)
                   VALUES (?, 'null_history_probe', 'medium', 'project', ?, '无身份旧历史',
                           'stale', 'historical', '2024', NULL, NULL)""",
                (info.project_id, info.project_id),
            ).lastrowid
        scope, params = run_contract.current_scope(conn, info.project_id, "a")
        ids = {
            int(row["id"]) for row in conn.execute(
                f"SELECT a.id FROM anomalies a WHERE a.project_id=? AND {scope}",
                (info.project_id, *params),
            )
        }
        assert fresh in ids
        assert stale not in ids
        assert null_history not in ids
        assert active.run_id
    finally:
        conn.close()


def test_current_scope_without_active_contract_excludes_null_historical_rows(tmp_path):
    """尚未形成运行契约时，NULL/NULL 历史或失效结果也不得混入当前面。"""
    info, conn = _project(tmp_path)
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        period_two = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 2, '第2期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        fresh_anomaly = conn.execute(
            """INSERT INTO anomalies(
                   project_id, rule_id, severity, subject_type, subject_id,
                   message, status, lifecycle_status, created_at)
               VALUES (?, 'fresh', 'medium', 'project', ?, '待复核', 'open', 'new', '2026')
               RETURNING id""",
            (info.project_id, info.project_id),
        ).fetchone()[0]
        historical_anomaly = conn.execute(
            """INSERT INTO anomalies(
                   project_id, rule_id, severity, subject_type, subject_id,
                   message, status, lifecycle_status, created_at)
               VALUES (?, 'old', 'medium', 'project', ?, '旧结果', 'stale', 'historical', '2025')
               RETURNING id""",
            (info.project_id, info.project_id),
        ).fetchone()[0]
        fresh_crosscheck = conn.execute(
            """INSERT INTO crosscheck_results(
                   project_id, period_id, verification_level, status,
                   checked_at, run_signature, run_id)
               VALUES (?, ?, 'insufficient', 'pending', '2026', NULL, NULL)
               RETURNING id""",
            (info.project_id, period_id),
        ).fetchone()[0]
        invalid_crosscheck = conn.execute(
            """INSERT INTO crosscheck_results(
                   project_id, period_id, verification_level, status,
                   checked_at, run_signature, run_id)
               VALUES (?, ?, 'sufficient', 'invalidated', '2025', NULL, NULL)
               RETURNING id""",
            (info.project_id, period_two),
        ).fetchone()[0]

        fresh_total = conn.execute(
            """INSERT INTO period_totals(
                   project_id, period_id, item_key, amount_sum,
                   cross_check_status, run_signature, run_id)
               VALUES (?, ?, 'fresh', '1.00', 'pending', NULL, NULL)
               RETURNING id""",
            (info.project_id, period_id),
        ).fetchone()[0]
        invalid_total = conn.execute(
            """INSERT INTO period_totals(
                   project_id, period_id, item_key, amount_sum,
                   cross_check_status, run_signature, run_id)
               VALUES (?, ?, 'invalid', '2.00', 'invalidated', NULL, NULL)
               RETURNING id""",
            (info.project_id, period_two),
        ).fetchone()[0]
        from jiadun.core.evidence import evidence as evidence_api

        source_evidence = evidence_api.add_evidence(
            conn,
            info.project_id,
            "source_probe",
            "未形成运行的原始来源",
            sources=[{"period_id": period_id}],
        )
        legacy_crosscheck_evidence = evidence_api.add_evidence(
            conn,
            info.project_id,
            "cross_check",
            "未形成运行的旧校核证据",
            sources=[{"period_id": period_id}],
        )
        fresh_match = conn.execute(
            """INSERT INTO matches(
                   project_id, group_key, item_ids_json, level, method,
                   score, status, run_signature, run_id)
               VALUES (?, 'fresh', '[]', 'probable', 'test', 0.5, 'pending', NULL, NULL)
               RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        stale_match = conn.execute(
            """INSERT INTO matches(
                   project_id, group_key, item_ids_json, level, method,
                   score, status, run_signature, run_id)
               VALUES (?, 'old', '[]', 'confirmed', 'test', 1.0, 'historical', NULL, NULL)
               RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]

        def ids(alias, table):
            scope, params = run_contract.current_scope(conn, info.project_id, alias)
            return {
                int(row["id"])
                for row in conn.execute(
                    f"SELECT {alias}.id FROM {table} {alias} "
                    f"WHERE {alias}.project_id=? AND {scope}",
                    (info.project_id, *params),
                )
            }

        assert fresh_anomaly in ids("a", "anomalies")
        assert historical_anomaly not in ids("a", "anomalies")
        assert fresh_crosscheck in ids("cr", "crosscheck_results")
        assert invalid_crosscheck not in ids("cr", "crosscheck_results")
        assert fresh_total in ids("pt", "period_totals")
        assert invalid_total not in ids("pt", "period_totals")
        assert fresh_match in ids("m", "matches")
        assert stale_match not in ids("m", "matches")
        evidence_scope, evidence_params = run_contract.current_scope(
            conn, info.project_id, "e"
        )
        evidence_ids = {
            int(row["id"])
            for row in conn.execute(
                f"SELECT e.id FROM evidence e WHERE e.project_id=? AND {evidence_scope}",
                (info.project_id, *evidence_params),
            )
        }
        assert source_evidence in evidence_ids
        assert legacy_crosscheck_evidence not in evidence_ids
        assert run_contract.get_current_contract(conn, info.project_id) is None
    finally:
        conn.close()


def test_export_direction_evidence_uses_current_run_id(tmp_path):
    """管理层导出不得按重复签名把历史方向 Evidence 带回当前结果。"""
    from jiadun.core.evidence import evidence as evidence_api
    from jiadun.core.export.excel_export import _current_direction_evidence_ids

    info, conn = _project(tmp_path)
    try:
        first = run_contract.ensure_run_contract(conn, info.project_id)
        first_period = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'upward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        old_evidence = evidence_api.add_evidence(
            conn, info.project_id, "crosscheck", "历史方向证据",
            run_signature=first.signature, run_id=first.run_id, scope="current",
        )
        with conn:
            conn.execute(
                """INSERT INTO crosscheck_results(
                       project_id, period_id, verification_level, status,
                       evidence_id, checked_at, run_signature, run_id
                   ) VALUES (?, ?, 'sufficient', 'match', ?, '2026-09-01T00:00:00', ?, ?)""",
                (info.project_id, first_period, old_evidence, first.signature, first.run_id),
            )

        # 人工确认会改变合同输入；新期次用于构成新的当前运行。
        conn.execute(
            """INSERT INTO audit_log(
                   project_id, ts, actor, action, target,
                   before_json, after_json, reason
               ) VALUES (?, '2026-09-01T00:01:00', 'user', 'confirm',
                         'period:1', '{}', '{\"confirmed\":true}', '补充确认')""",
            (info.project_id,),
        )
        second = run_contract.ensure_run_contract(conn, info.project_id)
        current_evidence = evidence_api.add_evidence(
            conn, info.project_id, "crosscheck", "当前方向证据",
            run_signature=second.signature, run_id=second.run_id, scope="current",
        )
        second_period = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 2, '第2期', 'upward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        with conn:
            conn.execute(
                """INSERT INTO crosscheck_results(
                       project_id, period_id, verification_level, status,
                       evidence_id, checked_at, run_signature, run_id
                   ) VALUES (?, ?, 'sufficient', 'match', ?, '2026-09-01T00:02:00', ?, ?)""",
                (info.project_id, second_period, current_evidence, second.signature, second.run_id),
            )

        # 新期次会再次切换合同；绑定到当前合同的结果必须重新生成，旧结果
        # 即使签名相同也不能凭时间顺序进入当前摘要。
        third = run_contract.ensure_run_contract(conn, info.project_id)
        assert third.signature != second.signature
        assert _current_direction_evidence_ids(conn, info.project_id)["upward"] is None
    finally:
        conn.close()


def test_evidence_invalidation_event_preserves_before_snapshot(tmp_path):
    """Evidence 失效时追加事件，旧摘要/步骤在事件中可还原。"""
    from jiadun.core.evidence import evidence as evidence_api

    info, conn = _project(tmp_path)
    try:
        active = run_contract.ensure_run_contract(conn, info.project_id)
        evidence_id = evidence_api.add_evidence(
            conn,
            info.project_id,
            "event_probe",
            "待失效证据",
            steps=[{"step": "A", "result": "100"}],
            run_signature=active.signature,
            run_id=active.run_id,
            scope="current",
        )
        event_id = evidence_api.record_event(
            conn,
            info.project_id,
            evidence_id,
            "invalidated",
            "输入范围发生变化",
            run_id=active.run_id,
            run_signature=active.signature,
            after_scope="historical",
        )
        event = conn.execute(
            """SELECT evidence_id, before_scope, before_summary,
                      before_steps_json, after_scope, reason
               FROM evidence_events WHERE id=?""",
            (event_id,),
        ).fetchone()
        assert tuple(event) == (
            evidence_id,
            "current",
            "待失效证据",
            '[{"step": "A", "result": "100"}]',
            "historical",
            "输入范围发生变化",
        )
    finally:
        conn.close()


def test_run_contract_binds_decimal_rules_and_human_snapshot(tmp_path):
    """人工确认快照变化必须使当前合同退出并形成新合同。"""
    info, conn = _project(tmp_path)
    try:
        first = run_contract.ensure_run_contract(conn, info.project_id)
        with conn:
            conn.execute(
                """INSERT INTO audit_log(
                       project_id, ts, actor, action, target,
                       before_json, after_json, reason
                   ) VALUES (?, '2026-09-01T00:00:00', 'user', 'confirm',
                             'sheet:1', '{}', '{\"status\":\"confirmed\"}', '人工确认')""",
                (info.project_id,),
            )
        second = run_contract.ensure_run_contract(conn, info.project_id)
        assert second.signature != first.signature
        assert second.components["human_confirmation_snapshot"]
        assert second.components["decimal_rounding"] == "ROUND_HALF_UP"
        assert second.components["matching_rule_config"]["composite_key_version"] == "composite-key-v1"
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


def test_register_export_rejects_non_current_signature_without_staling_current(tmp_path):
    """导出登记不得接受历史/伪造签名，也不得先使合法成果失效。"""
    info, conn = _project(tmp_path)
    try:
        active = run_contract.ensure_run_contract(conn, info.project_id)
        export_path = tmp_path / "审核底稿.xlsx"
        export_path.write_bytes(b"current export")
        export_id = run_contract.register_export(
            conn,
            info.project_id,
            "excel_workbook",
            export_path,
            run_signature=active.signature,
        )
        with pytest.raises(ValueError, match="必须等于当前活动合同"):
            run_contract.register_export(
                conn,
                info.project_id,
                "excel_workbook",
                export_path,
                run_signature="old-run-signature",
            )
        row = conn.execute(
            "SELECT id, status, run_signature, run_id FROM export_runs WHERE id=?",
            (export_id,),
        ).fetchone()
        assert tuple(row) == (export_id, "current", active.signature, active.run_id)
        assert conn.execute(
            "SELECT COUNT(*) FROM export_runs WHERE project_id=?", (info.project_id,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_register_export_rechecks_source_gate_inside_transaction(tmp_path, monkeypatch):
    """前置门控后副本发生变化时，事务内复核不得写入 current 登记。"""
    info, conn = _project(tmp_path)
    stored = tmp_path / "source.xlsx"
    stored.write_bytes(b"source before export")
    digest = run_contract.sha256_file(stored)
    try:
        conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?, ?, ?, 'source.xlsx', ?, ?, 'xlsx', '2026')""",
            (info.project_id, str(stored), str(stored), digest, stored.stat().st_size),
        )
        active = run_contract.ensure_run_contract(conn, info.project_id)
        export_path = tmp_path / "审核底稿.xlsx"
        export_path.write_bytes(b"export")
        real_gate = run_contract.require_current_results_available

        def mutate_after_gate(conn_, project_id_, *, operation="当前操作"):
            result = real_gate(conn_, project_id_, operation=operation)
            stored.write_bytes(b"source changed after gate")
            return result

        monkeypatch.setattr(
            run_contract, "require_current_results_available", mutate_after_gate
        )
        with pytest.raises(run_contract.CurrentResultsUnavailableError, match="存储副本"):
            run_contract.register_export(
                conn,
                info.project_id,
                "excel_workbook",
                export_path,
                run_signature=active.signature,
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM export_runs WHERE project_id=?",
            (info.project_id,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_fail_closed_state_persists_across_connections_and_clears_safely(tmp_path):
    """运行级不可用边界必须落到项目侧车，并可由重开连接读取。"""
    info, conn = _project(tmp_path)
    state_path = None
    try:
        active = run_contract.ensure_run_contract(conn, info.project_id)
        state = run_contract.set_fail_closed_state(
            conn,
            info.project_id,
            reason="synthetic database commit failure",
            run_signature=active.signature,
        )
        state_path = run_contract.fail_closed_state_path(conn, info.project_id)

        assert state["status"] == run_contract.FAIL_CLOSED_STATUS
        assert state["persisted"] is True
        assert state["persistence"] == "sidecar"
        assert state_path.name == f".jiadun-run-state-{info.project_id}.json"
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
        run_id = coverage.record_detection_run(
            reopened,
            info.project_id,
            coverage.coverage_from_values(["clear-proof"], ["clear-proof"]),
            run_signature=active.signature,
            run_kind=coverage.AGGREGATE_VALIDATION,
        )
        run_contract.clear_fail_closed_state(
            reopened,
            info.project_id,
            run_signature=active.signature,
            coverage_run_id=run_id,
            coverage_run_kind=coverage.AGGREGATE_VALIDATION,
        )
        assert run_contract.current_results_available(reopened, info.project_id)["available"]
        assert not state_path.exists()
    finally:
        reopened.close()


def test_legacy_fail_closed_sidecar_is_read_only_and_never_deleted(tmp_path, monkeypatch):
    """价盾双读旧侧车，但完整恢复也不得自动删除 legacy 文件。"""
    info, conn = _project(tmp_path)
    try:
        state = run_contract.set_fail_closed_state(conn, info.project_id, reason="旧侧车样本")
        current_path = run_contract.fail_closed_state_path(conn, info.project_id)
        legacy_path = run_contract.legacy_fail_closed_state_paths(conn, info.project_id)[0]
        legacy_path.write_bytes(current_path.read_bytes())
        current_path.unlink()

        loaded = run_contract.get_fail_closed_state(conn, info.project_id)
        assert loaded is not None
        assert loaded["_legacy_sidecar"] is True
        assert loaded["_sidecar_path"] == str(legacy_path)

        # 证明函数在这里仅用于隔离“legacy 不可删除”分支；实际业务仍必须
        # 通过真实 coverage proof 才能恢复。
        monkeypatch.setattr(run_contract, "_coverage_proof_is_valid", lambda *_a, **_k: True)
        with pytest.raises(RuntimeError, match="legacy"):
            run_contract.clear_fail_closed_state(
                conn,
                info.project_id,
                run_signature="legacy-signature",
                coverage_run_id=1,
                coverage_run_kind=coverage.AGGREGATE_VALIDATION,
            )
        assert legacy_path.is_file()
        assert state["status"] == run_contract.FAIL_CLOSED_STATUS
    finally:
        conn.close()


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
        conn.close()


def test_compatibility_import_uses_the_implementation_module_object():
    """兼容入口与实际模块必须共享属性，故障注入不能打到分叉引用。"""
    import jiadun.core.run_contract as compatibility

    assert compatibility is run_contract


def test_adopt_unsigned_records_requires_current_signature_and_both_identity_columns(tmp_path):
    """兼容绑定不得接受伪造签名或半成品运行身份。"""
    info, conn = _project(tmp_path)
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        active = run_contract.ensure_run_contract(conn, info.project_id)
        with conn:
            conn.execute(
                """INSERT INTO period_totals(
                       project_id, period_id, item_key, qty_sum, amount_sum,
                       cross_check_status, verification_level)
                   VALUES (?, ?, 'code:A', '1', '2', 'pending', 'insufficient')""",
                (info.project_id, period_id),
            )
            conn.execute(
                """INSERT INTO crosscheck_results(
                       project_id, period_id, verification_level, status, checked_at)
                   VALUES (?, ?, 'insufficient', 'pending', '2026')""",
                (info.project_id, period_id),
            )
            conn.execute(
                """INSERT INTO matches(
                       project_id, group_key, item_ids_json, level, method)
                   VALUES (?, 'code:A', '[]', 'pending_data', 'none')""",
                (info.project_id,),
            )
            conn.execute(
                """INSERT INTO anomalies(
                       project_id, rule_id, severity, subject_type, subject_id,
                       message, status, created_at)
                   VALUES (?, 'compatibility_probe', 'info', 'project', ?, '待复核', 'open', '2026')""",
                (info.project_id, info.project_id),
            )
        with pytest.raises(ValueError, match="不是当前活动 Run Contract"):
            run_contract.adopt_unsigned_records(conn, info.project_id, "bogus-signature")
        assert conn.execute(
            "SELECT COUNT(*) FROM period_totals WHERE run_signature IS NULL AND run_id IS NULL"
        ).fetchone()[0] == 1

        assert run_contract.adopt_unsigned_records(conn, info.project_id, active.signature) == 4
        for table in ("period_totals", "crosscheck_results", "matches", "anomalies"):
            row = conn.execute(
                f"SELECT run_signature, run_id FROM {table} WHERE project_id=? LIMIT 1",
                (info.project_id,),
            ).fetchone()
            assert tuple(row) == (active.signature, active.run_id)
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
