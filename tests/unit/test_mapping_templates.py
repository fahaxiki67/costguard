"""P1-03 字段映射模板：来源、作用域、版本和只读推荐。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from jiadun.core.db import migrations
from jiadun.core.mapping.templates import (
    recommend_mapping_templates,
    save_mapping_template,
)


@pytest.fixture()
def db(tmp_path: Path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        project_id = conn.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at)"
            " VALUES (?,?,?,?)",
            ("模板测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
        stored = tmp_path / "input.xlsx"
        stored.write_bytes(b"synthetic mapping-template source")
        digest = hashlib.sha256(stored.read_bytes()).hexdigest()
        file_id = conn.execute(
            """INSERT INTO source_files(
                   project_id, original_path, stored_path, original_name,
                   sha256, size_bytes, file_type, imported_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (project_id, str(tmp_path / "input.xlsx"), str(stored),
             "input.xlsx", digest, stored.stat().st_size, "xlsx", "2026"),
        ).lastrowid
        batch_id = conn.execute(
            "INSERT INTO parse_batches(file_id, parser, parsed_at, status) VALUES (?,?,?,?)",
            (file_id, "test", "2026", "ok"),
        ).lastrowid
        sheet_id = conn.execute(
            """INSERT INTO raw_sheets(
                   batch_id, sheet_index, sheet_name, n_rows, n_cols,
                   merged_ranges_json, hidden_rows_json, hidden_cols_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (batch_id, 0, "第1期", 5, 7, "[]", "[]", "[]"),
        ).lastrowid
        headers = ["清单编码", "清单名称", "项目特征", "单位", "工程量", "综合单价", "合价"]
        for col, value in enumerate(headers, 1):
            conn.execute(
                "INSERT INTO raw_cells(sheet_id,row,col,raw_value) VALUES (?,?,?,?)",
                (sheet_id, 1, col, value),
            )
        conn.execute(
            """INSERT INTO table_headers(
                   sheet_id, header_row_lo, header_row_hi, col_map_json,
                   confidence, needs_review, data_row_start, data_row_end,
                   data_range_status, data_range_method, data_range_evidence_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (sheet_id, 1, 1, json.dumps({
                "code": 1, "name": 2, "feature": 3, "unit": 4,
                "quantity": 5, "unit_price": 6, "amount": 7,
            }), 1.0, 0, 2, 5, "confirmed", "test", "{}"),
        )
    yield conn, int(project_id), int(sheet_id), int(file_id)
    conn.close()


def _map():
    return {
        "code": 1,
        "name": 2,
        "feature": 3,
        "unit": 4,
        "quantity": 5,
        "unit_price": 6,
        "amount": 7,
    }


def test_save_template_preserves_source_creator_and_audit(db):
    conn, project_id, sheet_id, file_id = db
    template = save_mapping_template(
        conn,
        project_id,
        sheet_id,
        scope="project",
        template_name="标准清单映射",
        col_map=_map(),
        header_range=(1, 1),
        data_range=(2, 5),
        created_by="张三",
        reason="已核对项目标准结算表表头与数据区",
        note="项目模板，不跨项目直接套用",
    )
    assert template.template_id > 0
    assert template.project_id == project_id
    assert template.source_file_id == file_id
    assert template.source_sheet_id == sheet_id
    assert template.created_by == "张三"
    assert template.version == 1
    assert template.col_map == _map()
    assert template.source_reference["file_sha256"] == hashlib.sha256(
        b"synthetic mapping-template source"
    ).hexdigest()
    assert template.source_reference["header_range"] == [1, 1]
    assert template.evidence_id is not None
    evidence = conn.execute(
        "SELECT kind, scope, summary, sources_json FROM evidence WHERE id=?",
        (template.evidence_id,),
    ).fetchone()
    assert tuple(evidence[:3]) == ("mapping_template", "human", "字段映射模板「标准清单映射」v1：仅作为人工复核候选")
    assert json.loads(evidence["sources_json"])[0]["sheet_id"] == sheet_id
    audit = conn.execute(
        "SELECT actor, action, target, reason, after_json FROM audit_log "
        "WHERE action='save_mapping_template' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert tuple(audit[:4]) == ("张三", "save_mapping_template", f"mapping_template:{template.template_id}",
                                "已核对项目标准结算表表头与数据区")
    assert json.loads(audit["after_json"])["evidence_id"] == template.evidence_id


def test_save_template_rebinds_operation_to_final_run(db):
    conn, project_id, sheet_id, _ = db
    from jiadun.core.contracts import run_contract

    first = run_contract.ensure_run_contract(conn, project_id)
    template = save_mapping_template(
        conn,
        project_id,
        sheet_id,
        scope="project",
        template_name="绑定测试模板",
        col_map=_map(),
        header_range=(1, 1),
        created_by="张三",
        reason="确认模板输入并绑定最终运行",
    )
    current = run_contract.get_current_contract(conn, project_id)
    assert current is not None and current.run_id != first.run_id
    assert run_contract.ensure_run_contract(conn, project_id).run_id == current.run_id
    evidence = conn.execute(
        "SELECT run_id, run_signature FROM evidence WHERE id=?", (template.evidence_id,)
    ).fetchone()
    audit = conn.execute(
        """SELECT run_id, run_signature FROM audit_log
           WHERE action='save_mapping_template' ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    assert tuple(evidence) == (current.run_id, current.signature)
    assert tuple(audit) == (current.run_id, current.signature)


def test_versions_are_append_only_and_scope_isolated(db):
    conn, project_id, sheet_id, _ = db
    kwargs = dict(
        project_id=project_id,
        sheet_id=sheet_id,
        scope="project",
        template_name="同名模板",
        col_map=_map(),
        header_range=(1, 1),
        created_by="甲",
        reason="第一次人工核对",
    )
    first = save_mapping_template(conn, **kwargs)
    second = save_mapping_template(conn, **{**kwargs, "created_by": "乙", "reason": "第二次复核"})
    global_template = save_mapping_template(
        conn, **{**kwargs, "scope": "global", "template_name": "全局模板", "created_by": "丙",
                 "reason": "确认可作为跨项目候选"},
    )
    assert (first.version, second.version, global_template.version) == (1, 2, 1)
    with pytest.raises(sqlite3.IntegrityError, match="mapping template immutable"):
        conn.execute("UPDATE mapping_templates SET note='篡改' WHERE id=?", (first.template_id,))
    with pytest.raises(sqlite3.IntegrityError, match="mapping template immutable"):
        conn.execute("DELETE FROM mapping_templates WHERE id=?", (first.template_id,))


def test_recommendation_is_read_only_manual_only_and_scored(db):
    conn, project_id, sheet_id, _ = db
    template = save_mapping_template(
        conn,
        project_id,
        sheet_id,
        scope="project",
        template_name="可推荐模板",
        col_map=_map(),
        header_range=(1, 1),
        created_by="用户",
        reason="确认该表头可复用为候选",
    )
    before_map = conn.execute(
        "SELECT col_map_json FROM table_headers WHERE sheet_id=?", (sheet_id,)
    ).fetchone()[0]
    recommendations = recommend_mapping_templates(conn, project_id, sheet_id)
    assert recommendations
    candidate = recommendations[0]
    assert candidate.template.template_id == template.template_id
    assert candidate.exact_header_match is True
    assert candidate.score == Decimal("1.0000")
    payload = candidate.as_dict()
    assert payload["requires_manual_confirmation"] is True
    assert payload["source_reference"]["file_id"]
    assert conn.execute(
        "SELECT col_map_json FROM table_headers WHERE sheet_id=?", (sheet_id,)
    ).fetchone()[0] == before_map
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='recommend_mapping_template'").fetchone()[0] == 0


def test_missing_reason_or_bad_mapping_is_rejected_without_template(db):
    conn, project_id, sheet_id, _ = db
    with pytest.raises(Exception, match="原因"):
        save_mapping_template(
            conn, project_id, sheet_id, scope="sheet", template_name="无原因",
            col_map=_map(), header_range=(1, 1), created_by="用户", reason="",
        )
    with pytest.raises(ValueError, match="金额口径"):
        save_mapping_template(
            conn, project_id, sheet_id, scope="sheet", template_name="坏映射",
            col_map={"name": 2}, header_range=(1, 1), created_by="用户", reason="核对",
        )
    assert conn.execute("SELECT COUNT(*) FROM mapping_templates").fetchone()[0] == 0


def test_recommendation_rejects_sheet_from_another_project(db):
    conn, project_id, sheet_id, _ = db
    with conn:
        other_project = conn.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at) VALUES (?,?,?,?)",
            ("其他项目", migrations.LATEST_SCHEMA_VERSION, "/other", "2026"),
        ).lastrowid
    with pytest.raises(ValueError, match="不属于指定项目"):
        recommend_mapping_templates(conn, int(other_project), sheet_id)
