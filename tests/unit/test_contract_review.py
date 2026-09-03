"""合同事实确认生命周期测试（阶段 C-1）。

抽取产出只是候选；只有人工确认的事实才是已确认合同事实；
历史数据不得自动升 confirmed；所有流转必须留下审计 Evidence。
"""

import docx as docx_lib
import pytest
from tests.unit.test_contract_extract import SAMPLE_CONTRACT

from jiadun.core.contracts import extract, run_contract
from jiadun.core.db import migrations


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        pid = conn.execute(
            "INSERT INTO projects(name, schema_version, workspace_path, created_at) VALUES ('t',1,'/t','2026')"
        ).lastrowid
    yield conn, pid, tmp_path
    conn.close()


@pytest.fixture()
def imported_doc(db, tmp_path):
    """导入一份含完整条款的合同，返回 (conn, pid, pdir, doc_id)。"""
    conn, pid, pdir = db
    src = pdir / "分包合同.docx"
    d = docx_lib.Document()
    for line in SAMPLE_CONTRACT.splitlines():
        d.add_paragraph(line)
    d.save(str(src))
    doc_id = extract.import_contract(conn, pid, pdir, src, doc_type="subcontract")
    return conn, pid, pdir, doc_id


def _fact_ids(conn, doc_id, fact_key):
    return [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM contract_facts WHERE doc_id=? AND fact_key=? ORDER BY id",
            (doc_id, fact_key),
        ).fetchall()
    ]


class TestCandidateByDefault:
    def test_new_facts_are_candidates(self, imported_doc):
        conn, pid, _, doc_id = imported_doc
        rows = conn.execute(
            "SELECT review_status, reviewed_at, reviewed_by FROM contract_facts WHERE doc_id=?",
            (doc_id,),
        ).fetchall()
        assert rows
        assert all(r["review_status"] == "candidate" for r in rows)
        assert all(r["reviewed_at"] is None and r["reviewed_by"] is None for r in rows)

    def test_migration_backfills_legacy_as_candidate(self, imported_doc, tmp_path):
        """历史事实无法证明经过人工确认：迁移回填 candidate，不自动升 confirmed。"""
        conn, pid, _, _ = imported_doc
        # 把库降回 v47 形态：删列 + 回退迁移记录
        with conn:
            for col in ("review_status", "reviewed_at", "reviewed_by", "review_reason"):
                conn.execute(f"ALTER TABLE contract_facts DROP COLUMN {col}")
            conn.execute("DELETE FROM schema_migrations WHERE version>=48")
            conn.execute("UPDATE projects SET schema_version=47")
        conn.close()

        final = migrations.migrate(tmp_path / "project.db", tmp_path / "backups2")
        assert final == migrations.LATEST_SCHEMA_VERSION
        conn2 = migrations.connect(tmp_path / "project.db")
        try:
            rows = conn2.execute(
                "SELECT review_status, reviewed_at, reviewed_by FROM contract_facts"
            ).fetchall()
            assert rows
            assert all(r["review_status"] == "candidate" for r in rows)
            assert all(r["reviewed_at"] is None for r in rows)
        finally:
            conn2.close()


