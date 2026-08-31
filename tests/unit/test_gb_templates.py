"""国标表式识别验收测试（issue：真实结算书全部被门控）。

真实依据（移动硬盘 T17/T14/T15，副本仅存 local_private_data）：
- 表-08 分部分项清单（三层表头，父级"金额（元）"横跨综合单价+合价）被判
  needs_role_review——最核心明细表无法自动解析；
- E.6 单位工程竣工结算汇总表被 weak 表单误判为 non_settlement_form；
- 表-08 数据区的"分部标题行"（仅名称无数值）被吸收成垃圾明细。

全部使用 synthetic_test_data/gb_templates 合成复现，不含真实内容。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "synthetic_test_data"))

from gb_templates import make_e6_summary, make_table_08  # noqa: E402


def _import(tmp_path: Path, make, fname: str):
    from jiadun.core.engine import settlement_io
    from jiadun.core.models import project as pm

    info = pm.create_project("国标表式测试", tmp_path / "ws")
    info, conn = pm.open_project(Path(info.workspace_path))
    src = tmp_path / fname
    make(src)
    rep = settlement_io.import_settlement_file(
        conn, info.project_id, Path(info.workspace_path), src, direction="upward")
    return conn, rep


class TestTable08:
    """表-08 分部分项清单：三层表头应自动解析为标准列映射。"""

    def test_parsed_without_role_gate(self, tmp_path):
        conn, rep = _import(tmp_path, make_table_08, "t08.xlsx")
        try:
            assert rep.status == "ok", f"表-08 应直接解析，实际 {rep.status}: " + ";".join(
                s.notes[0] if s.notes else "" for s in rep.sheets)
            assert rep.sheets[0].status == "parsed"
            assert rep.sheets[0].n_items == 3, (
                f"应恰好 3 条明细（分部标题行不得吸收），实际 {rep.sheets[0].n_items}")
            th = conn.execute(
                "SELECT col_map_json FROM table_headers ORDER BY sheet_id DESC LIMIT 1"
            ).fetchone()
            col_map = json.loads(th["col_map_json"])
            assert col_map.get("code") == 2 and col_map.get("name") == 3
            assert col_map.get("unit") == 5 and col_map.get("quantity") == 6
            assert col_map.get("unit_price") == 7, "综合单价（深层叶子）应在第 7 列"
            assert col_map.get("amount") == 8, "合价（深层叶子）应在第 8 列"
            # 分部标题行不得进入结算模型
            junk = conn.execute(
                """SELECT COUNT(*) c FROM line_items WHERE name='路基处理'"""
            ).fetchone()["c"]
            assert junk == 0, "分部标题行（仅名称无数值）不得成为明细"
            # 期次建立且 C 控制可用（小计行保留）
            n_periods = conn.execute(
                "SELECT COUNT(*) c FROM settlement_periods").fetchone()["c"]
            assert n_periods == 1
            sub = conn.execute(
                "SELECT amount FROM line_items WHERE flags_json LIKE '%\"subtotal\": true%'"
            ).fetchone()
            assert sub is not None and sub["amount"] is not None
        finally:
            conn.close()

    def test_crosscheck_ab_match_on_table08(self, tmp_path):
        conn, rep = _import(tmp_path, make_table_08, "t08b.xlsx")
        try:
            from jiadun.core.engine import crosscheck

            period_id = conn.execute(
                "SELECT id FROM settlement_periods LIMIT 1").fetchone()["id"]
            result = crosscheck.check_period(conn, period_id)
            assert result.status == "match", f"合成干净数据 A/B 应一致：{result.notes}"
            assert result.control_status == "match", "C 路径小计控制应可用且一致"
        finally:
            conn.close()


class TestE6Summary:
    """E.6 单位工程汇总表：是资金汇总表，不是键值对表单。"""

    def test_summary_sheet_routes_to_role_review_not_form(self, tmp_path):
        conn, rep = _import(tmp_path, make_e6_summary, "e06.xlsx")
        try:
            assert rep.sheets[0].status == "needs_role_review", (
                f"汇总表应进入角色审阅（语义门控），实际 {rep.sheets[0].status}——"
                "weak 表单不得抢在金额型表头之前")
            # 保留人工确认入口（PR#12 对话框的数据源）
            ev = conn.execute(
                """SELECT COUNT(*) c FROM evidence WHERE kind='sheet_role_candidate'"""
            ).fetchone()["c"]
            assert ev >= 1, "必须留有 sheet_role_candidate 供人工确认"
            # 不得写入结算模型
            assert conn.execute("SELECT COUNT(*) c FROM line_items").fetchone()["c"] == 0
        finally:
            conn.close()
