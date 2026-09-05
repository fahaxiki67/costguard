"""合同条款提取测试：原文引用必须随身携带。"""

import json

import pytest

from jiadun.core.contracts import docx_parser, extract, run_contract
from jiadun.core.db import migrations

SAMPLE_CONTRACT = """
工程分包合同

发包人：中禾建设集团有限公司
承包人：宏远劳务有限公司

第一条 工程概况
工程名称：某综合楼工程（合成示例）。

第二条 合同价款
本合同价款为固定单价合同，签约合同价 ￥1,286,500.00 元，增值税税率为 9%，价格含税。

第三条 工期
计划开工日期 2026 年 3 月 1 日，工期 180 日历天。

第四条 付款
承包人每月 25 日报送进度款申请，发包人应在收到申请后 28 天内支付已完工程量进度款的 80%。预付款为合同价款的 10%。

第五条 结算
工程竣工验收合格后 60 天内承包人提交竣工结算书，发包人在收到结算书后 90 日内完成结算审核。

第六条 变更与签证
工程变更须经发包人与监理审批后实施，现场签证由发包人、监理、承包人三方共同签认。

第七条 违约责任
承包人工期延误每日历天按合同价款万分之五支付违约金。
""".strip()


@pytest.fixture()
def contract_docx(tmp_path):
    import docx as docx_lib

    d = docx_lib.Document()
    for line in SAMPLE_CONTRACT.splitlines():
        d.add_paragraph(line)
    p = tmp_path / "分包合同.docx"
    d.save(str(p))
    return p


class TestDocxParser:
    def test_paragraphs_extracted(self, contract_docx):
        paras = docx_parser.parse_docx(contract_docx)
        texts = [p["text"] for p in paras]
        assert any("发包人：中禾建设集团有限公司" in t for t in texts)
        assert any("违约" in t for t in texts)