class TestSetFactReview:
    def test_confirm_records_reviewer_and_evidence(self, imported_doc):
        conn, pid, _, doc_id = imported_doc
        fact_id = _fact_ids(conn, doc_id, "contract_amount")[0]
        result = extract.set_fact_review(
            conn, pid, fact_id, "confirmed", reviewed_by="tester", reason="与合同原文核对一致"
        )
        assert result["before"] == "candidate"
        assert result["after"] == "confirmed"
        row = conn.execute(
            "SELECT review_status, reviewed_by, review_reason FROM contract_facts WHERE id=?",
            (fact_id,),
        ).fetchone()
        assert row["review_status"] == "confirmed"
        assert row["reviewed_by"] == "tester"
        assert "原文核对" in row["review_reason"]
        ev = conn.execute(
            "SELECT COUNT(*) AS n FROM evidence WHERE kind='contract_fact_review'"
        ).fetchone()
        assert ev["n"] >= 1

    def test_overturn_requires_reason(self, imported_doc):
        conn, pid, _, doc_id = imported_doc
        fact_id = _fact_ids(conn, doc_id, "contract_amount")[0]
        extract.set_fact_review(conn, pid, fact_id, "confirmed", reason="核对一致")
        with pytest.raises(ValueError, match="理由"):
            extract.set_fact_review(conn, pid, fact_id, "rejected")
        with pytest.raises(ValueError, match="理由"):
            extract.set_fact_review(conn, pid, fact_id, "rejected", reason="  ")
        result = extract.set_fact_review(
            conn, pid, fact_id, "rejected", reason="条款已由补充协议替换"
        )
        assert result["before"] == "confirmed"

    def test_invalid_decision_and_foreign_fact(self, imported_doc):
        conn, pid, _, doc_id = imported_doc
        fact_id = _fact_ids(conn, doc_id, "contract_amount")[0]
        with pytest.raises(ValueError, match="未知的确认决定"):
            extract.set_fact_review(conn, pid, fact_id, "auto_confirm")
        with pytest.raises(ValueError, match="不存在或不属于当前项目"):
            extract.set_fact_review(conn, pid, 999999, "confirmed")


class TestRunContractGating:
    def test_rejected_excluded_candidate_tagged(self, imported_doc):
        conn, pid, _, doc_id = imported_doc
        for amount_id in _fact_ids(conn, doc_id, "contract_amount"):
            extract.set_fact_review(conn, pid, amount_id, "rejected", reason="金额引用错误")
        facts = run_contract._contract_facts(conn, pid)
        by_key = {}
        for f in facts:
            by_key.setdefault(f["fact_key"], []).append(f)
        assert by_key.get("contract_amount") in (None, [])  # 拒绝 → 剔除
        assert by_key.get("payment_clause"), "候选事实应保留在载荷中"
        for f in facts:
            assert f["review_status"] in ("candidate", "confirmed", "needs_review")

    def test_review_summary_counts(self, imported_doc):
        conn, pid, _, doc_id = imported_doc
        amount_id = _fact_ids(conn, doc_id, "contract_amount")[0]
        extract.set_fact_review(conn, pid, amount_id, "confirmed")
        payload = run_contract.ensure_run_contract(conn, pid)
        summary = payload.components["contract_fact_review_summary"]
        assert summary["confirmed"] >= 1
        assert summary["candidate"] >= 1
        assert summary["rejected"] == 0


class TestRiskSemantics:
    def test_rejected_clause_counts_as_missing(self, imported_doc):
        conn, pid, _, doc_id = imported_doc
        for fact_id in _fact_ids(conn, doc_id, "payment_clause"):
            extract.set_fact_review(conn, pid, fact_id, "rejected", reason="原文无此条款")
        risks = extract.contract_risks(conn, pid)
        missing = {r["fact_key"] for r in risks if "缺少付款" in r["message"]}
        assert "payment_clause" in missing

    def test_pending_candidates_visible(self, imported_doc):
        conn, pid, _, doc_id = imported_doc
        risks = extract.contract_risks(conn, pid)
        pending = [r for r in risks if r["fact_key"] == "fact_review_pending"]
        assert pending, "候选条款必须产生待确认提示"
        # 完整合同的既有缺失类风险语义不变
        assert not any(r["severity"] == "high" for r in risks), risks

    def test_confirmed_clears_pending(self, imported_doc):
        conn, pid, _, doc_id = imported_doc
        rows = conn.execute(
            "SELECT id, review_status FROM contract_facts WHERE doc_id=?", (doc_id,)
        ).fetchall()
        for r in rows:
            if (r["review_status"] or "candidate") in ("candidate", "needs_review"):
                extract.set_fact_review(conn, pid, int(r["id"]), "confirmed")
        risks = extract.contract_risks(conn, pid)
        assert not any(r["fact_key"] == "fact_review_pending" for r in risks)
