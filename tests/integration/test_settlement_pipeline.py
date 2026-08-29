"""Phase 2 端到端集成测试：导入→解析→识别→抽取→落库，断言与 ground truth 一致。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "synthetic_test_data"))

from decimal import Decimal

from generator import STANDARD_ITEMS, make_clean, make_messy, make_multi_period  # noqa: E402

from costguard.core.engine import settlement_io  # noqa: E402

D = Decimal


@pytest.fixture()
def project(tmp_path):
    from costguard.core.models import project as pm

    info = pm.create_project("集成-结算", tmp_path / "ws")
    info, conn = pm.open_project(Path(info.workspace_path))
    yield Path(info.workspace_path), info, conn
    conn.close()


class TestCleanPipeline:
    def test_clean_end_to_end(self, project):
        pdir, info, conn = project
        src = pdir.parent / "clean_1期.xlsx"
        gt = make_clean(src)
        report = settlement_io.import_settlement_file(conn, info.project_id, pdir, src)
        assert report.status == "ok", report.message
        assert report.period_no == 1  # 文件名"第1期"
        srep = report.sheets[0]
        assert srep.status == "parsed"
        assert srep.n_items == len(STANDARD_ITEMS)

        rows = conn.execute(
            "SELECT code, name, quantity, unit_price, amount, tax_rate, flags_json FROM line_items"
            " WHERE period_id=? ORDER BY id",
            (report.period_id,),
        ).fetchall()
        detail = [r for r in rows if '"subtotal": true' not in r["flags_json"]]
        assert len(rows) == len(STANDARD_ITEMS) + 1  # 6 明细 + 1 小计（打标保留）
        assert len(detail) == len(STANDARD_ITEMS)
        for row, item in zip(detail, gt.items, strict=True):
            assert row["code"] == item.code
            assert row["name"] == item.name
            assert D(row["quantity"]) == D(item.quantity)
            assert D(row["unit_price"]) == D(item.unit_price)
            assert D(row["amount"]) == D(item.amount)
            assert D(row["tax_rate"]) == D(item.tax_rate)

    def test_evidence_preserved(self, project):
        pdir, info, conn = project
        src = pdir.parent / "clean_1期.xlsx"
        make_clean(src)
        report = settlement_io.import_settlement_file(conn, info.project_id, pdir, src)
        row = conn.execute(
            "SELECT qty_evid FROM line_items WHERE period_id=? ORDER BY id LIMIT 1", (report.period_id,)
        ).fetchone()
        import json

        ev = json.loads(row["qty_evid"])
        assert set(ev) == {"value", "raw", "row", "col"}
        assert ev["row"] == 2 and ev["col"] == 5  # 表头1行，数据从第2行


class TestMessyPipeline:
    def test_messy_values_normalize(self, project):
        pdir, info, conn = project
        src = pdir.parent / "messy.xlsx"
        make_messy(src, seed=7)
        report = settlement_io.import_settlement_file(conn, info.project_id, pdir, src)
        parsed = [s for s in report.sheets if s.status in ("parsed", "needs_review")]
        assert parsed, f"no sheet parsed: {[ (s.sheet_name, s.status) for s in report.sheets ]}"

        rows = conn.execute(
            "SELECT name, quantity, unit_price, amount, flags_json FROM line_items WHERE period_id=?",
            (report.period_id,),
        ).fetchall()

        # 至少验证：负数行正确规范化
        negatives = [r for r in rows if r["quantity"] and r["quantity"].startswith("-")]
        assert negatives, "parenthesized negative must normalize to minus"

    def test_subtotal_flagged_not_counted(self, project):
        pdir, info, conn = project
        src = pdir.parent / "clean_1期.xlsx"
        make_clean(src)
        report = settlement_io.import_settlement_file(conn, info.project_id, pdir, src)
        subs = conn.execute(
            "SELECT COUNT(*) AS c FROM line_items WHERE period_id=? AND flags_json LIKE '%\"subtotal\": true%'",
            (report.period_id,),
        ).fetchone()
        assert subs["c"] == 1  # 小计行保留但打标


class TestMultiPeriodPipeline:
    def test_three_periods_imported(self, project):
        """一个工作簿 3 个期次 Sheet → 应生成 3 个期次（v2 模型）。"""
        pdir, info, conn = project
        src = pdir.parent / "multi.xlsx"
        make_multi_period(src, periods=3)
        report = settlement_io.import_settlement_file(conn, info.project_id, pdir, src)
        assert report.status == "ok"
        periods = conn.execute(
            "SELECT period_no FROM settlement_periods WHERE project_id=? ORDER BY period_no",
            (info.project_id,),
        ).fetchall()
        assert [p["period_no"] for p in periods] == [1, 2, 3]
        counts = conn.execute("SELECT COUNT(*) AS c FROM line_items").fetchone()
        assert counts["c"] > 15

    def test_filename_guesses_period(self, project):
        pdir, info, conn = project
        src = pdir.parent / "结算第十期.xlsx"
        make_clean(src)
        report = settlement_io.import_settlement_file(conn, info.project_id, pdir, src)
        assert report.period_no == 10

    def test_same_file_reimport_reuses_copy(self, project):
        pdir, info, conn = project
        src = pdir.parent / "第2期.xlsx"
        make_clean(src)
        r1 = settlement_io.import_settlement_file(conn, info.project_id, pdir, src)
        r2 = settlement_io.import_settlement_file(conn, info.project_id, pdir, src)
        assert r1.file_id == r2.file_id
        assert r2.period_no == 2  # 文件名第2期 → 第2期