class TestExtract:
    def test_facts_with_quotes(self, contract_docx):
        paras = docx_parser.parse_docx(contract_docx)
        facts = extract.extract_facts(paras)
        by_key = {}
        for f in facts:
            by_key.setdefault(f["fact_key"], []).append(f)
        # 每条事实必须带原文
        for f in facts:
            assert f["quote_text"], "fact must carry quote"
            assert f["location"], "fact must carry location"

        assert any(f["fact_value"] and "中禾建设" in f["fact_value"] for f in by_key.get("employer_party", []))
        amounts = [f["fact_value"] for f in by_key.get("contract_amount", []) if f["fact_value"]]
        assert any("1286500" in a for a in amounts), amounts
        days = [f["fact_value"] for f in by_key.get("payment_clause", []) if f["fact_value"]]
        assert any("28" in d for d in days)
        assert "pricing_method" in by_key
        assert any("固定单价" in (f["fact_value"] or "") for f in by_key["pricing_method"])
        assert "tax_clause" in by_key
        assert "breach_clause" in by_key

    def test_party_facts_require_label_value_adjacency(self):
        """party 字段只接受"标签+分隔符+实体"邻接形态（2026-09-05 真实合同实测）。

        真实 EPC 主合同中每个提及"发包人/承包人"的条款段落都被当作
        party 事实（单份 3532 条候选中 2314 条为 party 噪声），人工复核
        队列被淹没。下列正文句子不得产生 party 事实。
        """
        noise_paragraphs = [
            {"index": 1, "text": "地产产品的精品战略，承包人必须严格执行，按发包人指定的标准设计施工图"},
            {"index": 2, "text": "发包人为了保证开发产品品质，实施集中采购"},
            {"index": 3, "text": "向承包人支付当期结算价及其增值税总额的75%"},
            {"index": 4, "text": "工期天数不一致的，以工期总日历天数为准。除非承包人符合本合同明确约定的工期顺延情形"},
            {"index": 5, "text": "[委托方]（以下简称甲方）：[受托方]（以下简称乙方）"},
        ]
        facts = extract.extract_facts(noise_paragraphs)
        party = [f for f in facts if f["fact_key"] in ("employer_party", "contractor_party")]
        assert party == [], [f["fact_value"] for f in party]

    def test_party_facts_accept_real_label_forms(self):
        """真实合同中的标签形态必须仍然命中：冒号式/括号后缀式/为式。"""
        paragraphs = [
            {"index": 1, "text": "发包人（全称）：河南泷通置业有限责任公司"},
            {"index": 2, "text": "发包人(全称) 河南泷通置业有限责任公司"},
            {"index": 3, "text": "承包人为中国水利水电第五工程局有限公司（联合体牵头人）"},
            {"index": 4, "text": "【发包人】（甲方）：武汉洺悦领江房地产有限公司"},
        ]
        facts = extract.extract_facts(paragraphs)
        party_values = [f["fact_value"] for f in facts
                        if f["fact_key"] in ("employer_party", "contractor_party")]
        assert "河南泷通置业有限责任公司" in party_values
        assert "中国水利水电第五工程局有限公司" in party_values
        assert "武汉洺悦领江房地产有限公司" in party_values

    def test_contract_amount_supports_chinese_capital_numerals(self):
        """合同金额必须支持人民币大写（真实补充协议全部以大写计价）。"""
        paragraphs = [
            {"index": 1,
             "text": "2.3 调整后合同总金额（含增值税）为人民币（大写）叁亿零捌拾叁万柒仟零玖拾元整"},
            {"index": 2, "text": "主合同金额为人民币大写：肆亿捌仟零叁拾陆万元整"},
            {"index": 3, "text": "本补充协议金额为人民币（大写）伍佰万元整（¥5,000,000.00）"},
        ]
        facts = extract.extract_facts(paragraphs)
        values = [f["fact_value"] for f in facts
                  if f["fact_key"] == "contract_amount" and f["fact_value"]]
        assert any(v == "300837090" for v in values), values
        assert any(v == "480360000" for v in values), values
        assert any(v == "5000000" for v in values), values

    def test_contract_amount_keeps_jiao_fen_and_negative_sign(self):
        """真实合同的角分（贰角伍分）与负数（负壹亿…）不得丢失（2026-09-05 实测）。"""
        paragraphs = [
            {"index": 1,
             "text": "不含增值税签约合同价为：人民币（大写）【壹亿捌仟叁佰壹拾玖万玖仟捌佰柒拾叁元贰角伍分】"},
            {"index": 2,
             "text": "2.2 此次补充协议金额为人民币大写：负壹亿柒仟玖佰伍拾贰万柒仟柒佰捌拾捌元整"},
        ]
        facts = extract.extract_facts(paragraphs)
        values = [f["fact_value"] for f in facts
                  if f["fact_key"] == "contract_amount" and f["fact_value"]]
        assert any(v == "183199873.25" for v in values), values
        assert any(v == "-179527788" for v in values), values

    def test_standard_code_numbers_are_not_amounts(self):
        """规范编号（GB50500-2013）不得被当作合同金额（真实噪声实测）。"""
        paragraphs = [
            {"index": 1,
             "text": "按《建设工程工程量清单计价规范》（GB50500-2013）相关规定计入合同价内"},
        ]
        facts = extract.extract_facts(paragraphs)
        values = [f["fact_value"] for f in facts
                  if f["fact_key"] == "contract_amount" and f["fact_value"]]
        assert values == [], values

    def test_chinese_capital_amount_converter_edges(self):
        """大写金额换算的确定性边界（程序计算，不靠 LLM）。"""
        convert = extract._parse_chinese_capital_amount
        assert convert("壹佰元整") == "100"
        assert convert("叁亿零捌拾叁万柒仟零玖拾元整") == "300837090"
        assert convert("肆亿捌仟零叁拾陆万元整") == "480360000"
        assert convert("伍佰万元整") == "5000000"
        assert convert("玖元整") == "9"
        assert convert("拾万元整") == "100000"
        assert convert("壹仟零壹元整") == "1001"
        assert convert("壹亿捌仟叁佰壹拾玖万玖仟捌佰柒拾叁元贰角伍分") == "183199873.25"
        assert convert("负壹亿柒仟玖佰伍拾贰万柒仟柒佰捌拾捌元整") == "-179527788"
        assert convert("玖角") is None  # 只有角没有元，不定标
        # 非法/不完整输入必须返回 None（不猜值）
        assert convert("这不是金额") is None
        assert convert("") is None
        assert convert("壹拾") is None  # 缺少元单位无法定标

    def test_missing_section_not_fabricated(self, tmp_path):
        """没有索赔条款 → 不得编造 claim_clause。"""
        import docx as docx_lib

        d = docx_lib.Document()
        d.add_paragraph("发包人：甲公司")
        d.add_paragraph("承包人：乙公司")
        p = tmp_path / "mini.docx"
        d.save(str(p))
        facts = extract.extract_facts(docx_parser.parse_docx(p))
        keys = {f["fact_key"] for f in facts}
        assert "claim_clause" not in keys
        assert "breach_clause" not in keys
        assert "employer_party" in keys and "contractor_party" in keys


