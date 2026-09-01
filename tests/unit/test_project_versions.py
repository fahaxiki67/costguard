"""P2-03 项目版本链与不可覆盖清单快照测试。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from jiadun.core.contracts import run_contract
from jiadun.core.models import project as project_model
from jiadun.core.versions import (
    compare_project_versions,
    create_project_version,
    list_project_versions,
)


def _project(tmp_path: Path):
    info = project_model.create_project("项目版本测试", tmp_path / "ws")
    info, conn = project_model.open_project(Path(info.workspace_path))
    return info, conn


def test_project_version_chain_is_append_only_and_run_bound(tmp_path: Path):
    info, conn = _project(tmp_path)
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO line_items(
                   period_id, code, name, feature, unit, quantity, unit_price, amount,
                   flags_json)
               VALUES (?, 'A-1', '混凝土', 'C30', 'm3', '10', '100', '1000', ?)""",
            (period_id, json.dumps({"row": 8})),
        )
        first_contract = run_contract.ensure_run_contract(conn, info.project_id)
        first = create_project_version(
            conn,
            info.project_id,
            "initial_submission",
            "第一次送审",
            created_by="复核人",
            reason="冻结第一次送审清单",
        )
        current = run_contract.get_current_contract(conn, info.project_id)
        assert current is not None and current.run_id != first_contract.run_id
        assert first.is_current_run
        assert first.item_count == 1
        assert first.evidence_id is not None and first.audit_id is not None
        assert current.components["project_version"]["version"]["version_no"] == 1
        assert run_contract.ensure_run_contract(conn, info.project_id).run_id == current.run_id
        bound = conn.execute(
            """SELECT v.run_id, v.run_signature, e.run_id AS evidence_run_id,
                      a.run_id AS audit_run_id
               FROM project_versions v
               JOIN evidence e ON e.id=v.evidence_id
               JOIN audit_log a ON a.id=v.audit_id
               WHERE v.id=?""",
            (first.version_id,),
        ).fetchone()
        assert tuple(bound) == (
            current.run_id, current.signature, current.run_id, current.run_id
        )
        with pytest.raises(sqlite3.IntegrityError, match="project version immutable"):
            conn.execute("UPDATE project_versions SET title='篡改' WHERE id=?", (first.version_id,))
        with pytest.raises(sqlite3.IntegrityError, match="project version item immutable"):
            conn.execute(
                "UPDATE project_version_items SET name='篡改' WHERE version_id=?",
                (first.version_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="project version immutable"):
            conn.execute("DELETE FROM project_versions WHERE id=?", (first.version_id,))
    finally:
        conn.close()


def test_version_item_snapshot_guard_rejects_fake_line_item_values(tmp_path: Path):
    """真实 line_item_id 不能被挂接到伪造的版本字段。"""
    info, conn = _project(tmp_path)
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        line_item_id = conn.execute(
            """INSERT INTO line_items(
                   period_id, code, name, unit, quantity, unit_price, amount, flags_json)
               VALUES (?, 'A-1', '真实项目', 'm3', '1', '10', '10', '{}') RETURNING id""",
            (period_id,),
        ).fetchone()[0]
        version = create_project_version(
            conn, info.project_id, "initial_submission", "第一次送审",
            created_by="测试人", reason="验证版本来源快照闸门",
        )
        with pytest.raises(sqlite3.IntegrityError, match="snapshot source mismatch"):
            conn.execute(
                """INSERT INTO project_version_items(
                       version_id, project_id, identity_key, occurrence,
                       period_id, period_no, direction, line_item_id,
                       code, name, unit, quantity, unit_price, amount, created_at)
                   VALUES (?, ?, 'code:A-1', 2, ?, 1, 'downward', ?,
                           'A-1', '伪造项目', 'm3', '1', '999', '999', '2026')""",
                (version.version_id, info.project_id, period_id, line_item_id),
            )
    finally:
        conn.close()


def test_version_comparison_keeps_pending_out_of_confirmed_net(tmp_path: Path):
    info, conn = _project(tmp_path)
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        conn.executemany(
            """INSERT INTO line_items(
                   period_id, code, name, feature, unit, quantity, unit_price, amount,
                   flags_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (period_id, "A-1", "混凝土", "C30", "m3", "10", "100", "1000", "{}"),
                (period_id, "D-1", "钢筋", "HRB400", "t", "2", "3000", "6000", "{}"),
            ],
        )
        first = create_project_version(
            conn, info.project_id, "initial_submission", "第一次送审",
            created_by="甲", reason="建立版本基线",
        )
        conn.execute(
            "UPDATE line_items SET quantity='12', amount='1200' WHERE code='A-1'"
        )
        conn.execute("DELETE FROM line_items WHERE code='D-1'")
        conn.execute(
            """INSERT INTO line_items(
                   period_id, code, name, feature, unit, quantity, unit_price, amount,
                   flags_json)
               VALUES (?, 'N-1', '新增项', '', '项', '1', '500', '500', ?)""",
            (period_id, json.dumps({"needs_review": True})),
        )
        second = create_project_version(
            conn, info.project_id, "supplement", "补充资料",
            created_by="乙", reason="补充并调整送审清单",
        )
        comparison = compare_project_versions(
            conn, info.project_id, first.version_id, second.version_id
        )
        assert comparison.status == "conditional"
        by_category = {item.category: item for item in comparison.items}
        assert by_category["quantity_changed"].status == "confirmed"
        assert by_category["quantity_changed"].amount_impact == 200
        assert by_category["deleted"].amount_impact == -6000
        assert by_category["pending"].confirmed_amount_impact is None
        assert comparison.confirmed_net_amount_impact == -5800
        assert comparison.category_summary["pending"]["amount_available"] is False
        assert comparison.evidence_ids
        versions = list_project_versions(conn, info.project_id)
        assert [item.version_no for item in versions] == [1, 2]
        assert all(item.is_current_run is (item.version_no == 2) for item in versions)
    finally:
        conn.close()


def test_version_comparison_marks_unparseable_amount_as_pending(tmp_path: Path):
    """金额损坏/缺失时不能用无金额差异的 confirmed 结果冒充可用比较。"""
    info, conn = _project(tmp_path)
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO line_items(
                   period_id, code, name, feature, unit, quantity, unit_price, amount,
                   flags_json)
               VALUES (?, 'A-1', '混凝土', 'C30', 'm3', '10', '100', '1000', '{}')""",
            (period_id,),
        )
        first = create_project_version(
            conn, info.project_id, "initial_submission", "第一次送审",
            created_by="甲", reason="建立金额解析基线",
        )
        conn.execute(
            "UPDATE line_items SET amount='BAD', flags_json=? WHERE code='A-1'",
            (json.dumps({"amount_unparsed": "BAD"}),),
        )
        second = create_project_version(
            conn, info.project_id, "supplement", "补充资料",
            created_by="乙", reason="验证坏金额不越过确认净额",
        )
        comparison = compare_project_versions(
            conn, info.project_id, first.version_id, second.version_id
        )
        assert comparison.status == "conditional"
        assert comparison.items[0].status == "pending"
        assert comparison.items[0].amount_impact is None
        assert comparison.confirmed_net_amount_impact is None
    finally:
        conn.close()


def test_version_comparison_does_not_merge_duplicate_or_cross_period_items(tmp_path: Path):
    """重复键与跨期同码项目不得吸收进 confirmed 净影响。"""
    info, conn = _project(tmp_path)
    try:
        first_period = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        second_period = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 2, '第2期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        conn.executemany(
            """INSERT INTO line_items(
                   period_id, code, name, feature, unit, quantity, unit_price, amount,
                   flags_json)
               VALUES (?, 'DUP', '重复项', 'C30', 'm3', '1', '100', ?, '{}')""",
            [(first_period, "100"), (first_period, "200")],
        )
        first = create_project_version(
            conn, info.project_id, "initial_submission", "第一次送审",
            created_by="甲", reason="建立重复键基线",
        )
        conn.execute("DELETE FROM line_items WHERE period_id=?", (first_period,))
        conn.execute(
            """INSERT INTO line_items(
                   period_id, code, name, feature, unit, quantity, unit_price, amount,
                   flags_json)
               VALUES (?, 'DUP', '重复项', 'C30', 'm3', '1', '100', '500', '{}')""",
            (second_period,),
        )
        second = create_project_version(
            conn, info.project_id, "supplement", "补充资料",
            created_by="乙", reason="验证跨期与重复键边界",
        )
        comparison = compare_project_versions(
            conn, info.project_id, first.version_id, second.version_id
        )
        assert comparison.status == "conditional"
        assert comparison.confirmed_net_amount_impact is None
        assert all(item.status in {"pending", "incomparable"} for item in comparison.items)
        assert all(item.confirmed_amount_impact is None for item in comparison.items)
    finally:
        conn.close()


