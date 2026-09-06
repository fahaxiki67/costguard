import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiadun.core.db import migrations
from jiadun.core.engine import settlement_io
from jiadun.core.matching import matching
from jiadun.core.matching.mirror import build_mirror_comparison
from jiadun.core.models import project as project_model
from jiadun.core.parsing import excel_parser


@pytest.fixture()
def project_db(tmp_path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        project_id = conn.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("剩余门控", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
        upward = conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction)"
            " VALUES (?, ?, ?, 'upward')",
            (project_id, 1, "对上"),
        ).lastrowid
        downward = conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction)"
            " VALUES (?, ?, ?, 'downward')",
            (project_id, 1, "对下"),
        ).lastrowid
    yield conn, project_id, upward, downward
    conn.close()


def _line(conn, period_id, *, unit, quantity, unit_price, amount):
    with conn:
        conn.execute(
            "INSERT INTO line_items(period_id, code, name, unit, quantity, unit_price, amount, flags_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (period_id, "K", "同项", unit, quantity, unit_price, amount, "{}"),
        )


def _match(conn, project_id):
    contract = matching.run_contract.ensure_run_contract(conn, project_id)
    with conn:
        return conn.execute(
            "INSERT INTO matches(project_id, group_key, item_ids_json, level, method, score,"
            " status, run_signature, run_id) VALUES (?, ?, '[]', ?, ?, ?, 'pending', ?, ?)",
            (project_id, "downward:code:K", matching.PROBABLE, "code_exact", 0.8,
             contract.signature, contract.run_id),
        ).lastrowid


@pytest.mark.parametrize(
    "case",
    ["missing", "unknown", "same_side_multiple"],
)
def test_mirror_never_computes_numeric_difference_without_one_known_unit_per_side(
    project_db, case
):
    conn, project_id, upward, downward = project_db
    if case == "missing":
        _line(conn, upward, unit="", quantity="10", unit_price="100", amount="1000")
        _line(conn, downward, unit="m3", quantity="20", unit_price="50", amount="1000")
    elif case == "unknown":
        _line(conn, upward, unit="unknown", quantity="10", unit_price="100", amount="1000")
        _line(conn, downward, unit="m3", quantity="20", unit_price="50", amount="1000")
    else:
        _line(conn, upward, unit="m3", quantity="10", unit_price="100", amount="1000")
        _line(conn, upward, unit="m2", quantity="10", unit_price="100", amount="1000")
        _line(conn, downward, unit="m3", quantity="20", unit_price="50", amount="1000")

    result = build_mirror_comparison(conn, project_id, int(_match(conn, project_id)))

    assert result.reason != "按当前匹配规则生成左右镜像复核"
    for field in result.fields:
        if field.field in {"quantity", "unit_price", "amount"}:
            assert field.difference is None, (case, field)
            assert field.difference_rate is None, (case, field)
    assert result.quantity_difference is None
    assert result.unit_price_difference is None
    assert result.amount_difference is None


@pytest.mark.parametrize(
    "unit",
    ["待确认", "待补资料", "未知单位", "TBD", "—", "–", "#N/A", "N.A."],
)
def test_mirror_treats_explicit_unknown_unit_tokens_as_incomparable(project_db, unit):
    conn, project_id, upward, downward = project_db
    _line(conn, upward, unit=unit, quantity="10", unit_price="100", amount="1000")
    _line(conn, downward, unit=unit, quantity="20", unit_price="50", amount="1000")

    result = build_mirror_comparison(conn, project_id, int(_match(conn, project_id)))

    by_field = {field.field: field for field in result.fields}
    assert by_field["unit"].status != "完全一致"
    for field in ("quantity", "unit_price", "amount"):
        assert by_field[field].difference is None, (unit, by_field[field])
        assert by_field[field].difference_rate is None, (unit, by_field[field])
    assert result.quantity_difference is None
    assert result.unit_price_difference is None
    assert result.amount_difference is None


def test_fuzzy_match_assigns_each_item_to_one_group_before_saving(project_db):
    conn, project_id, period_id, _period_2 = project_db
    base = "现浇C25商品混凝土垫层泵送商品砼运至现场含浇捣养护及运输费"
    with conn:
        item_1 = conn.execute(
            "INSERT INTO line_items(period_id, name, unit) VALUES (?,?,?)",
            (period_id, base, "m3"),
        ).lastrowid
        item_2 = conn.execute(
            "INSERT INTO line_items(period_id, name, unit) VALUES (?,?,?)",
            (period_id, base + "率", "m2"),
        ).lastrowid

    groups = matching.match_items(conn, project_id, direction="upward")
    memberships = {
        item_id: [group for group in groups if item_id in group.item_ids]
        for item_id in (item_1, item_2)
    }
    assert all(len(item_groups) == 1 for item_groups in memberships.values()), memberships
    assert any(
        group.level == matching.INCOMPARABLE and {item_1, item_2}.issubset(group.item_ids)
        for group in groups
    )

    saved = matching.save_matches(conn, project_id, groups)
    rows = conn.execute(
        "SELECT item_ids_json FROM matches WHERE project_id=?", (project_id,)
    ).fetchall()
    assert saved == len(rows)
    saved_memberships = {
        item_id: [row for row in rows if item_id in json.loads(row["item_ids_json"])]
        for item_id in (item_1, item_2)
    }
    assert all(len(item_rows) == 1 for item_rows in saved_memberships.values()), saved_memberships


