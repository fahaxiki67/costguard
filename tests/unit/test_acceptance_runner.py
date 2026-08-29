"""验收执行器非破坏性 + 表单路由（监督第五/六/七轮，先红后绿）。

1) runner 必须使用时间戳 run 目录（微秒唯一，同秒碰撞安全）：重复运行不得
   覆盖/删除既有 run 结果（基线保留）；支持可恢复续跑且不破坏未完成现场；
2) 键值对表单（支付审批单类）路由为 non_settlement_form 待人工状态：
   绝不写入 settlement/contract 模型；候选仅存通用 evidence 表；
   纯表单导入归类为 partial/needs_manual_review，不进入结算计算与导出；
3) steps.compute 必须反映真实计算（有 groups/checks/复算结果），不得用
   解析成功代替。
"""
import json
from pathlib import Path

import pytest


def _make_form_workbook(path: Path) -> None:
    """脱敏复现 R08 结构：键值对支付审批单（无列式表头、大量合并）。"""
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("VYSMSZ")
    ws.cell(row=1, column=1, value=" ")  # 近空 sheet
    ws2 = wb.create_sheet("支付审批单")
    rows = [
        ("××公司（脱敏示例）",),
        ("资金支付审批单",),
        ("申请单位名称：", None, "甲单位（脱敏）"),  # 键带冒号 + 值在右侧第2列（间隔布局）
        ("合同名称", "某工程（脱敏）"),
        ("合同编号", "HZ-001"),
        ("收款方信息", "户名", "乙单位（脱敏）"),
        ("", "开户行", "某银行"),
        ("本次申请支付金额", None, "1,000,000.00"),  # 金额在 C 列（col3）
    ]
    for r, row in enumerate(rows, start=1):
        for c, v in enumerate(row, start=1):
            if v is not None:
                ws2.cell(row=r, column=c, value=v)
    ws2.merge_cells("A1:J1")
    ws2.merge_cells("A2:J2")
    ws2.merge_cells("A6:A7")
    wb.save(path)


@pytest.fixture()
def runner_env(tmp_path, monkeypatch):
    """给 runner 注入临时 base 目录与 13 行假 corpus（脱敏）。"""
    import scripts.real_acceptance_run as runner

    base = tmp_path / "real_acceptance"
    corpus = base / "corpus"
    corpus.mkdir(parents=True)
    rows = []
    for i in range(1, 14):
        src = corpus / f"T{i:02d}_sample.xlsx"
        _make_form_workbook(src) if i == 1 else _make_simple(src)
        rows.append({
            "test_id": f"T{i:02d}",
            "source_path": f"orig/T{i:02d}",
            "copy_path": f"corpus/T{i:02d}_sample.xlsx",
            "sha256": runner.sha256_of(src),
            "purpose": "脱敏回归",
        })
    manifest = base / "manifest.csv"
    import csv

    with open(manifest, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["test_id", "source_path", "copy_path", "sha256", "purpose"])
        w.writeheader()
        w.writerows(rows)
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "WORK", base / "work")
    monkeypatch.setattr(runner, "MANIFEST", manifest)
    return runner, base