def test_version_comparison_direction_change_is_incomparable(tmp_path: Path):
    """同一清单从对上改为对下时，不得计入确认净金额。"""
    info, conn = _project(tmp_path)
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO line_items(
                   period_id, code, name, feature, unit, quantity, unit_price, amount,
                   flags_json)
               VALUES (?, 'A-1', '方向变化项', 'C30', 'm3', '10', '100', '1000', '{}')""",
            (period_id,),
        )
        first = create_project_version(
            conn, info.project_id, "initial_submission", "第一次送审",
            created_by="甲", reason="建立方向基线",
        )
        conn.execute(
            "UPDATE settlement_periods SET direction='upward' WHERE id=?", (period_id,)
        )
        conn.execute("UPDATE line_items SET amount='1200' WHERE period_id=?", (period_id,))
        second = create_project_version(
            conn, info.project_id, "supplement", "补充资料",
            created_by="乙", reason="验证方向变化不可比",
        )
        comparison = compare_project_versions(
            conn, info.project_id, first.version_id, second.version_id
        )
        assert comparison.status == "conditional"
        assert len(comparison.items) == 1
        assert comparison.items[0].status == "incomparable"
        assert comparison.items[0].confirmed_amount_impact is None
        assert comparison.confirmed_net_amount_impact is None
    finally:
        conn.close()


def test_version_scope_guard_rejects_bare_source_row(tmp_path: Path):
    """只有 source_row、没有可追溯文件/Sheet/Evidence 的快照行必须拒绝。"""
    info, conn = _project(tmp_path)
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        version_id = conn.execute(
            """INSERT INTO project_versions(
                   project_id, version_no, version_kind, title, snapshot_sha256,
                   item_count, created_by, created_at, reason)
               VALUES (?, 1, 'initial_submission', '旧版本', 'sha', 1,
                       '旧脚本', '2026', '范围闸门测试') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="project version item scope incomplete"):
            conn.execute(
                """INSERT INTO project_version_items(
                       version_id, project_id, identity_key, occurrence,
                       period_id, period_no, direction, code, name, unit,
                       quantity, unit_price, amount, source_row, created_at)
                   VALUES (?, ?, 'code:X', 1, ?, 1, 'downward', 'X', '项目X',
                           'm3', '1', '100', '100', 8, '2026')""",
                (version_id, info.project_id, period_id),
            )
    finally:
        conn.close()


