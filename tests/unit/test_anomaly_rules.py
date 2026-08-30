"""异常检测规则单元测试：手工构造数据，逐规则精确断言。"""
import json

import pytest

from costguard.core.anomalies import engine, rules
from costguard.core.db import migrations

D = 0


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        cur = conn.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at) VALUES ('t',1,'/t','2026')"
        )
        pid = cur.lastrowid
        pmap = {}
        for pno in (1, 2):
            cur = conn.execute(
                "INSERT INTO settlement_periods(project_id, period_no, title) VALUES (?,?,?)",
                (pid, pno, f"第{pno}期"),
            )
            pmap[pno] = cur.lastrowid
    yield conn, pid, pmap
    conn.close()


def add_item(conn, period_id, code="C1", name="清单A", qty="10", price="100", amount="1000",
             unit="m3", tax="0.09", flags=None):
    with conn:
        conn.execute(
            """INSERT INTO line_items(period_id, code, name, unit, quantity, unit_price, amount,
               tax_rate, flags_json) VALUES (?,?,?,?,?,?,?,?,?)""",
            (period_id, code, name, unit, qty, price, amount, tax,
             json.dumps(flags or {})),
        )


def attach_confirmed_sheet(conn, project_id, period_id, col_map, *, sheet_name="计算明细"):
    """给既有测试行补充人工确认后的来源 Sheet 与列映射。"""
    with conn:
        file_id = conn.execute(
            """INSERT INTO source_files(project_id, original_path, stored_path, original_name,
               sha256, size_bytes, file_type, imported_at) VALUES (?,?,?,?,?,?,?,?)""",
            (project_id, f"/{period_id}.xlsx", f"/{period_id}.xlsx", f"{period_id}.xlsx",
             f"sha-{period_id}-{sheet_name}", 1, "xlsx", "2026"),
        ).lastrowid
        batch_id = conn.execute(
            "INSERT INTO parse_batches(file_id, parser, parsed_at, status) VALUES (?,?,?,?)",
            (file_id, "test", "2026", "ok"),
        ).lastrowid
        sheet_id = conn.execute(
            """INSERT INTO raw_sheets(batch_id, sheet_index, sheet_name, n_rows, n_cols,
               period_id) VALUES (?,?,?,?,?,?)""",
            (batch_id, 0, sheet_name, 20, max(col_map.values()), period_id),
        ).lastrowid
        conn.execute(
            """INSERT INTO table_headers(sheet_id, header_row_lo, header_row_hi,
               col_map_json, confidence, needs_review, data_row_start, data_row_end)
               VALUES (?,?,?,?,?,?,?,?)""",
            (sheet_id, 5, 7, json.dumps(col_map), 1.0, 0, 8, 17),
        )
        conn.execute("UPDATE line_items SET sheet_id=? WHERE period_id=?", (sheet_id, period_id))
    return sheet_id


class TestRowRules:
    def test_qty_price_mismatch_high(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], qty="10", price="100", amount="1500")
        f = rules.rule_qty_price_amount(conn, pid)
        assert len(f) == 1 and f[0].severity == "high"
        assert "差异" in f[0].message

    def test_rounding_difference_low(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], qty="10", price="100", amount="1000.01")
        f = rules.rule_qty_price_amount(conn, pid)
        assert len(f) == 1 and f[0].rule_id == "rounding_difference" and f[0].severity == "low"

    def test_negative_qty(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], qty="-10", amount="-1000")
        f = rules.rule_negative_qty(conn, pid)
        assert len(f) == 1 and f[0].severity == "medium"

    def test_large_round_amount(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], amount="100000")
        f = rules.rule_round_amounts(conn, pid)
        assert len(f) == 1 and f[0].rule_id == "large_round_amount"

    def test_missing_fields(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], code="", name="", qty=None, amount=None)
        f = rules.rule_missing_key_fields(conn, pid)
        assert len(f) == 1
        assert "待补资料" in f[0].message

    def test_subtotal_rows_ignored(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], qty="10", price="100", amount="1500", flags={"subtotal": True})
        assert rules.rule_qty_price_amount(conn, pid) == []
        assert rules.rule_missing_key_fields(conn, pid) == []

    def test_confirmed_amount_only_path_does_not_require_qty_or_price(self, db):
        """人工确认名称+金额后，不得继续提示缺工程量、单价。"""
        conn, pid, pmap = db
        add_item(conn, pmap[1], qty=None, price=None, amount="3370591.52", flags={"row": 7})
        attach_confirmed_sheet(
            conn, pid, pmap[1], {"code": 1, "name": 2, "amount": 7}
        )
        assert rules.rule_missing_key_fields(conn, pid) == []

    def test_tax_amount_adjustment_does_not_require_code_qty_or_price(self, db):
        """税金行是金额调整项，有名称和金额即可，不应制造清单字段误报。"""
        conn, pid, pmap = db
        add_item(
            conn, pmap[1], code="", name="税金", qty=None, price=None,
            amount="1680", flags={"row": 17},
        )
        attach_confirmed_sheet(
            conn, pid, pmap[1],
            {"code": 1, "name": 2, "quantity": 9, "unit_price": 4, "amount": 10},
        )
        assert rules.rule_missing_key_fields(conn, pid) == []


