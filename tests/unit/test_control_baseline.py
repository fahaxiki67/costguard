"""对上控制基准候选与上限比较测试（宪章 §六）。

候选只能来自已确认事实或人工显式登记；确认必须给依据；比较输出固定五态；
两个并存有效基准必须 CONTROL_CONFLICT，不允许自动挑选。
"""

import json

import docx as docx_lib
import pytest
from openpyxl import Workbook
from tests.unit.test_contract_extract import SAMPLE_CONTRACT

from jiadun.core.contracts import extract
from jiadun.core.db import migrations
from jiadun.core.engine import control_baseline as cb


@pytest.fixture()
def project_db(tmp_path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        project_id = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("控制基准测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
    yield conn, int(project_id), tmp_path
    conn.close()


@pytest.fixture()
def confirmed_amount_fact(project_db):
    """导入合成合同并把合同价款事实确认为 confirmed。"""
    conn, pid, pdir = project_db
    src = pdir / "分包合同.docx"
    d = docx_lib.Document()
    for line in SAMPLE_CONTRACT.splitlines():
        d.add_paragraph(line)
    d.save(str(src))
    extract.import_contract(conn, pid, pdir, src, doc_type="subcontract")
    fact_ids = [
        int(r["id"]) for r in conn.execute(
            """SELECT cf.id FROM contract_facts cf
               JOIN contract_docs cd ON cd.id=cf.doc_id
               WHERE cd.project_id=? AND cf.fact_key='contract_amount'
                 AND cf.fact_value LIKE '%1286500%'""",
            (pid,),
        ).fetchall()
    ]
    assert fact_ids
    extract.set_fact_review(conn, pid, fact_ids[0], "confirmed", reason="与合同原文核对一致")
    return conn, pid, fact_ids[0]


class TestCandidateLifecycle:
    def test_unconfirmed_fact_cannot_become_candidate(self, project_db):
        conn, pid, pdir = project_db
        src = pdir / "分包合同.docx"
        d = docx_lib.Document()
        for line in SAMPLE_CONTRACT.splitlines():
            d.add_paragraph(line)
        d.save(str(src))
        extract.import_contract(conn, pid, pdir, src, doc_type="subcontract")
        fact_id = int(conn.execute(
            """SELECT cf.id FROM contract_facts cf
               JOIN contract_docs cd ON cd.id=cf.doc_id
               WHERE cd.project_id=? AND cf.fact_key='contract_amount' LIMIT 1""",
            (pid,),
        ).fetchone()["id"])
        with pytest.raises(ValueError, match="已确认"):
            cb.create_candidate_from_fact(conn, pid, fact_id)

    def test_candidate_from_confirmed_fact(self, confirmed_amount_fact):
        conn, pid, fact_id = confirmed_amount_fact
        cb.create_candidate_from_fact(conn, pid, fact_id)
        baselines = cb.list_baselines(conn, pid)
        assert len(baselines) == 1
        b = baselines[0]
        assert b["status"] == "candidate"
        assert b["amount"].startswith("1286500")
        assert b["doc_title"] == "分包合同"

    def test_manual_candidate_requires_source(self, project_db):
        conn, pid, _ = project_db
        with pytest.raises(ValueError, match="出处"):
            cb.create_candidate_manual(conn, pid, "1286500", source_note="  ")
        with pytest.raises(ValueError, match="金额"):
            cb.create_candidate_manual(conn, pid, "abc", source_note="审计报告审定金额")

    def test_confirm_requires_reason(self, confirmed_amount_fact):
        conn, pid, fact_id = confirmed_amount_fact
        baseline_id = cb.create_candidate_from_fact(conn, pid, fact_id)
        with pytest.raises(ValueError, match="核对依据"):
            cb.set_baseline_review(conn, pid, baseline_id, "confirmed")
        cb.set_baseline_review(
            conn, pid, baseline_id, "confirmed",
            reason="终审报告审定表已核对，含税口径一致",
        )
        b = cb.list_baselines(conn, pid)[0]
        assert b["status"] == "confirmed"
        assert b["confirmed_by"] == "user"


class TestCompare:
    def test_unconfirmed_baseline_is_pending(self, confirmed_amount_fact):
        conn, pid, fact_id = confirmed_amount_fact
        baseline_id = cb.create_candidate_from_fact(conn, pid, fact_id)
        result = cb.compare_upward_result(conn, pid, baseline_id, "1200000")
        assert result["status"] == "PENDING"

    def test_unknown_tax_basis_is_pending(self, confirmed_amount_fact):
        conn, pid, fact_id = confirmed_amount_fact
        baseline_id = cb.create_candidate_from_fact(conn, pid, fact_id)
        cb.set_baseline_review(conn, pid, baseline_id, "confirmed", reason="核对一致")
        result = cb.compare_upward_result(conn, pid, baseline_id, "1200000")
        assert result["status"] == "PENDING"
        assert "税口径" in result["reason"]

    def test_tax_mismatch_is_incomparable(self, confirmed_amount_fact):
        conn, pid, fact_id = confirmed_amount_fact
        baseline_id = cb.create_candidate_from_fact(conn, pid, fact_id, tax_basis="included")
        cb.set_baseline_review(conn, pid, baseline_id, "confirmed", reason="核对一致")
        result = cb.compare_upward_result(
            conn, pid, baseline_id, "1200000", settlement_tax_basis="excluded"
        )
        assert result["status"] == "INCOMPARABLE"

    def test_pass_and_fail_with_delta(self, confirmed_amount_fact):
        conn, pid, fact_id = confirmed_amount_fact
        baseline_id = cb.create_candidate_from_fact(conn, pid, fact_id, tax_basis="included")
        cb.set_baseline_review(conn, pid, baseline_id, "confirmed", reason="核对一致")
        ok = cb.compare_upward_result(
            conn, pid, baseline_id, "1200000.00", settlement_tax_basis="included"
        )
        assert ok["status"] == "PASS"
        assert ok["delta"] == "-86500.00"
        over = cb.compare_upward_result(
            conn, pid, baseline_id, "1300000.00", settlement_tax_basis="included"
        )
        assert over["status"] == "FAIL"
        assert over["delta"] == "13500.00"

    def test_conflict_without_supersedes(self, confirmed_amount_fact):
        conn, pid, fact_id = confirmed_amount_fact
        first = cb.create_candidate_from_fact(conn, pid, fact_id, tax_basis="included")
        cb.set_baseline_review(conn, pid, first, "confirmed", reason="初审报告核对一致")
        second = cb.create_candidate_from_fact(
            conn, pid, fact_id, tax_basis="included",
        )
        cb.set_baseline_review(conn, pid, second, "confirmed", reason="终审报告核对一致")
        result = cb.compare_upward_result(
            conn, pid, second, "1200000", settlement_tax_basis="included"
        )
        assert result["status"] == "CONTROL_CONFLICT"

        # 冲突解除：终审版显式声明取代初审版 + 人工拒绝初审候选
        cb.set_baseline_review(conn, pid, first, "rejected", reason="被终审报告取代")
        third = cb.create_candidate_from_fact(
            conn, pid, fact_id, tax_basis="included", supersedes_id=second
        )
        cb.set_baseline_review(conn, pid, third, "confirmed", reason="终审版取代初审版")
        active = cb.compare_upward_result(
            conn, pid, third, "1200000", settlement_tax_basis="included"
        )
        assert active["status"] == "PASS"


class TestUpwardPeriods:
    def test_upward_periods_with_decimal_totals(self, project_db):
        conn, pid, _ = project_db
        with conn:
            cur = conn.execute(
                """INSERT INTO settlement_periods(project_id, period_no, title, direction, tax_mode)
                   VALUES (?,?,?,?,?)""",
                (pid, 1, "第一期对上结算", "upward", "included"),
            )
            period_id = cur.lastrowid
            for amount in ("1000.50", "2000.25", None):
                conn.execute(
                    """INSERT INTO line_items(period_id, name, amount) VALUES (?,?,?)""",
                    (period_id, "明细项", amount),
                )
            conn.execute(
                """INSERT INTO settlement_periods(project_id, period_no, title, direction)
                   VALUES (?,?,?,?)""",
                (pid, 2, "对下分包结算", "downward"),
            )
        periods = cb.list_upward_periods(conn, pid)
        assert len(periods) == 1
        assert periods[0]["amount_total"] == "3000.75"
        assert periods[0]["detail_rows"] == 3


class TestEvidence:
    def test_all_three_kinds_written(self, confirmed_amount_fact):
        conn, pid, fact_id = confirmed_amount_fact
        baseline_id = cb.create_candidate_from_fact(conn, pid, fact_id)
        cb.set_baseline_review(conn, pid, baseline_id, "confirmed", reason="核对一致")
        cb.compare_upward_result(conn, pid, baseline_id, "100", settlement_tax_basis="included")
        kinds = {
            r["kind"] for r in conn.execute(
                "SELECT DISTINCT kind FROM evidence WHERE project_id=?", (pid,)
            ).fetchall()
        }
        assert {"control_baseline", "control_baseline_review", "control_baseline_compare"} <= kinds


def _confirmed_baseline(conn, pid, fact_id, **kwargs) -> int:
    baseline_id = cb.create_candidate_from_fact(conn, pid, fact_id, **kwargs)
    cb.set_baseline_review(conn, pid, baseline_id, "confirmed", reason="终审报告已核对")
    return baseline_id


class TestComparisonFinding:
    """比较结论进入审核问题中心与导出（ROADMAP v0.1.25 遗留项）。"""

    def test_fail_recorded_as_high_finding(self, confirmed_amount_fact):
        conn, pid, fact_id = confirmed_amount_fact
        baseline_id = _confirmed_baseline(conn, pid, fact_id, tax_basis="included")
        result = cb.compare_upward_result(
            conn, pid, baseline_id, "1300000.00", settlement_tax_basis="included"
        )
        assert result["status"] == "FAIL"
        recorded = cb.record_comparison_finding(conn, pid, result, period_no=1)
        assert recorded["severity"] == "high"
        assert recorded["status"] == "FAIL"
        row = conn.execute(
            "SELECT * FROM anomalies WHERE id=?", (recorded["anomaly_id"],)
        ).fetchone()
        assert row is not None
        assert row["rule_id"] == cb.COMPARISON_RULE_ID
        assert row["subject_type"] == "control_baseline"
        assert row["subject_id"] == baseline_id
        assert row["lifecycle_status"] == "new"
        assert row["run_signature"] and row["run_id"]
        assert "1300000.00" in row["message"] and "13500.00" in row["message"]
        assert "不构成违规" in row["message"]
        raw = json.loads(row["raw_values_json"])
        assert raw["baseline_amount"] == "1286500.00"
        assert raw["settlement_amount"] == "1300000.00"
        assert raw["delta"] == "13500.00"
        assert raw["status"] == "FAIL"
        assert raw["period_no"] == 1
        limitations = json.loads(row["limitations_json"])
        assert any("不构成违规" in item for item in limitations)

    def test_five_states_severity_mapping(self, confirmed_amount_fact):
        conn, pid, fact_id = confirmed_amount_fact
        # INCOMPARABLE（税口径不同，单一有效基准）→ low
        sole = _confirmed_baseline(conn, pid, fact_id, tax_basis="included")
        res_incomp = cb.compare_upward_result(
            conn, pid, sole, "100", settlement_tax_basis="excluded"
        )
        assert res_incomp["status"] == "INCOMPARABLE"
        assert cb.record_comparison_finding(conn, pid, res_incomp)["severity"] == "low"
        # PASS → info
        ok = cb.compare_upward_result(
            conn, pid, sole, "1200000.00", settlement_tax_basis="included"
        )
        assert ok["status"] == "PASS"
        assert cb.record_comparison_finding(conn, pid, ok)["severity"] == "info"
        # PENDING（基准未确认）→ low
        pending = cb.create_candidate_from_fact(conn, pid, fact_id, tax_basis="included")
        res_pending = cb.compare_upward_result(
            conn, pid, pending, "100", settlement_tax_basis="included"
        )
        assert res_pending["status"] == "PENDING"
        assert cb.record_comparison_finding(conn, pid, res_pending)["severity"] == "low"
        # CONTROL_CONFLICT（两个有效已确认基准并存）→ medium
        cb.set_baseline_review(conn, pid, pending, "confirmed", reason="第二次核对一致")
        res_conflict = cb.compare_upward_result(
            conn, pid, pending, "100", settlement_tax_basis="included"
        )
        assert res_conflict["status"] == "CONTROL_CONFLICT"
        assert cb.record_comparison_finding(conn, pid, res_conflict)["severity"] == "medium"

    def test_new_comparison_supersedes_old_for_same_baseline_only(
        self, confirmed_amount_fact
    ):
        conn, pid, fact_id = confirmed_amount_fact
        first_base = _confirmed_baseline(conn, pid, fact_id, tax_basis="included")
        r1 = cb.compare_upward_result(
            conn, pid, first_base, "1200000.00", settlement_tax_basis="included"
        )
        first = cb.record_comparison_finding(conn, pid, r1)
        # 另一个基准的比较不受影响
        second_base = cb.create_candidate_from_fact(conn, pid, fact_id, tax_basis="excluded")
        cb.set_baseline_review(conn, pid, second_base, "confirmed", reason="核对一致")
        r2 = cb.compare_upward_result(
            conn, pid, second_base, "100", settlement_tax_basis="excluded"
        )
        cb.record_comparison_finding(conn, pid, r2)
        # 同一基准重新比较：旧结论转历史，新结论从新发现开始
        r3 = cb.compare_upward_result(
            conn, pid, first_base, "1300000.00", settlement_tax_basis="included"
        )
        third = cb.record_comparison_finding(conn, pid, r3)
        rows = conn.execute(
            """SELECT id, subject_id, lifecycle_status FROM anomalies
               WHERE project_id=? AND rule_id=? ORDER BY id""",
            (pid, cb.COMPARISON_RULE_ID),
        ).fetchall()
        assert [(r["subject_id"], r["lifecycle_status"]) for r in rows] == [
            (first_base, "historical"),
            (second_base, "new"),
            (first_base, "new"),
        ]
        old_ev = conn.execute(
            "SELECT scope FROM evidence WHERE id=?", (first["evidence_id"],)
        ).fetchone()
        new_ev = conn.execute(
            "SELECT scope FROM evidence WHERE id=?", (third["evidence_id"],)
        ).fetchone()
        assert old_ev["scope"] == "historical"
        assert new_ev["scope"] == "current"
        # 历史流转留痕
        events = conn.execute(
            "SELECT after_status FROM finding_status_events WHERE anomaly_id=?",
            (first["anomaly_id"],),
        ).fetchall()
        assert any(e["after_status"] == "historical" for e in events)

    def test_repeat_of_identical_conclusion_links_history(self, confirmed_amount_fact):
        conn, pid, fact_id = confirmed_amount_fact
        baseline_id = _confirmed_baseline(conn, pid, fact_id, tax_basis="included")
        result = cb.compare_upward_result(
            conn, pid, baseline_id, "1200000.00", settlement_tax_basis="included"
        )
        first = cb.record_comparison_finding(conn, pid, result)
        again = cb.record_comparison_finding(conn, pid, result)
        assert again["anomaly_id"] != first["anomaly_id"]
        row = conn.execute(
            "SELECT repeat_history_json, lifecycle_status FROM anomalies WHERE id=?",
            (again["anomaly_id"],),
        ).fetchone()
        assert row["lifecycle_status"] == "new"
        history = json.loads(row["repeat_history_json"])
        assert history and history[0]["anomaly_id"] == first["anomaly_id"]

    def test_unknown_status_rejected(self, project_db):
        conn, pid, _ = project_db
        with pytest.raises(ValueError, match="未知的控制基准比较状态"):
            cb.record_comparison_finding(conn, pid, {"status": "MAYBE", "baseline_id": 1})

    def test_anomaly_detection_keeps_baseline_cap_findings(self, confirmed_amount_fact):
        """重跑异常检测不得清扫输入绑定的基准比较结论（回归）。"""
        from jiadun.core.anomalies import engine as anomaly_engine

        conn, pid, fact_id = confirmed_amount_fact
        baseline_id = _confirmed_baseline(conn, pid, fact_id, tax_basis="included")
        result = cb.compare_upward_result(
            conn, pid, baseline_id, "1300000.00", settlement_tax_basis="included"
        )
        recorded = cb.record_comparison_finding(conn, pid, result)
        anomaly_engine.run_anomalies(conn, pid)
        row = conn.execute(
            "SELECT lifecycle_status FROM anomalies WHERE id=?",
            (recorded["anomaly_id"],),
        ).fetchone()
        assert row["lifecycle_status"] == "new"

    def test_export_sheet_contains_conclusion(self, confirmed_amount_fact):
        conn, pid, fact_id = confirmed_amount_fact
        baseline_id = _confirmed_baseline(conn, pid, fact_id, tax_basis="included")
        result = cb.compare_upward_result(
            conn, pid, baseline_id, "1300000.00", settlement_tax_basis="included"
        )
        cb.record_comparison_finding(conn, pid, result, period_no=1)
        from jiadun.core.export.excel_export import export_control_baseline_compares

        wb = Workbook()
        wb.remove(wb.active)
        export_control_baseline_compares(conn, pid, wb)
        ws = wb["控制基准比较"]
        assert ws.max_row == 2
        data = [cell.value for cell in ws[2]]
        assert data[2] == "超上限（FAIL）"
        assert data[3] == f"#{baseline_id}"
        assert data[4] == "1286500.00"
        assert data[5] == "1300000.00"
        assert data[6] == "13500.00"
        assert data[7] == "第 1 期"
