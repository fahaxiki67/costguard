"""全 Sheet 清单与类型建议测试（任务书任务 B1/B2/B3）。"""

import pytest

from jiadun.core.db import migrations
from jiadun.core.engine import sheet_inventory


@pytest.fixture()
def project_db(tmp_path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        pid = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("Sheet清单测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
    yield conn, int(pid), tmp_path
    conn.close()


def _add_file_with_sheets(conn, pid, tmp_path, name, sheets):
    """登记文件与批次并写入若干 Sheet（name, status, reason, n_rows）。"""
    from jiadun.core.models.source_file import import_file

    src = tmp_path / name
    src.write_bytes(b"placeholder")
    sf = import_file(conn, pid, tmp_path, src)
    now = "2026-09-05T00:00:00"
    with conn:
        cur = conn.execute(
            """INSERT INTO parse_batches(file_id, parser, parsed_at, status, stats_json)
               VALUES (?,?,?,?,?)""",
            (sf.file_id, "pipeline", now, "ok", "{}"),
        )
        batch_id = cur.lastrowid
        for index, (sheet_name, status, reason, n_rows) in enumerate(sheets, start=1):
            conn.execute(
                """INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name,
                   n_rows, n_cols, sheet_status, sheet_status_reason)
                   VALUES (?,?,?,?,?,?,?)""",
                (batch_id, index, sheet_name, n_rows, 8, status, reason),
            )
    return sf.file_id


class TestListWorkbookSheets:
    def test_lists_all_sheets_including_confirmed_and_non_pending(self, project_db):
        conn, pid, tmp = project_db
        fid = _add_file_with_sheets(
            conn, pid, tmp, "多Sheet工作簿.xlsx",
            [
                ("分部分项清单", "pending", "表头歧义待人工确认", 120),
                ("总价措施项目清单与计价表", "pending", "汇总样式", 20),
                ("封面", "confirmed", "非业务页已确认", 3),
                ("汇总表", "confirmed", "汇总页已确认", 10),
            ],
        )
        sheets = sheet_inventory.list_workbook_sheets(conn, pid)
        assert len(sheets) == 4  # 全列表：pending + confirmed 都在
        by_name = {s["sheet_name"]: s for s in sheets}
        assert by_name["分部分项清单"]["suggested_kind"] == "boq_detail"
        assert "分部分项" in by_name["分部分项清单"]["suggest_reason"]
        assert by_name["总价措施项目清单与计价表"]["suggested_kind"] == "measure_total"
        assert "费率计取" in by_name["总价措施项目清单与计价表"]["suggest_reason"]
        assert by_name["封面"]["suggested_kind"] == "non_business"

    def test_confirmed_sheet_keeps_human_kind(self, project_db):
        conn, pid, tmp = project_db
        fid = _add_file_with_sheets(
            conn, pid, tmp, "工作簿.xlsx", [("汇总表", "confirmed", "已确认", 10)]
        )
        conn.execute(
            "UPDATE raw_sheets SET list_kind='measure_unit' WHERE sheet_name='汇总表'"
        )
        sheets = sheet_inventory.list_workbook_sheets(conn, pid)
        assert sheets[0]["suggested_kind"] == "measure_unit"

    def test_filters(self, project_db):
        conn, pid, tmp = project_db
        fid = _add_file_with_sheets(
            conn, pid, tmp, "工作簿.xlsx",
            [
                ("分部分项清单", "pending", "待确认", 50),
                ("封面", "confirmed", "已确认", 2),
            ],
        )
        by_status = sheet_inventory.list_workbook_sheets(conn, pid, status="pending")
        assert [s["sheet_name"] for s in by_status] == ["分部分项清单"]
        by_kw = sheet_inventory.list_workbook_sheets(conn, pid, keyword="封面")
        assert [s["sheet_name"] for s in by_kw] == ["封面"]
        by_file = sheet_inventory.list_workbook_sheets(conn, pid, file_id=fid)
        assert len(by_file) == 2

    def test_unknown_when_no_feature(self, project_db):
        conn, pid, tmp = project_db
        _add_file_with_sheets(conn, pid, tmp, "工作簿.xlsx", [("Sheet1", "pending", "", 5)])
        sheets = sheet_inventory.list_workbook_sheets(conn, pid)
        assert sheets[0]["suggested_kind"] == "unknown"


class TestSetSheetListKind:
    def test_human_annotation_requires_reason(self, project_db):
        conn, pid, tmp = project_db
        fid = _add_file_with_sheets(
            conn, pid, tmp, "工作簿.xlsx", [("清单", "pending", "待确认", 30)]
        )
        sheet_id = sheet_inventory.list_workbook_sheets(conn, pid)[0]["sheet_id"]
        with pytest.raises(ValueError, match="理由"):
            sheet_inventory.set_sheet_list_kind(conn, pid, sheet_id, "boq_detail")
        result = sheet_inventory.set_sheet_list_kind(
            conn, pid, sheet_id, "boq_detail", reason="表头含 编码/名称/工程量/单价/合价"
        )
        assert result["after"] == "boq_detail"
        kinds = {
            r["kind"] for r in conn.execute(
                "SELECT DISTINCT kind FROM evidence WHERE project_id=?", (pid,)
            ).fetchall()
        }
        assert "sheet_list_kind" in kinds

    def test_invalid_kind_and_foreign_sheet(self, project_db):
        conn, pid, tmp = project_db
        _add_file_with_sheets(conn, pid, tmp, "工作簿.xlsx", [("清单", "pending", "", 30)])
        sheet_id = sheet_inventory.list_workbook_sheets(conn, pid)[0]["sheet_id"]
        with pytest.raises(ValueError, match="未知的清单类型"):
            sheet_inventory.set_sheet_list_kind(conn, pid, sheet_id, "golden")
        with pytest.raises(ValueError, match="不存在或不属于当前项目"):
            sheet_inventory.set_sheet_list_kind(conn, pid, 999999, "boq_detail", reason="x")
