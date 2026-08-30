"""演示数据（examples/demo）验收测试。

锁定三件事：
1. 确定性——重新生成的演示文件与仓库内文件字节一致（含跨进程时间戳归一化）；
2. manifest 与实际文件/实际导入管线行为一致（行数、方向隔离、缺失不填零、
   真零保留、小计标记、角色门控、异常规则、匹配档位、合同事实）；
3. 演示数据不含敏感内容（本机路径、用户名、凭据形态字符串）。

这些测试同时是安装包内置演示数据的验收门槛：打包脚本从 examples/demo 取件。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "demo"
GENERATOR = REPO_ROOT / "scripts" / "generate_demo_data.py"

DEMO_FILES = [
    "演示-对上结算-第1至3期.xlsx",
    "演示-对下结算-第1至3期.xlsx",
    "演示-对下结算-附表.xlsx",
    "演示-合同摘录-合成.docx",
]
# 文档级/Sheet 级语义门控词：演示数据除刻意的"人材机汇总"Sheet 外不得命中
GATE_WORDS = ("汇总", "核销", "台账", "summary", "reconciliation", "ledger")
FORBIDDEN_SNIPPETS = (b"/Users/", b"lqwdl", b"local_private_data", b"ghp_", b"sk-",
                      b"BEGIN RSA", b"10.49.27.")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def manifest() -> dict:
    raw = (DEMO_DIR / "manifest.json").read_text(encoding="utf-8")
    return json.loads(raw)


def test_demo_files_exist_with_manifest() -> None:
    for name in DEMO_FILES + ["manifest.json", "SHA256SUMS", "README_zh-CN.md"]:
        assert (DEMO_DIR / name).is_file(), f"缺少演示文件：{name}"


def test_manifest_sha256_matches_files(manifest: dict) -> None:
    for entry in manifest["files"]:
        assert _sha256(DEMO_DIR / entry["file_name"]) == entry["sha256"], \
            f"{entry['file_name']} 与 manifest 记录的 SHA-256 不一致"
    # SHA256SUMS 文件本身也要与实际文件一致
    for line in (DEMO_DIR / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split(None, 1)
        assert _sha256(DEMO_DIR / name.strip()) == digest, f"SHA256SUMS 与实际不一致：{name}"


def test_manifest_required_fields_and_disclaimer(manifest: dict) -> None:
    required = {"file_name", "sha256", "data_type", "direction", "periods",
                "expected_parsed_rows", "expected_anomalies", "expected_matching",
                "known_limitations", "coverage"}
    for entry in manifest["files"]:
        missing = required - set(entry)
        assert not missing, f"{entry['file_name']} 缺少 manifest 字段：{missing}"
    assert "合成" in manifest["disclaimer"] and "不代表真实业务结论" in manifest["disclaimer"]
    # 28 项覆盖矩阵
    assert len(manifest["coverage_matrix"]) >= 28, "覆盖矩阵不足 28 项"


def test_demo_data_regenerates_byte_identical(tmp_path: Path) -> None:
    """确定性：独立子进程重新生成（跨进程/跨秒），所有文件字节一致。"""
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--out", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", errors="strict",
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    for name in DEMO_FILES + ["manifest.json", "SHA256SUMS"]:
        assert _sha256(tmp_path / name) == _sha256(DEMO_DIR / name), \
            f"重新生成的 {name} 与仓库内版本不一致（演示数据必须确定性）"


def test_gate_words_absent_from_file_and_sheet_names() -> None:
    """演示文件名不得命中语义门控词；唯一例外是刻意的"人材机汇总"Sheet。"""
    for name in DEMO_FILES:
        stem = Path(name).stem
        assert not any(w.lower() in stem.lower() for w in GATE_WORDS), \
            f"文件名 {name} 命中语义门控词，导入会被整文件拦截"
    import openpyxl

    for name in DEMO_FILES:
        if not name.endswith(".xlsx"):
            continue
        wb = openpyxl.load_workbook(DEMO_DIR / name, read_only=True)
        for ws in wb.worksheets:
            if "汇总" in ws.title:
                assert ws.title == "人材机汇总", f"意外的门控 Sheet 名：{ws.title}"
        wb.close()


def test_demo_content_has_no_sensitive_snippets() -> None:
    for name in DEMO_FILES:
        data = (DEMO_DIR / name).read_bytes()
        for snippet in FORBIDDEN_SNIPPETS:
            assert snippet not in data, f"{name} 含敏感片段 {snippet!r}"


class TestDemoPipeline:
    """演示数据 → 导入 → 校核/异常/匹配 → 合同 → 导出 的端到端行为锁定。"""

    @pytest.fixture(scope="class")
    def imported(self, tmp_path_factory):
        from costguard.core.contracts import extract as contract_extract
        from costguard.core.engine import settlement_io
        from costguard.core.models import project as project_model

        root = tmp_path_factory.mktemp("demo_ws") / "CostGuardProjects"
        info = project_model.create_project("匿名演示项目", root)
        info, conn = project_model.open_project(info.workspace_path)
        pdir = Path(info.workspace_path)
        settlement_io.import_settlement_file(
            conn, info.project_id, pdir, DEMO_DIR / DEMO_FILES[0], direction="upward")
        settlement_io.import_settlement_file(
            conn, info.project_id, pdir, DEMO_DIR / DEMO_FILES[1], direction="downward")
        settlement_io.import_settlement_file(
            conn, info.project_id, pdir, DEMO_DIR / DEMO_FILES[2], direction="downward")
        contract_extract.import_contract(
            conn, info.project_id, pdir, DEMO_DIR / DEMO_FILES[3])
        return info, conn, pdir

    def test_periods_and_direction_isolation(self, imported, manifest: dict) -> None:
        """同期号在对上/对下各自成立期次（方向隔离），期次行数与 manifest 汇总一致。

        注意：附表文件的「第1期补充」并入对下第1期，预期行数需按 (方向, 期号) 求和。
        """
        info, conn, _ = imported
        rows = conn.execute(
            "SELECT period_no, direction, COUNT(li.id) n FROM settlement_periods sp"
            " LEFT JOIN line_items li ON li.period_id=sp.id"
            " AND li.flags_json NOT LIKE '%\"subtotal\": true%'"
            " WHERE sp.project_id=? GROUP BY sp.id ORDER BY sp.direction, sp.period_no",
            (info.project_id,),
        ).fetchall()
        by_dir: dict[str, dict[int, int]] = {}
        for r in rows:
            by_dir.setdefault(r["direction"], {})[r["period_no"]] = r["n"]
        assert set(by_dir) == {"upward", "downward"}, f"方向不完整：{set(by_dir)}"
        expected_totals: dict[str, dict[int, int]] = {}
        for entry in manifest["files"]:
            if entry["data_type"] != "xlsx":
                continue
            for sheet_name, expected in entry["expected_parsed_rows"].items():
                pno = int("".join(ch for ch in sheet_name if ch.isdigit())[:1] or 0)
                d = expected_totals.setdefault(entry["direction"], {})
                d[pno] = d.get(pno, 0) + expected
        for direction, per_pno in expected_totals.items():
            for pno, expected in per_pno.items():
                actual = by_dir[direction].get(pno)
                assert actual == expected, (
                    f"{direction} 第{pno}期 预期 {expected} 行明细（多文件求和），实际 {actual}")
        # 同期号双方向并存（方向隔离的直接证据）
        both = conn.execute(
            """SELECT period_no FROM settlement_periods WHERE project_id=?
               GROUP BY period_no HAVING COUNT(DISTINCT direction)>1""",
            (info.project_id,),
        ).fetchall()
        assert {r["period_no"] for r in both} == {1, 2, 3}

    def test_missing_values_stay_null_and_zero_stays_zero(self, imported) -> None:
        info, conn, _ = imported
        # 缺失单价/金额的行：NULL（不得填 0）。
        # 注意排除小计行要匹配 "subtotal": true 的值，flags 里每行都有 subtotal 键。
        missing = conn.execute(
            """SELECT COUNT(*) n FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
               WHERE sp.project_id=? AND li.flags_json NOT LIKE '%\"subtotal\": true%'
                 AND li.name IN ('屋面SBS防水卷材','其他零星工作') AND li.unit_price IS NULL
                 AND li.amount IS NULL""",
            (info.project_id,),
        ).fetchone()["n"]
        assert missing == 2, f"缺失值行应为 NULL 且共 2 行，实际 {missing}"
        # 缺失数量行（对下钢筋接头）：quantity NULL
        missing_qty = conn.execute(
            """SELECT COUNT(*) n FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
               WHERE sp.project_id=? AND sp.direction='downward' AND li.code='010416001001'
                 AND li.quantity IS NULL AND li.unit_price IS NOT NULL
                 AND li.flags_json NOT LIKE '%\"subtotal\": true%'""",
            (info.project_id,),
        ).fetchone()["n"]
        assert missing_qty == 1
        # 真零行：0 必须保留为 0，不是 NULL
        zero = conn.execute(
            """SELECT li.quantity, li.amount FROM line_items li JOIN settlement_periods sp
               ON sp.id=li.period_id WHERE sp.project_id=? AND sp.direction='upward'
                 AND li.code='010416001001' AND li.flags_json NOT LIKE '%\"subtotal\": true%'""",
            (info.project_id,),
        ).fetchone()
        assert zero is not None, "真零行必须存在"
        # quantity/amount 以 TEXT 存储：值必须是 0 而不是 NULL（真零不得当缺失）
        assert zero["quantity"] is not None and zero["quantity"] == "0"
        assert zero["amount"] is not None and zero["amount"] == "0"

    def test_subtotal_rows_flagged_not_counted_as_items(self, imported) -> None:
        info, conn, _ = imported
        n_sub = conn.execute(
            """SELECT COUNT(*) n FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
               WHERE sp.project_id=? AND li.flags_json LIKE '%\"subtotal\": true%'""",
            (info.project_id,),
        ).fetchone()["n"]
        assert n_sub == 14  # 对上 3 期×2 + 对下 3 期×2 + 附表 1 期×2

    def test_summary_sheet_is_role_gated(self, imported) -> None:
        """「人材机汇总」Sheet 必须被角色门控拦截：不写 line_items，留待人工确认。"""
        info, conn, _ = imported
        row = conn.execute(
            """SELECT rs.id, rs.period_id FROM raw_sheets rs
               JOIN parse_batches pb ON pb.id=rs.batch_id JOIN source_files sf ON sf.id=pb.file_id
               WHERE sf.project_id=? AND rs.sheet_name='人材机汇总'""",
            (info.project_id,),
        ).fetchone()
        assert row is not None, "人材机汇总 Sheet 未被导入记录"
        assert row["period_id"] is None, "被门控的 Sheet 不得写入期次"
        n_items = conn.execute(
            "SELECT COUNT(*) n FROM line_items WHERE sheet_id=?", (row["id"],)
        ).fetchone()["n"]
        assert n_items == 0, "被门控的 Sheet 不得写入明细"
        # 保留角色候选证据 + 审计
        ev = conn.execute(
            """SELECT COUNT(*) n FROM evidence e WHERE e.project_id=?
               AND e.kind='sheet_role_candidate' AND e.summary LIKE '%人材机汇总%'""",
            (info.project_id,),
        ).fetchone()["n"]
        assert ev >= 1, "门控 Sheet 必须留有人工确认入口（evidence 候选）"

    def test_anomaly_rules_match_manifest(self, imported, manifest: dict) -> None:
        from costguard.core.anomalies import engine as anomaly_engine

        info, conn, _ = imported
        findings = anomaly_engine.run_anomalies(conn, info.project_id)
        observed = {f.rule_id for f in findings}
        expected: set[str] = set()
        for entry in manifest["files"]:
            if entry["data_type"] == "xlsx":
                expected |= set(entry["expected_anomalies"])
        assert observed == expected, (
            f"异常规则集合与 manifest 不一致：\n 缺少: {sorted(expected - observed)}"
            f"\n 多出: {sorted(observed - expected)}")

    def test_matching_levels_present(self, imported) -> None:
        from costguard.core.matching import matching

        info, conn, _ = imported
        groups = matching.match_items(conn, info.project_id)
        levels = {g.level for g in groups}
        assert "confirmed" in levels and "probable" in levels
        assert "incomparable" in levels, "单位不一致的组必须标为不可比"
        assert "pending_data" in levels, "缺名称行必须进入待补资料"
        # 方向隔离：group_key 带方向前缀
        assert all(":" in g.group_key for g in groups)

    def test_contract_facts_have_quotes(self, imported) -> None:
        info, conn, _ = imported
        rows = conn.execute(
            """SELECT cf.fact_key, cf.quote_text, cf.location FROM contract_facts cf
               JOIN contract_docs cd ON cd.id=cf.doc_id WHERE cd.project_id=?""",
            (info.project_id,),
        ).fetchall()
        assert len(rows) >= 10, f"合同事实过少：{len(rows)}"
        for r in rows:
            assert r["quote_text"], f"{r['fact_key']} 缺少原文引用"
            assert r["location"], f"{r['fact_key']} 缺少位置信息"

    def test_export_workbook_and_summary(self, imported) -> None:
        from costguard.core.export import excel_export

        info, conn, pdir = imported
        xlsx = excel_export.export_workbook(conn, info.project_id, pdir / "exports")
        docx = excel_export.export_management_summary_docx(conn, info.project_id, pdir / "exports")
        assert xlsx.is_file() and xlsx.stat().st_size > 10_000
        assert docx.is_file() and docx.stat().st_size > 5_000
