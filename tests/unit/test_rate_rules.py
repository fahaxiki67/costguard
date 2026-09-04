"""框架/管理性协议费率规则测试（宪章 §五）。

抽取只产候选、多百分比各成一条；确认必须人工设定基数；试算 Decimal 确定性。
"""


import pytest

from jiadun.core import document_intake
from jiadun.core.contracts import rate_rules
from jiadun.core.db import migrations
from jiadun.core.models.source_file import import_file


@pytest.fixture()
def project_db(tmp_path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        project_id = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("费率规则测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
    yield conn, int(project_id), tmp_path
    conn.close()


@pytest.fixture()
def make_framework_doc(project_db):
    """工厂：写入指定内容并以框架协议类别登记只读副本，返回 (conn, pid, sf)。"""
    conn, pid, pdir = project_db

    def _make(content: str, name: str = "框架协议.txt"):
        src = pdir / name
        src.write_text(content, encoding="utf-8")
        sf = import_file(conn, pid, pdir, src)
        document_intake.record_document(
            conn, pid, sf.file_id, category="upward_framework_management",
            parse_status="evidence_only", detail="", parser="",
        )
        return conn, pid, sf

    return _make


class TestExtraction:
    def test_multi_percent_become_separate_candidates(self):
        paras = [{
            "index": 5,
            "text": "承包人按结算价的3%上缴管理费，按结算价的2%上缴配合费；"
                    "监理按0.5%计取。",
        }]
        cands = rate_rules.extract_rate_candidates_from_paragraphs(paras)
        assert [c["rate_percent"] for c in cands] == ["3", "2", "0.5"]
        assert all(c["quote_text"] for c in cands)

    def test_points_style_percent(self):
        paras = [{"index": 1, "text": "协作方收取 3 个点的管理费。"}]
        cands = rate_rules.extract_rate_candidates_from_paragraphs(paras)
        assert [c["rate_percent"] for c in cands] == ["3"]

    def test_no_trigger_no_candidate(self):
        paras = [{"index": 1, "text": "付款周期为 28 天，逾期按日 0.05% 计息。"}]
        assert rate_rules.extract_rate_candidates_from_paragraphs(paras) == []

    def test_rate_sentence_without_number_skipped(self):
        paras = [{"index": 1, "text": "管理费另行协商确定。"}]
        assert rate_rules.extract_rate_candidates_from_paragraphs(paras) == []


class TestImportAndConfirm:
    def test_import_candidates_from_file(self, make_framework_doc):
        conn, pid, sf = make_framework_doc(
            "协作管理费按对上结算价的3%收取，另按结算价的2%收取配合费。"
        )
        n = rate_rules.import_rate_candidates(conn, pid, sf.file_id)
        assert n == 2
        rules = rate_rules.list_rate_rules(conn, pid)
        assert all(r["status"] == "candidate" for r in rules)
        assert all(r["base_type"] == "unset" for r in rules)
        with pytest.raises(ValueError, match="已登记过"):
            rate_rules.import_rate_candidates(conn, pid, sf.file_id)

    def test_confirm_requires_base_fields(self, make_framework_doc):
        conn, pid, sf = make_framework_doc("管理费按结算价 5% 收取。")
        rate_rules.import_rate_candidates(conn, pid, sf.file_id)
        rule_id = rate_rules.list_rate_rules(conn, pid)[0]["id"]
        with pytest.raises(ValueError, match="base_type"):
            rate_rules.confirm_rate_rule(conn, pid, rule_id, base_type="unset",
                                         base_definition="按对上结算价")
        with pytest.raises(ValueError, match="基数说明"):
            rate_rules.confirm_rate_rule(conn, pid, rule_id, base_type="custom",
                                         base_definition="  ")
        result = rate_rules.confirm_rate_rule(
            conn, pid, rule_id,
            base_type="upward_settlement_amount",
            base_definition="按对上结算审定金额计取，含税",
            tax_basis="included",
        )
        assert result["status"] == "confirmed"

    def test_reject_requires_reason(self, make_framework_doc):
        conn, pid, sf = make_framework_doc("管理费按 5% 收取。")
        rate_rules.import_rate_candidates(conn, pid, sf.file_id)
        rule_id = rate_rules.list_rate_rules(conn, pid)[0]["id"]
        with pytest.raises(ValueError, match="理由"):
            rate_rules.reject_rate_rule(conn, pid, rule_id, reason="")
        rate_rules.reject_rate_rule(conn, pid, rule_id, reason="该比例属履约保证金")
        rules = rate_rules.list_rate_rules(conn, pid, status="rejected")
        assert len(rules) == 1


class TestApply:
    def _confirmed_rule(self, make_framework_doc, **kwargs):
        conn, pid, sf = make_framework_doc("管理费按结算价 3% 收取。")
        rate_rules.import_rate_candidates(conn, pid, sf.file_id)
        rule_id = rate_rules.list_rate_rules(conn, pid)[0]["id"]
        rate_rules.confirm_rate_rule(conn, pid, rule_id, **kwargs)
        return conn, pid, rule_id

    def test_candidate_cannot_apply(self, make_framework_doc):
        conn, pid, sf = make_framework_doc("管理费按 3% 收取。")
        rate_rules.import_rate_candidates(conn, pid, sf.file_id)
        rule_id = rate_rules.list_rate_rules(conn, pid)[0]["id"]
        with pytest.raises(ValueError, match="已确认"):
            rate_rules.apply_rate_rule(conn, pid, rule_id, "1000000")

    def test_deterministic_fee(self, make_framework_doc):
        conn, pid, rule_id = self._confirmed_rule(
            make_framework_doc,
            base_type="upward_settlement_amount",
            base_definition="按对上结算审定金额",
        )
        result = rate_rules.apply_rate_rule(conn, pid, rule_id, "1286500.00")
        assert result["fee"] == "38595.00"
        assert result["detail"]["rate_percent"] == "3"

    def test_cap_and_floor_applied(self, make_framework_doc):
        conn, pid, rule_id = self._confirmed_rule(
            make_framework_doc,
            base_type="custom", base_definition="按对下分包结算额",
            cap="50000", floor="10000",
        )
        under = rate_rules.apply_rate_rule(conn, pid, rule_id, "200000")  # 3% = 6000 → floor
        assert under["fee"] == "10000"
        assert under["detail"]["floor_applied"] == "10000"
        over = rate_rules.apply_rate_rule(conn, pid, rule_id, "3000000")  # 3% = 90000 → cap
        assert over["fee"] == "50000"
        assert over["detail"]["cap_applied"] == "50000"
