"""异常检测集成测试：messy/multi 合成文件端到端触发预期规则。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "synthetic_test_data"))

from generator import make_messy, make_multi_period  # noqa: E402

from costguard.core.anomalies import engine  # noqa: E402
from costguard.core.engine import settlement_io  # noqa: E402


@pytest.fixture()
def project_messy(tmp_path):
    from costguard.core.models import project as pm

    info = pm.create_project("异常-messy", tmp_path / "ws")
    info, conn = pm.open_project(Path(info.workspace_path))
    pdir = Path(info.workspace_path)
    src = pdir.parent / "messy.xlsx"
    make_messy(src, seed=7)
    settlement_io.import_settlement_file(conn, info.project_id, pdir, src)
    yield info, conn
    conn.close()


@pytest.fixture()
def project_multi(tmp_path):
    from costguard.core.models import project as pm

    info = pm.create_project("异常-multi", tmp_path / "ws")
    info, conn = pm.open_project(Path(info.workspace_path))
    pdir = Path(info.workspace_path)
    src = pdir.parent / "multi.xlsx"
    make_multi_period(src, periods=3)
    settlement_io.import_settlement_file(conn, info.project_id, pdir, src)
    yield info, conn
    conn.close()


class TestMessyAnomalies:
    def test_expected_rules_fired(self, project_messy):
        info, conn = project_messy
        findings = engine.run_anomalies(conn, info.project_id)
        fired = {f.rule_id for f in findings}
        expected = {
            "hidden_rows", "hidden_cols",          # 隐藏行列
            "formula_no_cache",                    # 公式无缓存（openpyxl 写入公式无缓存值）
            "negative_quantity",                   # 负数工程量
            "text_number_in_value_col",            # 文本数字/千分位/¥
            "merged_cells_in_data",                # 数据区合并单元格
            "rounding_difference",                 # +0.01 四舍五入差异（low）
            "same_code_diff_name",                 # 名称被改动而编码相同
            "tax_rate_changed",                    # 税率 0.09 → 0.13
        }
        missing = expected - fired
        assert not missing, f"expected rules not fired: {missing}; fired={sorted(fired)}"

    def test_unit_alias_not_flagged(self, project_messy):
        """'m3' 与 '立方米' 是等价单位，不得报 unit_changed。"""
        info, conn = project_messy
        findings = engine.run_anomalies(conn, info.project_id)
        assert not any(f.rule_id == "unit_changed" for f in findings)

    def test_severity_mix(self, project_messy):
        info, conn = project_messy
        findings = engine.run_anomalies(conn, info.project_id)
        summary = engine.anomaly_summary(findings)
        assert summary["total"] >= 10
        assert summary["high"] >= 1 and summary["low"] >= 1

    def test_anomalies_in_db_with_evidence(self, project_messy):
        info, conn = project_messy
        engine.run_anomalies(conn, info.project_id)
        row = conn.execute(
            """SELECT a.message, e.steps_json FROM anomalies a
               JOIN evidence e ON e.id = a.evidence_id LIMIT 1"""
        ).fetchone()
        assert row and row["steps_json"]


class TestMultiAnomalies:
    def test_multi_period_rules(self, project_multi):
        info, conn = project_multi
        findings = engine.run_anomalies(conn, info.project_id)
        fired = {f.rule_id for f in findings}
        # 第3期 C25 单价 465→510（+9.7%）→ price_changed；
        # 名称轻微软/语义相同 → same_code_diff_name（同编码不同名称）或同名不同码不触发，
        # 但"挖沟槽土方 "带空格 → 名称文本不同 → same_code_diff_name
        assert "price_changed" in fired
        # 生成器名称微调不改编码 → 同编码不同名称
        assert "same_code_diff_name" in fired
