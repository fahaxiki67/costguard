"""正式分析入口与 A/B 同一物理行集语义的回归测试。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_run_analysis_uses_one_stable_stage_order(monkeypatch):
    """一次用户分析必须固定先跑 A/B/C，再跑异常规则。"""
    from jiadun.core import analysis

    calls: list[str] = []
    crosscheck_result = SimpleNamespace(
        verification_level="sufficient",
        ab_row_set_status="same_row_set",
        path_independence_level="source_independent_raw_scan",
    )

    def fake_crosscheck(_conn, project_id):
        calls.append(f"crosscheck:{project_id}")
        return [crosscheck_result]

    def fake_anomalies(_conn, project_id):
        calls.append(f"anomalies:{project_id}")
        return []

    monkeypatch.setattr(analysis.crosscheck, "run_crosscheck_project", fake_crosscheck)
    monkeypatch.setattr(analysis.anomaly_engine, "run_anomalies", fake_anomalies)

    result = analysis.run_analysis(object(), 7)

    assert calls == ["crosscheck:7", "anomalies:7"]
    assert result.crosscheck_results == [crosscheck_result]
    assert result.anomaly_findings == []
    assert result.stage_order == ("crosscheck", "anomalies")
    assert result.status == "completed"


def test_run_analysis_is_fail_closed_when_anomaly_stage_fails(monkeypatch):
    """异常覆盖未完成时，编排结果不得给出可完成结论。"""
    from jiadun.core import analysis

    calls: list[str] = []

    def fake_crosscheck(_conn, _project_id):
        calls.append("crosscheck")
        return [SimpleNamespace(verification_level="sufficient")]

    def broken_anomalies(_conn, _project_id):
        calls.append("anomalies")
        raise RuntimeError("synthetic anomaly failure")

    monkeypatch.setattr(analysis.crosscheck, "run_crosscheck_project", fake_crosscheck)
    monkeypatch.setattr(analysis.anomaly_engine, "run_anomalies", broken_anomalies)

    result = analysis.run_analysis(object(), 8)

    assert calls == ["crosscheck", "anomalies"]
    assert result.status == "failed"
    assert result.conclusion == "cannot_conclude"
    assert result.failed_stage == "anomalies"
    assert "synthetic anomaly failure" in (result.error or "")


def test_run_analysis_is_fail_closed_when_anomaly_engine_reports_rule_failure(monkeypatch):
    """异常引擎内部捕获的规则失败也不能被正式入口当成成功。"""
    from jiadun.core import analysis

    monkeypatch.setattr(
        analysis.crosscheck,
        "run_crosscheck_project",
        lambda _conn, _project_id: [SimpleNamespace(verification_level="sufficient")],
    )
    monkeypatch.setattr(
        analysis.anomaly_engine,
        "run_anomalies",
        lambda _conn, _project_id: [
            SimpleNamespace(rule_id="rule_error_missing_amount", detection_mode="technical_failure")
        ],
    )

    result = analysis.run_analysis(object(), 10)

    assert result.status == "failed"
    assert result.conclusion == "cannot_conclude"
    assert result.failed_stage == "anomalies"
    assert "覆盖不完整" in (result.error or "")


def test_run_analysis_does_not_skip_anomaly_stage_when_crosscheck_fails(monkeypatch):
    """校核阶段异常不能让正式入口静默跳过后续检测；整体仍须失败关闭。"""
    from jiadun.core import analysis

    calls: list[str] = []

    def broken_crosscheck(_conn, _project_id):
        calls.append("crosscheck")
        raise RuntimeError("synthetic crosscheck failure")

    def fake_anomalies(_conn, _project_id):
        calls.append("anomalies")
        return []

    monkeypatch.setattr(analysis.crosscheck, "run_crosscheck_project", broken_crosscheck)
    monkeypatch.setattr(analysis.anomaly_engine, "run_anomalies", fake_anomalies)

    result = analysis.run_analysis(object(), 9)

    assert calls == ["crosscheck", "anomalies"]
    assert result.status == "failed"
    assert result.conclusion == "cannot_conclude"
    assert result.failed_stage == "crosscheck"


def test_run_analysis_materializes_period_totals_for_fresh_import(tmp_path):
    """新导入项目首次正式分析前应先物化聚合行，不能卡在缺少回写行。"""
    import openpyxl

    from jiadun.core import analysis
    from jiadun.core.engine import settlement_io
    from jiadun.core.models import project as project_model

    info = project_model.create_project("首次正式分析", tmp_path / "ws")
    info, conn = project_model.open_project(info.workspace_path)
    source = tmp_path / "第1期.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "第1期"
    sheet.append(["清单编码", "清单名称", "计量单位", "工程量", "综合单价", "合价"])
    sheet.append(["A-001", "平整场地", "m2", 2, 3, 6])
    sheet.append(["合计", "", "", "", "", 6])
    book.save(source)
    try:
        report = settlement_io.import_settlement_file(
            conn,
            info.project_id,
            info.workspace_path,
            source,
            direction="upward",
        )
        assert report.status in {"ok", "partial"}
        assert conn.execute("SELECT COUNT(*) FROM period_totals").fetchone()[0] == 0

        result = analysis.run_analysis(conn, info.project_id)

        assert result.crosscheck_results, result.errors
        assert not any("period_totals 缺少当前项目" in error for error in result.errors)
        assert conn.execute("SELECT COUNT(*) FROM period_totals").fetchone()[0] > 0
    finally:
        conn.close()


@pytest.mark.parametrize("status", ["same_row_set"])
def test_same_row_set_has_non_green_semantic_warning(status):
    from jiadun.core import analysis

    assert analysis.same_row_set_warning(status) == (
        "读取/计算过程不同但参与行集相同，不能证明行集完整性"
    )


def test_workbench_combines_independent_scan_with_same_row_set_warning():
    from jiadun.ui.workbench import _crosscheck_independence_text

    text = _crosscheck_independence_text(
        SimpleNamespace(
            path_independence_level="source_independent_raw_scan",
            ab_row_set_status="same_row_set",
        )
    )

    assert text == (
        "已完成原始网格独立扫描（读取/计算过程不同但参与行集相同，不能证明行集完整性）"
    )


def test_export_notice_combines_path_and_same_row_set_warning(monkeypatch):
    from jiadun.core.export import excel_export

    class FakeConn:
        def execute(self, sql, _params=()):
            if "FROM crosscheck_results" in sql:
                return _Cursor([
                    {
                        "period_id": 1,
                        "evidence_id": 12,
                        "ab_row_set_status": "same_row_set",
                    }
                ])
            if "SELECT steps_json FROM evidence" in sql:
                return _Cursor({"steps_json": '[{"path_independence_level":"source_independent_raw_scan", "ab_independence_level":"shared_extractor"}]'})
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Cursor:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows if isinstance(self.rows, list) else [self.rows]

        def fetchone(self):
            return self.rows[0] if isinstance(self.rows, list) else self.rows

    monkeypatch.setattr(
        excel_export.run_contract,
        "current_scope",
        lambda *_args, **_kwargs: ("1=1", []),
    )

    text = excel_export._current_crosscheck_path_notice(FakeConn(), 1)

    assert "A 使用清洗明细，B 使用 raw_cells 原始网格独立扫描" in text
    assert "读取/计算过程不同但参与行集相同，不能证明行集完整性" in text
    assert "覆盖证明本身不构成独立复核" in text