class TestImportAndRisk:
    @pytest.fixture()
    def db(self, tmp_path):
        db_path = tmp_path / "project.db"
        migrations.migrate(db_path, tmp_path / "backups")
        conn = migrations.connect(db_path)
        with conn:
            pid = conn.execute(
                "INSERT INTO projects(name, schema_version, workspace_path, created_at) VALUES ('t',1,'/t','2026')"
            ).lastrowid
        yield conn, pid, tmp_path
        conn.close()

    def test_import_contract_full(self, db, tmp_path):
        conn, pid, pdir = db
        src = tmp_path / "分包合同.docx"
        import docx as docx_lib

        d = docx_lib.Document()
        for line in SAMPLE_CONTRACT.splitlines():
            d.add_paragraph(line)
        d.save(str(src))
        doc_id = extract.import_contract(conn, pid, pdir, src, doc_type="subcontract")
        facts = conn.execute("SELECT fact_key, fact_value, quote_text, evidence_id FROM contract_facts WHERE doc_id=?", (doc_id,)).fetchall()
        assert facts
        assert all(f["quote_text"] and f["evidence_id"] for f in facts)
        # 全量条款 → 无高风险缺失（付款/结算条款都在）
        risks = extract.contract_risks(conn, pid)
        assert not any(r["severity"] == "high" for r in risks), risks

    def test_risk_for_thin_contract(self, db, tmp_path):
        conn, pid, pdir = db
        src = tmp_path / "简短协议.docx"
        import docx as docx_lib

        d = docx_lib.Document()
        d.add_paragraph("发包人：甲公司")
        d.add_paragraph("承包人：乙公司")
        d.save(str(src))
        extract.import_contract(conn, pid, pdir, src)
        risks = extract.contract_risks(conn, pid)
        keys = {r["fact_key"] for r in risks}
        assert {"payment_clause", "settlement_clause", "duration", "contract_amount"} <= keys
        n = extract.persist_risks(conn, pid, risks)
        assert n == len(risks)
        active = run_contract.get_current_contract(conn, pid)
        assert active is not None
        current_scope, scope_params = run_contract.current_scope(conn, pid, "a")
        current_risks = conn.execute(
            f"SELECT COUNT(*) FROM anomalies a WHERE a.project_id=? AND a.rule_id='contract_risk' AND {current_scope}",
            (pid, *scope_params),
        ).fetchone()[0]
        assert current_risks == n
        assert conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE project_id=? AND kind='contract_risk' "
            "AND run_id=?", (pid, active.run_id)
        ).fetchone()[0] == n
        ev = conn.execute("SELECT COUNT(*) c FROM evidence WHERE project_id=?", (pid,)).fetchone()["c"]
        assert ev >= n

        first = conn.execute(
            "SELECT id, evidence_id FROM anomalies WHERE project_id=? AND rule_id='contract_risk' "
            "ORDER BY id LIMIT 1",
            (pid,),
        ).fetchone()
        assert first is not None
        assert extract.persist_risks(conn, pid, []) == 0
        historical = conn.execute(
            "SELECT status, lifecycle_status FROM anomalies WHERE id=?", (first["id"],)
        ).fetchone()
        assert tuple(historical) == ("stale", "historical")
        assert conn.execute(
            "SELECT scope FROM evidence WHERE id=?", (first["evidence_id"],)
        ).fetchone()["scope"] == "historical"
        assert conn.execute(
            "SELECT COUNT(*) FROM finding_status_events WHERE anomaly_id=? AND after_status='historical'",
            (first["id"],),
        ).fetchone()[0] == 1
        assert extract.persist_risks(conn, pid, risks) == n
        current = conn.execute(
            "SELECT repeat_history_json FROM anomalies WHERE project_id=? AND rule_id='contract_risk' "
            "AND lifecycle_status='new' ORDER BY id DESC LIMIT 1",
            (pid,),
        ).fetchone()
        assert current is not None and json.loads(current["repeat_history_json"])

    def test_txt_contract(self, db, tmp_path):
        conn, pid, pdir = db
        src = tmp_path / "会议纪要.txt"
        src.write_text("发包人：甲公司\n支付期限：验收后 30 天内付款\n", encoding="utf-8")
        doc_id = extract.import_contract(conn, pid, pdir, src)
        keys = {r["fact_key"] for r in conn.execute("SELECT fact_key FROM contract_facts WHERE doc_id=?", (doc_id,))}
        assert "payment_clause" in keys

    def test_anomaly_rerun_does_not_hide_current_contract_risks(self, db, tmp_path):
        """通用异常快照重跑不能把合同风险误历史化。"""
        from jiadun.core.anomalies import engine as anomaly_engine

        conn, pid, pdir = db
        src = tmp_path / "简短协议-异常边界.docx"
        import docx as docx_lib

        document = docx_lib.Document()
        document.add_paragraph("发包人：甲公司")
        document.add_paragraph("承包人：乙公司")
        document.save(str(src))
        extract.import_contract(conn, pid, pdir, src)
        risks = extract.contract_risks(conn, pid)
        assert extract.persist_risks(conn, pid, risks) == len(risks)
        before = conn.execute(
            "SELECT COUNT(*) FROM anomalies WHERE project_id=? AND rule_id='contract_risk' "
            "AND lifecycle_status<>'historical'",
            (pid,),
        ).fetchone()[0]
        assert before == len(risks)

        anomaly_engine.run_anomalies(conn, pid, rules=[])

        current_scope, scope_params = run_contract.current_scope(conn, pid, "a")
        after = conn.execute(
            f"SELECT COUNT(*) FROM anomalies a WHERE a.project_id=? "
            f"AND a.rule_id='contract_risk' AND {current_scope}",
            (pid, *scope_params),
        ).fetchone()[0]
        assert after == before