class TestPeriodRules:
    def test_summary_mismatch(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], amount="1000")
        add_item(conn, pmap[1], amount="2000")
        add_item(conn, pmap[1], amount="3500", flags={"subtotal": True})  # 应为 3000
        f = rules.rule_subtotal_vs_details(conn, pid)
        assert len(f) == 1 and f[0].rule_id == "summary_mismatch" and f[0].severity == "high"

    def test_summary_missing_data(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], amount="1000")
        add_item(conn, pmap[1], amount=None)  # 缺失
        add_item(conn, pmap[1], amount="1000", flags={"subtotal": True})
        f = rules.rule_subtotal_vs_details(conn, pid)
        assert f[0].rule_id == "summary_missing_data"

    def test_subtotal_compares_only_preceding_detail_segment(self, db):
        """税金等后置调整不得被倒灌进前面的税前小计，制造高风险误报。"""
        conn, pid, pmap = db
        add_item(conn, pmap[1], amount="28000", flags={"row": 8})
        add_item(conn, pmap[1], amount="28000", flags={"row": 9})
        add_item(conn, pmap[1], amount="56000", flags={"row": 16, "subtotal": True})
        add_item(conn, pmap[1], amount="1680", flags={"row": 17})  # 小计后的税金
        assert rules.rule_subtotal_vs_details(conn, pid) == []

    def test_duplicates(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], code="C1", name="清单A")
        add_item(conn, pmap[1], code="C1", name="清单A")
        f = rules.rule_duplicates(conn, pid)
        assert len(f) == 1 and "重复" in f[0].message

    def test_same_code_diff_name(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], code="C1", name="清单A")
        add_item(conn, pmap[2], code="C1", name="清单A改")
        f = rules.rule_same_code_diff_name(conn, pid)
        assert len(f) == 1

    def test_same_name_diff_code(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], code="C1", name="清单A")
        add_item(conn, pmap[2], code="C2", name="清单A")
        f = rules.rule_same_name_diff_code(conn, pid)
        assert len(f) == 1

    def test_code_and_name_consistency_rules_do_not_mix_directions(self, db):
        conn, pid, pmap = db
        with conn:
            conn.execute(
                "UPDATE settlement_periods SET direction='upward' WHERE id=?", (pmap[1],)
            )
            conn.execute(
                "UPDATE settlement_periods SET direction='downward' WHERE id=?", (pmap[2],)
            )
        add_item(conn, pmap[1], code="C1", name="对上名称")
        add_item(conn, pmap[2], code="C1", name="对下名称")
        add_item(conn, pmap[1], code="U1", name="同名清单")
        add_item(conn, pmap[2], code="D1", name="同名清单")

        assert rules.rule_same_code_diff_name(conn, pid) == []
        assert rules.rule_same_name_diff_code(conn, pid) == []