def _make_simple(src: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "第1期"
    for c, v in enumerate(["清单编码", "清单名称", "单位", "工程量", "综合单价", "合价"], start=1):
        ws.cell(row=1, column=c, value=v)
    ws.cell(row=2, column=1, value="K1")
    ws.cell(row=2, column=2, value="某清单")
    ws.cell(row=2, column=3, value="m3")
    ws.cell(row=2, column=4, value=10)
    ws.cell(row=2, column=5, value=10)
    ws.cell(row=2, column=6, value=100)
    wb.save(src)


class TestRunnerNonDestructive:
    def test_timestamped_runs_preserve_previous(self, runner_env):
        """两次运行产生两个 run 目录：第一次结果必须原样保留（基线不覆盖）。"""
        runner, base = runner_env
        runner.main()
        runs = sorted((base / "work").glob("run_*"))
        assert len(runs) == 1, f"首次运行应产生 1 个 run 目录: {runs}"
        first_result = runs[0] / "acceptance_results.json"
        first_bytes = first_result.read_bytes()

        runner.main()  # 第二次运行
        runs = sorted((base / "work").glob("run_*"))
        assert len(runs) == 2, "第二次运行必须新建 run 目录"
        # 第一次 run 的结果逐字节保留
        assert runs[0].joinpath("acceptance_results.json").read_bytes() == first_bytes
        # 每轮都包含全部 13 个 test_id 项目
        projects = [d.name for d in runs[1].iterdir() if d.is_dir() and d.name.startswith("验收-")]
        assert len(projects) == 13

    def test_resume_skips_completed(self, runner_env):
        """可恢复重跑：同 run 目录续跑时跳过已完成 test_id。"""
        runner, base = runner_env
        runner.main()
        runs = sorted((base / "work").glob("run_*"))
        first = runs[-1]
        done = {d.name for d in first.iterdir() if d.is_dir() and d.name.startswith("验收-")}
        # 续跑同一 run 目录：全部已完成 → 不重复建项目
        runner.main(run_dir=first)
        after = {d.name for d in first.iterdir() if d.is_dir() and d.name.startswith("验收-")}
        assert after == done, "续跑不得重建已完成项目"


class TestFormRouting:
    def test_form_sheet_routed_out_of_settlement_model(self, tmp_path):
        """键值对支付表单必须路由为 non_settlement_form：

        - 不生成 settlement_period / line_items / period_totals（不得污染结算模型）；
        - 保留原 Sheet 与单元格（保真层不变）；
        - 事实候选进既有 contract_facts（含原文、单元格位置、证据ID、待人工状态）；
        - 审计留痕。
        """
        from costguard.core.engine import settlement_io
        from costguard.core.models import project as pm

        src = tmp_path / "form.xlsx"
        _make_form_workbook(src)
        info = pm.create_project("表单路由", tmp_path / "ws")
        info, conn = pm.open_project(Path(info.workspace_path))
        try:
            report = settlement_io.import_settlement_file(
                conn, info.project_id, Path(info.workspace_path), src)
            form_sheet = next(s for s in report.sheets if s.sheet_name == "支付审批单")
            assert form_sheet.status == "non_settlement_form", \
                f"表单 sheet 应路由为 non_settlement_form，得到 {form_sheet.status}"
            assert any("人工" in n or "表单" in n for n in form_sheet.notes), \
                f"必须给出可恢复的人工复核提示: {form_sheet.notes}"

            # 结算与合同模型全部零污染（contract_docs/facts 是合同模块专用，
            # 表单路由不得写入，否则 contract_risks 会产生虚假合同风险）
            for table in ("settlement_periods", "line_items", "period_totals",
                          "contract_docs", "contract_facts"):
                c = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
                assert c == 0, f"{table} 被表单路由污染（{c} 行）"

            # 保真层保留原 Sheet
            assert conn.execute(
                "SELECT COUNT(*) c FROM raw_sheets WHERE sheet_name='支付审批单'").fetchone()["c"] == 1
            assert conn.execute(
                "SELECT COUNT(*) c FROM raw_cells WHERE row=8 AND col=3").fetchone()["c"] == 1

            # 候选仅存通用证据表：带行列、原文、证据ID、待人工确认
            import json as _json

            evs = conn.execute(
                "SELECT id, kind, summary, sources_json FROM evidence WHERE kind='form_field_candidate'"
            ).fetchall()
            assert evs, "表单键值对应作为待人工确认的证据候选"
            # 金额候选必须用真实值列：金额在 C 列 → location 精确为 行8列3
            amt = next(e for e in evs if "支付金额" in e["summary"])
            assert "1,000,000.00" in amt["summary"], amt["summary"]
            amt_src = _json.loads(amt["sources_json"])[0]
            assert amt_src["location"] == "行8列3", \
                f"金额位置必须为真实值列 行8列3，得到 {amt_src['location']}"
            # 带冒号键（申请单位名称：）+ 右值间隔布局 → 候选不得遗漏，位置为真实值列 行3列3
            unit = next(e for e in evs if "申请单位名称" in e["summary"])
            assert "甲单位（脱敏）" in unit["summary"]
            assert _json.loads(unit["sources_json"])[0]["location"] == "行3列3"
            # 可反向定位：evidence 的行列必须对应 raw_cells 中的非空原格
            sheet_id = conn.execute(
                "SELECT id FROM raw_sheets WHERE sheet_name='支付审批单'").fetchone()["id"]
            for e in evs:
                src = _json.loads(e["sources_json"])[0]
                assert src["location"].startswith("行") and src["quote"], \
                    "每条候选必须带行列位置与原文"
                assert "待人工确认" in e["summary"]
                row_no = int(src["location"][1:src["location"].index("列")])
                col_no = int(src["location"][src["location"].index("列") + 1:])
                cell = conn.execute(
                    "SELECT raw_value FROM raw_cells WHERE sheet_id=? AND row=? AND col=?",
                    (sheet_id, row_no, col_no)).fetchone()
                assert cell and (cell["raw_value"] or "").strip(), \
                    f"证据位置不可反向定位: {src['location']}"

            # 回归：表单导入不得产生虚假合同风险
            from costguard.core.contracts import extract as contract_extract

            assert contract_extract.contract_risks(conn, info.project_id) == []

            # 审计留痕
            from costguard.core.evidence import audit as audit_log

            entries = audit_log.history_for(conn, info.project_id)
            assert any("form" in e.action or "表单" in e.reason for e in entries)
        finally:
            conn.close()

    def test_form_like_detection_hint(self, tmp_path):
        """键值对表单结构必须给出可恢复诊断提示（而非裸 no_header）。"""
        from costguard.core.parsing.excel_parser import parse_file
        from costguard.core.parsing.header_detect import detect_form_like

        src = tmp_path / "form.xlsx"
        _make_form_workbook(src)
        result = parse_file(src, "xlsx")
        ws2 = next(s for s in result.sheets if s.sheet_name == "支付审批单")
        cells = {(c.row, c.col): (c.raw_value or "") for c in ws2.cells}
        assert detect_form_like(cells, ws2.merged_ranges) is True
        plain = next(s for s in result.sheets if s.sheet_name == "VYSMSZ")
        plain_cells = {(c.row, c.col): (c.raw_value or "") for c in plain.cells}
        assert detect_form_like(plain_cells, plain.merged_ranges) is False

class TestPureFormProjectHandling:
    def test_pure_form_project_partial_and_skips_settlement_pipeline(self, runner_env, monkeypatch):
        """纯表单项目：status=partial（非 ok/failed），不进计算/异常/匹配/导出，
        steps 单列 non_settlement_form_needs_manual_review，full_pipeline=False。"""
        runner, base = runner_env
        # T01 换成纯表单，其余 simple 清单
        manifest_t01 = base / "corpus" / "T01_sample.xlsx"
        _make_form_workbook(manifest_t01)
        import csv

        rows = []
        for i in range(1, 14):
            src = base / "corpus" / f"T{i:02d}_sample.xlsx"
            rows.append({
                "test_id": f"T{i:02d}", "source_path": f"orig/T{i:02d}",
                "copy_path": f"corpus/T{i:02d}_sample.xlsx",
                "sha256": runner.sha256_of(src), "purpose": "脱敏回归",
            })
        with open(base / "manifest.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["test_id", "source_path", "copy_path", "sha256", "purpose"])
            w.writeheader()
            w.writerows(rows)

        runner.main()
        runs = sorted((base / "work").glob("run_*"))
        t01 = json.loads((runs[-1] / "done" / "T01.json").read_text(encoding="utf-8"))
        steps = t01.get("steps", {})
        assert steps.get("non_settlement_form_needs_manual_review") is True, \
            f"纯表单必须单列 needs_manual_review: {steps}"
        assert steps.get("full_pipeline") is False
        for k in ("compute", "anomalies", "matches", "excel", "word"):
            assert steps.get(k) is False, f"纯表单 steps.{k} 必须为 False: {steps}"

        # ImportReport 状态：纯表单 = partial 且 needs_manual_review（非 ok 非普通 failed）
        rec = t01
        sp = rec.get("settlement_parse") or {}
        assert sp.get("status") == "partial", f"纯表单导入状态应为 partial: {sp}"
        assert sp.get("needs_manual_review") is True


class TestInterruptSafety:
    def test_interrupted_site_preserved_on_rerun(self, runner_env):
        """中断续跑不得删除未完成现场：旧项目目录保留，新目录带序号生成。"""
        runner, base = runner_env
        runner.main()
        runs = sorted((base / "work").glob("run_*"))
        first = runs[-1]
        # 模拟中断：T13 无 done marker，且项目目录留有现场标记
        (first / "done" / "T13.json").unlink(missing_ok=True)
        site = first / "验收-T13"
        site.mkdir(exist_ok=True)
        (site / "interrupted.marker").write_text("现场")
        # 删除另一个 test 的 marker 模拟两个未完成
        (first / "done" / "T12.json").unlink(missing_ok=True)

        runner.main(run_dir=first)  # 续跑
        # T13 旧现场必须保留（不得 rmtree）
        assert (site / "interrupted.marker").exists(), "中断现场被删除"
        # 重跑生成新目录（带序号），不覆盖旧目录
        new_dirs = sorted(first.glob("验收-T13*"))
        assert len(new_dirs) >= 2 and new_dirs[-1].name != "验收-T13"


class TestRunDirUniqueness:
    def test_same_second_runs_both_kept(self, runner_env):
        """同一秒连续两次运行：两个 run 目录都必须保留（微秒/唯一后缀）。"""
        runner, base = runner_env
        runner.main()
        runner.main()
        runs = sorted((base / "work").glob("run_*"))
        assert len(runs) == 2, f"同秒两次运行应产生 2 个 run 目录: {runs}"


class TestComputeTruthfulness:
    def test_compute_false_when_all_computation_fails(self, runner_env, monkeypatch):
        """steps.compute 必须反映真实计算：groups 与 checks 全部失败时 compute=False
        （不得用解析成功代替）。"""
        runner, base = runner_env
        from costguard.core.engine import aggregate as agg_mod
        from costguard.core.engine import crosscheck as cc_mod

        def boom(*a, **k):
            raise RuntimeError("computation broken")
        monkeypatch.setattr(agg_mod, "aggregate_project", boom)
        monkeypatch.setattr(cc_mod, "run_crosscheck", boom)
        runner.main()
        runs = sorted((base / "work").glob("run_*"))
        t02 = json.loads((runs[-1] / "done" / "T02.json").read_text(encoding="utf-8"))
        steps = t02.get("steps", {})
        assert steps.get("compute") is False, \
            f"复算失败时 compute 必须为 False: {steps}（不得用解析成功代替）"