def test_version_summary_downgrades_snapshot_with_extra_item(tmp_path: Path):
    """版本责任记录正确但快照被追加篡改时，摘要也必须 fail-closed。"""
    from jiadun.core.reporting.summary import _version_chain

    info, conn = _project(tmp_path)
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        line_item_id = conn.execute(
            """INSERT INTO line_items(
                   period_id, code, name, unit, quantity, unit_price, amount, flags_json)
               VALUES (?, 'A-1', '项目A', 'm3', '1', '100', '100', '{}') RETURNING id""",
            (period_id,),
        ).fetchone()[0]
        version = create_project_version(
            conn, info.project_id, "initial_submission", "第一次送审",
            created_by="甲", reason="建立摘要完整性基线",
        )
        conn.execute(
            """INSERT INTO project_version_items(
                   version_id, project_id, identity_key, occurrence,
                   period_id, period_no, direction, line_item_id, code, name, unit,
                   quantity, unit_price, amount, created_at)
               VALUES (?, ?, 'code:A-1', 2, ?, 1, 'downward', ?,
                       'A-1', '项目A', 'm3', '1', '100', '100', '2026')""",
            (version.version_id, info.project_id, period_id, line_item_id),
        )
        conn.commit()
        chain = _version_chain(conn, info.project_id)
        assert chain["latest"]["chain_valid"] is False
        assert "version_item_count_mismatch" in chain["latest"]["chain_reason_codes"]
        assert chain["status"] != "closed"
    finally:
        conn.close()


