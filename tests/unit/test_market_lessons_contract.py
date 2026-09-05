"""市场实测教训金样本的单元级锁定（2026-09 三轮实测沉淀）。

与 tests/golden/cases.json 的 market_lessons_v1 案例互补：黄金回归锁
全链路指标，本文件锁具体事实值与门控行为，任何破坏大写金额换算、
当事人邻接守卫或规范编号防误报的改动都会在这里秒级现形。

事实清单已逐条对照 examples/demo/演示-市场实测教训-合同摘录-合成.docx
原文人工核对（见 scripts/generate_demo_data.py MARKET_PARAGRAPHS）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from jiadun.core.contracts import extract
from jiadun.core.db import migrations

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKET_DOCX = REPO_ROOT / "examples" / "demo" / "演示-市场实测教训-合同摘录-合成.docx"

# 期望事实（fact_key, fact_value, 置信度）：与原文条款一一对应。
EXPECTED_FACTS = [
    # 1.1 人民币大写（含角分）→ Decimal 精确换算（市场实测值）
    ("contract_amount", "183199873.25", 0.9),
    # 1.2 人民币大写（负号）→ Decimal 精确换算（市场实测值）
    ("contract_amount", "-179527788", 0.9),
    # 2.1 冒号形态
    ("employer_party", "示例置业发展有限公司", 0.9),
    # 2.2 括号后缀形态
    ("contractor_party", "示例建设集团股份有限公司", 0.9),
    # 标题「三、审核与支付时限」命中支付触发词：无值低置信候选
    ("payment_clause", None, 0.4),
    # 3.1 时限候选取首个天数（已知候选级行为，人工复核门控兜底）
    ("payment_clause", "7 日内", 0.7),
    # 4.2 支付句无量纲天数：无值低置信候选
    ("payment_clause", None, 0.4),
]


@pytest.fixture()
def imported(tmp_path: Path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        project_id = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES(?,?,?,?)""",
            ("市场教训金样本", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
    copy = tmp_path / MARKET_DOCX.name
    shutil.copy2(MARKET_DOCX, copy)
    extract.import_contract(conn, project_id, tmp_path, copy)
    yield conn, int(project_id)
    conn.close()


def test_market_lesson_facts_exact_values(imported):
    conn, project_id = imported
    rows = conn.execute(
        """SELECT fact_key, fact_value, confidence, quote_text, review_status
           FROM contract_facts WHERE doc_id IN
             (SELECT id FROM contract_docs WHERE project_id=?)
           ORDER BY id""",
        (project_id,),
    ).fetchall()
    assert len(rows) == len(EXPECTED_FACTS), (
        f"事实数 {len(rows)} != 期望 {len(EXPECTED_FACTS)}："
        + "; ".join(f"{r['fact_key']}={r['fact_value']}" for r in rows)
    )
    for row, (key, value, confidence) in zip(rows, EXPECTED_FACTS, strict=True):
        assert row["fact_key"] == key
        assert row["fact_value"] == value, (
            f"{key} 期望 {value!r}，实际 {row['fact_value']!r}（{row['quote_text'][:40]}）"
        )
        assert row["confidence"] == pytest.approx(confidence)


def test_market_lesson_facts_stay_candidates(imported):
    """全部事实保持 candidate：自动抽取永不直接确认（宪章原则 7/8）。"""
    conn, project_id = imported
    statuses = {
        row["review_status"]
        for row in conn.execute(
            "SELECT review_status FROM contract_facts WHERE doc_id IN"
            " (SELECT id FROM contract_docs WHERE project_id=?)",
            (project_id,),
        )
    }
    assert statuses == {"candidate"}


def test_no_fact_from_noise_lines(imported):
    """噪声行不得产生事实：规范编号、项目部、监理为式、全国统一编码。"""
    conn, project_id = imported
    quotes = [
        row["quote_text"]
        for row in conn.execute(
            "SELECT quote_text FROM contract_facts WHERE doc_id IN"
            " (SELECT id FROM contract_docs WHERE project_id=?)",
            (project_id,),
        )
    ]
    noise_markers = ("GB50500", "项目部", "监理单位为", "十二位")
    for marker in noise_markers:
        assert not any(marker in q for q in quotes), f"噪声行 {marker} 产生了候选"


def test_market_docx_registered_in_manifest():
    """演示清单与 SHA256SUMS 必须收录金样本文件（打包/黄金输入前提）。"""
    import hashlib
    import json

    manifest = json.loads(
        (REPO_ROOT / "examples" / "demo" / "manifest.json").read_text(encoding="utf-8")
    )
    names = {entry["file_name"] for entry in manifest["files"]}
    assert MARKET_DOCX.name in names
    digest = hashlib.sha256(MARKET_DOCX.read_bytes()).hexdigest()
    sums = (REPO_ROOT / "examples" / "demo" / "SHA256SUMS").read_text(encoding="utf-8")
    assert digest in sums