class TestCrossPeriodRules:
    def test_tax_mode_comparison_is_isolated_by_direction(self, db):
        conn, pid, pmap = db
        with conn:
            conn.execute(
                "UPDATE settlement_periods SET direction='upward' WHERE id=?", (pmap[1],)
            )
            conn.execute(
                "UPDATE settlement_periods SET direction='downward' WHERE id=?", (pmap[2],)
            )
        up_sheet = attach_confirmed_sheet(
            conn, pid, pmap[1], {"name": 1, "unit_price": 2}, sheet_name="对上"
        )
        down_sheet = attach_confirmed_sheet(
            conn, pid, pmap[2], {"name": 1, "unit_price": 2}, sheet_name="对下"
        )
        with conn:
            conn.execute(
                "INSERT INTO raw_cells(sheet_id,row,col,raw_value) VALUES (?,?,?,?)",
                (up_sheet, 5, 2, "含税单价"),
            )
            conn.execute(
                "INSERT INTO raw_cells(sheet_id,row,col,raw_value) VALUES (?,?,?,?)",
                (down_sheet, 5, 2, "不含税单价"),
            )
        assert rules.rule_tax_mode_mixed(conn, pid) == []

        with conn:
            conn.execute(
                "UPDATE settlement_periods SET direction='upward' WHERE id=?", (pmap[2],)
            )
        findings = rules.rule_tax_mode_mixed(conn, pid)
        assert len(findings) == 1
        assert findings[0].details["direction"] == "upward"
    def test_unit_changed_and_alias_ok(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], unit="m3")
        add_item(conn, pmap[2], unit="立方米")
        # 别名归一化 → 不算单位变化
        assert rules.rule_unit_changed(conn, pid) == []
        add_item(conn, pmap[2], code="C1", name="清单A", unit="m2")
        f = rules.rule_unit_changed(conn, pid)
        assert len(f) == 1 and f[0].severity == "high"

    def test_price_change_graded(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], price="100")
        add_item(conn, pmap[2], price="115")  # +15%：仅记录
        f = rules.rule_price_changed(conn, pid)
        assert [x.rule_id for x in f] == ["price_changed"]

        conn2, pid2, pmap2 = db
        add_item(conn2, pmap2[1], code="C9", name="X", price="100")
        add_item(conn2, pmap2[2], code="C9", name="X", price="150")  # +50%：异常
        f2 = rules.rule_price_changed(conn2, pid2)
        assert f2[0].rule_id == "price_abnormal_change" and f2[0].severity == "high"

    def test_tax_changed(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], tax="0.09")
        add_item(conn, pmap[2], tax="0.13")
        f = rules.rule_tax_changed(conn, pid)
        assert len(f) == 1 and f[0].rule_id == "tax_rate_changed"

    def test_qty_spike(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], qty="100")
        add_item(conn, pmap[2], qty="250")  # 2.5 倍
        f = rules.rule_qty_spike(conn, pid)
        assert len(f) == 1 and "计量依据" in f[0].message

    def test_suspected_duplicate_settlement(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], qty="10", price="100", amount="1000")
        add_item(conn, pmap[2], qty="10", price="100", amount="1000")
        f = rules.rule_suspected_duplicate_settlement(conn, pid)
        assert len(f) == 1 and "重复结算" in f[0].message


class TestEngine:
    def test_engine_persists_with_evidence(self, db):
        conn, pid, pmap = db
        add_item(conn, pmap[1], qty="10", price="100", amount="1500")
        findings = engine.run_anomalies(conn, pid)
        assert findings
        n_anom = conn.execute("SELECT COUNT(*) c FROM anomalies WHERE project_id=?", (pid,)).fetchone()["c"]
        n_ev = conn.execute("SELECT COUNT(*) c FROM evidence WHERE project_id=?", (pid,)).fetchone()["c"]
        assert n_anom == len(findings)
        assert n_ev == len(findings)
        # 可重跑不重复累积
        engine.run_anomalies(conn, pid)
        n_anom2 = conn.execute("SELECT COUNT(*) c FROM anomalies WHERE project_id=?", (pid,)).fetchone()["c"]
        assert n_anom2 == len(findings)

    def test_rule_failure_recorded(self, db, monkeypatch):
        conn, pid, pmap = db
        def boom(conn, pid):
            raise RuntimeError("boom")
        findings = engine.run_anomalies(conn, pid, rules=[boom, rules.rule_negative_qty])
        assert any("rule_error" in f.rule_id for f in findings)