def test_version_comparison_downgrades_unbound_direct_sql_versions(tmp_path: Path):
    """旧脚本直接写入未绑定版本时，不得产生可确认净金额。"""
    info, conn = _project(tmp_path)
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        line_item_id = conn.execute(
            """INSERT INTO line_items(
                   period_id, code, name, unit, quantity, unit_price, amount, flags_json)
               VALUES (?, 'X', '项目X', 'm3', '1', '100', '100', '{}') RETURNING id""",
            (period_id,),
        ).fetchone()[0]
        with conn:
            first = conn.execute(
                """INSERT INTO project_versions(
                       project_id, version_no, version_kind, title,
                       snapshot_sha256, item_count, created_by, created_at, reason)
                   VALUES (?, 1, 'initial_submission', '旧版本1', 'sha-1', 1,
                           '旧脚本', '2026', '未绑定测试') RETURNING id""",
                (info.project_id,),
            ).fetchone()[0]
            second = conn.execute(
                """INSERT INTO project_versions(
                       project_id, version_no, version_kind, title,
                       snapshot_sha256, item_count, created_by, created_at, reason)
                   VALUES (?, 2, 'supplement', '旧版本2', 'sha-2', 1,
                           '旧脚本', '2026', '未绑定测试') RETURNING id""",
                (info.project_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO project_version_items(
                       version_id, project_id, identity_key, occurrence,
                       period_id, period_no, direction, line_item_id, code, name, unit,
                       quantity, unit_price, amount, source_row, created_at)
                   VALUES (?, ?, 'code:X', 1, ?, 1, 'downward', ?, 'X', '项目X',
                           'm3', '1', '100', ?, 1, '2026')""",
                (first, info.project_id, period_id, line_item_id, "100"),
            )
            conn.execute(
                """INSERT INTO project_version_items(
                       version_id, project_id, identity_key, occurrence,
                       period_id, period_no, direction, line_item_id, code, name, unit,
                       quantity, unit_price, amount, source_row, created_at)
               VALUES (?, ?, 'code:X', 1, ?, 1, 'downward', ?, 'X', '项目X',
                           'm3', '1', '100', ?, 1, '2026')""",
                (second, info.project_id, period_id, line_item_id, "100"),
            )
        comparison = compare_project_versions(conn, info.project_id, first, second)
        assert comparison.status == "conditional"
        assert comparison.confirmed_net_amount_impact is None
        assert comparison.items[0].status == "pending"
        assert comparison.items[0].confirmed_amount_impact is None
        assert "version_run_unbound" in comparison.reason_codes
        assert "version_evidence_missing" in comparison.reason_codes
    finally:
        conn.close()


def test_version_integrity_rechecks_explicit_source_cells(tmp_path: Path):
    """无 line_item_id 的显式来源快照必须回读 Evidence 对应原始单元格。"""
    from jiadun.core.evidence import evidence as evidence_api
    from jiadun.core.versions.project import _version_integrity, get_project_version

    info, conn = _project(tmp_path)
    try:
        period_id = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        stored = tmp_path / "source.xlsx"
        stored.write_bytes(b"synthetic project-version source")
        digest = hashlib.sha256(stored.read_bytes()).hexdigest()
        file_id = conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name, sha256,
                   size_bytes, file_type, imported_at)
               VALUES (?, '/source.xlsx', ?, 'source.xlsx', ?, ?, 'xlsx', '2026')
               RETURNING id""",
            (info.project_id, str(stored), digest, stored.stat().st_size),
        ).fetchone()[0]
        batch_id = conn.execute(
            """INSERT INTO parse_batches(file_id, parser, parsed_at, status)
               VALUES (?, 'test', '2026', 'ok') RETURNING id""",
            (file_id,),
        ).fetchone()[0]
        sheet_id = conn.execute(
            """INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name, n_rows, n_cols, period_id)
               VALUES (?, 0, '第1期明细', 2, 7, ?) RETURNING id""",
            (batch_id, period_id),
        ).fetchone()[0]
        conn.executemany(
            """INSERT INTO raw_cells(sheet_id, row, col, raw_value, cached_value)
               VALUES (?, 2, ?, ?, ?)""",
            [
                (sheet_id, 1, "A-1", "A-1"),
                (sheet_id, 2, "项目A", "项目A"),
                (sheet_id, 3, "1", "1"),
                (sheet_id, 4, "100", "100"),
                (sheet_id, 5, "100", "100"),
                (sheet_id, 6, "项目A", "项目A"),
                (sheet_id, 7, "m3", "m3"),
            ],
        )
        version = create_project_version(
            conn, info.project_id, "initial_submission", "第一次送审",
            created_by="测试人", reason="显式来源快照测试",
        )
        active = run_contract.get_current_contract(conn, info.project_id)
        assert active is not None
        field_cols = {
            "code": (1, "A-1"), "name": (2, "项目A"), "feature": (6, "项目A"),
            "unit": (7, "m3"), "quantity": (3, "1"),
            "unit_price": (4, "100"), "amount": (5, "100"),
        }
        source_evidence_id = evidence_api.add_evidence(
            conn,
            info.project_id,
            "line_item_source",
            "显式来源快照测试",
            sources=[
                {
                    "field": field,
                    "file_id": file_id,
                    "sheet_id": sheet_id,
                    "row": 2,
                    "col": col,
                    "raw_value": raw,
                    "value": raw,
                }
                for field, (col, raw) in field_cols.items()
            ],
            run_signature=active.signature,
            run_id=active.run_id,
            scope="current",
        )
        conn.execute(
            """INSERT INTO project_version_items(
                   version_id, project_id, identity_key, occurrence,
                   period_id, period_no, direction, code, name, feature, unit,
                   quantity, unit_price, amount, source_file_id, source_sheet_id,
                   source_row, source_evidence_id, created_at)
               VALUES (?, ?, 'code:A-1', 1, ?, 1, 'downward', 'A-1', '项目A',
                       '项目A', 'm3', '1', '100', '999', ?, ?, 2, ?, '2026')""",
            (version.version_id, info.project_id, period_id, file_id, sheet_id, source_evidence_id),
        )
        conn.commit()
        stored = get_project_version(conn, info.project_id, version.version_id)
        assert stored is not None
        valid, reasons = _version_integrity(conn, stored)
        assert not valid
        assert "version_item_source_evidence_value_mismatch" in reasons

        # 即使 Evidence 被直接改成与伪造快照一致，raw_cells 仍是不可变证据根，
        # 回读必须继续发现其原文金额不一致。
        source = json.loads(
            conn.execute(
                "SELECT sources_json FROM evidence WHERE id=?", (source_evidence_id,)
            ).fetchone()[0]
        )
        for entry in source:
            if entry["field"] == "amount":
                entry["raw_value"] = "999"
                entry["value"] = "999"
        conn.execute(
            "UPDATE evidence SET sources_json=? WHERE id=?",
            (json.dumps(source, ensure_ascii=False), source_evidence_id),
        )
        valid, reasons = _version_integrity(conn, stored)
        assert not valid
        assert "version_item_source_evidence_raw_mismatch" in reasons
    finally:
        conn.close()


def test_version_explicit_source_cannot_cross_period_even_with_or_without_line_item(
    tmp_path: Path,
):
    """版本来源 Sheet/文件必须与快照期次一致，不能跨期挂接真实定位。"""
    from jiadun.core.evidence import evidence as evidence_api

    info, conn = _project(tmp_path)
    try:
        period_one = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 1, '第1期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        period_two = conn.execute(
            """INSERT INTO settlement_periods(project_id, period_no, title, direction)
               VALUES (?, 2, '第2期', 'downward') RETURNING id""",
            (info.project_id,),
        ).fetchone()[0]
        stored_two = tmp_path / "period2.xlsx"
        stored_two.write_bytes(b"synthetic project-version period-two source")
        digest_two = hashlib.sha256(stored_two.read_bytes()).hexdigest()
        file_two = conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name, sha256,
                   size_bytes, file_type, imported_at)
               VALUES (?, '/period2.xlsx', ?, 'period2.xlsx',
                       ?, ?, 'xlsx', '2026') RETURNING id""",
            (info.project_id, str(stored_two), digest_two, stored_two.stat().st_size),
        ).fetchone()[0]
        batch_two = conn.execute(
            """INSERT INTO parse_batches(file_id, parser, parsed_at, status)
               VALUES (?, 'test', '2026', 'ok') RETURNING id""",
            (file_two,),
        ).fetchone()[0]
        sheet_two = conn.execute(
            """INSERT INTO raw_sheets(
                   batch_id, sheet_index, sheet_name, n_rows, n_cols, period_id)
               VALUES (?, 0, '第2期明细', 2, 7, ?) RETURNING id""",
            (batch_two, period_two),
        ).fetchone()[0]
        version = create_project_version(
            conn, info.project_id, "initial_submission", "第一次送审",
            created_by="测试人", reason="跨期来源闸门",
        )
        active = run_contract.get_current_contract(conn, info.project_id)
        assert active is not None
        source_evidence_id = evidence_api.add_evidence(
            conn,
            info.project_id,
            "line_item_source",
            "跨期来源测试",
            sources=[{
                "field": "amount", "file_id": file_two, "sheet_id": sheet_two,
                "row": 2, "col": 7, "raw_value": "100", "value": "100",
            }],
            run_signature=active.signature,
            run_id=active.run_id,
            scope="current",
        )
        with pytest.raises(sqlite3.IntegrityError, match="scope incomplete"):
            conn.execute(
                """INSERT INTO project_version_items(
                       version_id, project_id, identity_key, occurrence,
                       period_id, period_no, direction, code, name, unit,
                       quantity, unit_price, amount, source_file_id, source_sheet_id,
                       source_row, source_evidence_id, created_at)
                   VALUES (?, ?, 'code:X', 1, ?, 1, 'downward', 'X', '跨期项', 'm3',
                           '1', '100', '100', ?, ?, 2, ?, '2026')""",
                (
                    version.version_id, info.project_id, period_one,
                    file_two, sheet_two, source_evidence_id,
                ),
            )

        line_item_id = conn.execute(
            """INSERT INTO line_items(
                   period_id, code, name, unit, quantity, unit_price, amount, flags_json)
               VALUES (?, 'X', '本期项', 'm3', '1', '100', '100', '{}') RETURNING id""",
            (period_one,),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="scope incomplete"):
            conn.execute(
                """INSERT INTO project_version_items(
                       version_id, project_id, identity_key, occurrence,
                       period_id, period_no, direction, line_item_id,
                       code, name, unit, quantity, unit_price, amount,
                       source_file_id, source_sheet_id, source_row,
                       source_evidence_id, created_at)
                   VALUES (?, ?, 'code:X', 2, ?, 1, 'downward', ?, 'X', '本期项', 'm3',
                           '1', '100', '100', ?, ?, 2, ?, '2026')""",
                (
                    version.version_id, info.project_id, period_one, line_item_id,
                    file_two, sheet_two, source_evidence_id,
                ),
            )
    finally:
        conn.close()


def test_project_version_rejects_unknown_kind(tmp_path: Path):
    info, conn = _project(tmp_path)
    try:
        with pytest.raises(ValueError, match="不支持的项目版本类型"):
            create_project_version(
                conn, info.project_id, "unknown", "错误版本",
                created_by="用户", reason="验证输入",
            )
    finally:
        conn.close()
