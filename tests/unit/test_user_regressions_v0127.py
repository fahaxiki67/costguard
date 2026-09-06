from decimal import Decimal

import pytest

from jiadun.core.db import migrations
from jiadun.core.engine import aggregate
from jiadun.core.matching import matching
from jiadun.core.matching.mirror import build_mirror_comparison


@pytest.fixture()
def project_db(tmp_path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        project_id = conn.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("回归候选", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
        period_ids = []
        for period_no in (1, 2):
            period_ids.append(
                conn.execute(
                    "INSERT INTO settlement_periods(project_id, period_no, title, direction)"
                    " VALUES (?, ?, ?, 'upward')",
                    (project_id, period_no, f"第{period_no}期"),
                ).lastrowid
            )
    yield conn, project_id, period_ids
    conn.close()


def _line(conn, period_id, *, code="", name="", unit="", quantity="1", amount="10"):
    with conn:
        conn.execute(
            "INSERT INTO line_items(period_id, code, name, unit, quantity, amount)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (period_id, code, name, unit, quantity, amount),
        )


def test_aggregate_normalizes_same_name_variants(project_db):
    conn, project_id, (period_1, period_2) = project_db
    _line(conn, period_1, name="水泥砂浆 楼地面", unit="m²", amount="10")
    _line(conn, period_2, name="水泥砂浆楼地面", unit="m²", amount="20")

    aggs = aggregate.aggregate_project(conn, project_id, direction="upward")

    assert len(aggs) == 1
    assert aggs[0].cum_amount == Decimal("30")


def test_aggregate_never_reports_one_effective_total_for_mixed_area_volume_units(project_db):
    conn, project_id, (period_1, period_2) = project_db
    _line(conn, period_1, code="S1", name="商砼", unit="m²", quantity="10", amount="100")
    _line(conn, period_2, code="S1", name="商砼", unit="m³", quantity="2", amount="200")

    aggs = aggregate.aggregate_project(conn, project_id, direction="upward")

    assert not any(
        item.cum_qty == Decimal("12") and item.cum_amount == Decimal("300")
        for item in aggs
    )


def test_fuzzy_name_match_cannot_merge_mixed_area_volume_units(project_db):
    conn, project_id, (period_1, period_2) = project_db
    base = "商品混凝土浇筑施工部位及标高说明和强度等级及配合比"
    _line(conn, period_1, name=base, unit="m²")
    _line(conn, period_2, name=base + "板", unit="m³")

    groups = matching.match_items(conn, project_id, direction="upward")

    item_ids = {row["id"] for row in conn.execute("SELECT id FROM line_items")}
    assert not any(
        set(group.item_ids) == item_ids and group.level == matching.PROBABLE
        for group in groups
    )


def test_mirror_does_not_compute_differences_across_mixed_units(project_db):
    conn, project_id, (period_1, period_2) = project_db
    with conn:
        conn.execute("UPDATE settlement_periods SET direction='downward' WHERE id=?", (period_2,))
    _line(conn, period_1, code="S2", name="商砼", unit="m²", quantity="10", amount="100")
    _line(conn, period_2, code="S2", name="商砼", unit="m³", quantity="2", amount="200")
    contract = matching.run_contract.ensure_run_contract(conn, project_id)
    with conn:
        match_id = conn.execute(
            "INSERT INTO matches(project_id, group_key, item_ids_json, level, method, score,"
            " status, run_signature, run_id) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (project_id, "downward:code:S2", "[]", matching.PROBABLE, "code_exact", 0.8,
             contract.signature, contract.run_id),
        ).lastrowid

    result = build_mirror_comparison(conn, project_id, int(match_id))

    assert result.amount_difference is None
