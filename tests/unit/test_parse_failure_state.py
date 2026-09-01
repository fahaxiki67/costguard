"""解析失败也必须进入可追溯的统一 Sheet/批次状态。"""

import json
from pathlib import Path

import openpyxl

from jiadun.core.engine import settlement_io
from jiadun.core.models import project as project_model
from jiadun.core.parsing import excel_parser
from jiadun.core.reporting import build_project_summary


def test_failed_parser_persists_batch_state_and_source_evidence(tmp_path: Path):
    """损坏工作簿不能只返回 ImportReport.failed 后从项目状态中消失。"""
    info = project_model.create_project("解析失败状态", tmp_path / "workspace")
    info, conn = project_model.open_project(Path(info.workspace_path))
    bad_file = tmp_path / "损坏结算表.xlsx"
    bad_file.write_bytes(b"not-a-zip-workbook")
    try:
        report = settlement_io.import_settlement_file(
            conn, info.project_id, Path(info.workspace_path), bad_file
        )

        assert report.status == "failed"
        assert report.batch_id is not None
        assert report.sheets
        assert report.sheets[0].status == "parse_failed"
        assert report.sheets[0].state_code == "parse_failed"

        batch = conn.execute(
            "SELECT status, stats_json FROM parse_batches WHERE id=?",
            (report.batch_id,),
        ).fetchone()
        assert batch["status"] == "failed"
        stats = json.loads(batch["stats_json"])
        assert stats["error"]

        evidence = conn.execute(
            """SELECT kind, scope, sources_json, steps_json
               FROM evidence WHERE project_id=? AND kind='parse_failure'
               ORDER BY id DESC LIMIT 1""",
            (info.project_id,),
        ).fetchone()
        assert evidence is not None
        assert evidence["scope"] == "source"
        sources = json.loads(evidence["sources_json"])
        assert sources[0]["file_id"] == report.file_id
        assert sources[0]["batch_id"] == report.batch_id
        assert sources[0]["location"] == "文件级解析"
        assert json.loads(evidence["steps_json"])[0]["status"] == "failed"

        summary = build_project_summary(conn, info.project_id, read_only=True)
        failures = [
            item for item in summary.statuses["sheet_states"]
            if item["code"] == "parse_failed"
        ]
        assert failures
        assert failures[0]["entity_type"] == "parse_batch"
        assert failures[0]["batch_id"] == report.batch_id
        assert summary.statuses["project_status_code"] == "cannot_conclude"
        assert "sheet_parse_failed" in summary.statuses["project_status_reason_codes"]
    finally:
        conn.close()


def test_unknown_persisted_sheet_state_is_normalized_to_pending(tmp_path: Path):
    """未来状态码不能从摘要接口泄漏成第四级以外的业务状态。"""
    info = project_model.create_project("未知工作表状态", tmp_path / "workspace")
    info, conn = project_model.open_project(Path(info.workspace_path))
    source = tmp_path / "第1期.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "第1期"
    ws.append(["清单编码", "清单名称", "单位", "工程量", "综合单价", "合价"])
    ws.append(["A1", "测试项", "m2", 1, 2, 2])
    wb.save(source)
    try:
        report = settlement_io.import_settlement_file(
            conn, info.project_id, Path(info.workspace_path), source
        )
        sheet_id = conn.execute(
            "SELECT id FROM raw_sheets WHERE batch_id=?", (report.batch_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE raw_sheets SET sheet_status='future_state' WHERE id=?",
            (sheet_id,),
        )

        summary = build_project_summary(conn, info.project_id, read_only=True)

        state = next(
            item for item in summary.statuses["sheet_states"]
            if item["sheet_id"] == sheet_id
        )
        assert state["code"] == "pending"
        assert state["raw_code"] == "future_state"
        assert summary.statuses["sheet_status_code"][str(sheet_id)] == "pending"
        assert summary.statuses["project_status_code"] != "can_conclude"
    finally:
        conn.close()


def test_latest_successful_parse_supersedes_prior_failed_batch(
    tmp_path: Path, monkeypatch
):
    """同一源文件重试成功后，旧解析失败批次只保留历史，不阻断当前状态。"""
    info = project_model.create_project("解析批次重试", tmp_path / "workspace")
    info, conn = project_model.open_project(Path(info.workspace_path))
    source = tmp_path / "第1期.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "第1期"
    ws.append(["清单编码", "清单名称", "单位", "工程量", "综合单价", "合价"])
    ws.append(["A1", "测试项", "m2", 1, 2, 2])
    wb.save(source)
    real_parser = excel_parser.parse_file
    calls = 0

    def fail_once(path, file_type):
        nonlocal calls
        calls += 1
        if calls == 1:
            return excel_parser.ParseResult(
                parser="openpyxl", status="failed", error="一次性读取失败"
            )
        return real_parser(path, file_type)

    monkeypatch.setattr(excel_parser, "parse_file", fail_once)
    try:
        first = settlement_io.import_settlement_file(
            conn, info.project_id, Path(info.workspace_path), source
        )
        assert first.sheets[0].state_code == "parse_failed"
        second = settlement_io.import_settlement_file(
            conn, info.project_id, Path(info.workspace_path), source
        )
        assert second.status == "ok"

        summary = build_project_summary(conn, info.project_id, read_only=True)
        assert not any(
            item["code"] == "parse_failed"
            for item in summary.statuses["sheet_states"]
        )
        assert summary.statuses["sheet_states"]
    finally:
        conn.close()