def test_name_merge_keeps_every_item_in_one_group_before_saving(project_db):
    conn, project_id, period_1, period_2 = project_db
    with conn:
        period_2_upward = conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction)"
            " VALUES (?, ?, ?, 'upward')",
            (project_id, 2, "第2期对上"),
        ).lastrowid
        period_3 = conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction)"
            " VALUES (?, ?, ?, 'upward')",
            (project_id, 3, "第3期"),
        ).lastrowid
        period_4 = conn.execute(
            "INSERT INTO settlement_periods(project_id, period_no, title, direction)"
            " VALUES (?, ?, ?, 'upward')",
            (project_id, 4, "第4期"),
        ).lastrowid
        item_ids = [
            conn.execute(
                "INSERT INTO line_items(period_id, code, name, unit) VALUES (?,?,?,?)",
                (period, code, name, "m3"),
            ).lastrowid
            for period, code, name in (
                (period_1, "A", "x"),
                (period_2_upward, "B", "x"),
                (period_3, "B", "y"),
                (period_4, "C", "y"),
            )
        ]

    groups = matching.match_items(conn, project_id, direction="upward")
    memberships = {
        item_id: [group for group in groups if item_id in group.item_ids]
        for item_id in item_ids
    }
    assert all(len(item_groups) == 1 for item_groups in memberships.values()), memberships

    matching.save_matches(conn, project_id, groups)
    rows = conn.execute(
        "SELECT item_ids_json FROM matches WHERE project_id=?", (project_id,)
    ).fetchall()
    saved_memberships = {
        item_id: [row for row in rows if item_id in json.loads(row["item_ids_json"])]
        for item_id in item_ids
    }
    assert all(len(item_rows) == 1 for item_rows in saved_memberships.values()), saved_memberships


def test_csv_import_marks_the_single_sheet_visible_and_materializes_rows(tmp_path):
    info = project_model.create_project("CSV 可见态", tmp_path / "workspace")
    info, conn = project_model.open_project(Path(info.workspace_path))
    source = tmp_path / "第1期.csv"
    source.write_text(
        "清单编码,清单名称,项目特征,单位,工程量,综合单价,合价\n"
        "A1,项目A,普通,m3,1,100,100\n",
        encoding="utf-8",
    )
    try:
        report = settlement_io.import_settlement_file(
            conn, info.project_id, Path(info.workspace_path), source,
            period_no=1, direction="upward",
        )

        assert report.status == "ok"
        assert report.period_id > 0
        assert report.sheets[0].status == "parsed"
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM line_items WHERE period_id=?",
            (report.period_id,),
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_xls_parser_reads_available_sheet_visibility(monkeypatch, tmp_path):
    class FakeCell:
        ctype = 1
        value = "x"

    class FakeSheet:
        name = "隐藏页"
        nrows = 1
        ncols = 1
        rowinfo_map = {}
        colinfo_map = {}

        def cell(self, _row, _col):
            return FakeCell()

    class FakeBook:
        nsheets = 1
        _sheet_visibility = [1]

        def sheet_by_index(self, _index):
            return FakeSheet()

        def release_resources(self):
            return None

    monkeypatch.setitem(sys.modules, "xlrd", SimpleNamespace(open_workbook=lambda _path: FakeBook()))

    result = excel_parser.parse_xls(tmp_path / "sample.xls")

    assert result.sheets[0].visible_state == "hidden"


def test_unknown_xls_visibility_stays_pending_without_canonical_rows(monkeypatch, tmp_path):
    info = project_model.create_project("未知 xls 可见性", tmp_path / "workspace")
    info, conn = project_model.open_project(Path(info.workspace_path))
    source = tmp_path / "unknown.xls"
    source.write_bytes(b"placeholder")
    from jiadun.core.parsing.excel_parser import CellRecord, ParseResult, SheetRecord

    values = [
        (1, 1, "项目编码"), (1, 2, "项目名称"), (1, 3, "工程量"),
        (1, 4, "综合单价"), (1, 5, "合价"),
        (2, 1, "A1"), (2, 2, "项目A"), (2, 3, "1"), (2, 4, "100"), (2, 5, "100"),
        (3, 2, "合计"), (3, 5, "100"),
    ]
    fake_sheet = SheetRecord(
        sheet_index=0,
        sheet_name="第1期明细",
        n_rows=3,
        n_cols=5,
        visible_state=None,
        cells=[
            CellRecord(
                row=row, col=col, raw_value=value, cached_value=value,
                is_formula=False, is_number_stored_as_text=False, num_fmt=""
            )
            for row, col, value in values
        ],
    )
    fake_result = ParseResult(parser="xlrd", status="partial", sheets=[fake_sheet])
    monkeypatch.setattr(excel_parser, "parse_file", lambda *_args, **_kwargs: fake_result)

    try:
        report = settlement_io.import_settlement_file(
            conn, info.project_id, Path(info.workspace_path), source, direction="upward"
        )
        sheet = conn.execute(
            "SELECT sheet_status, sheet_status_reason FROM raw_sheets WHERE batch_id=?",
            (report.batch_id,),
        ).fetchone()
        assert report.status == "partial"
        assert sheet["sheet_status"] == "pending"
        assert "可见性未知" in sheet["sheet_status_reason"]
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM settlement_periods WHERE project_id=?",
            (info.project_id,),
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM line_items WHERE period_id IN "
            "(SELECT id FROM settlement_periods WHERE project_id=?)",
            (info.project_id,),
        ).fetchone()["c"] == 0
    finally:
        conn.close()
