"""对上/对下结论性框架的结构化报告导出（Markdown）。

每条结论带 Evidence 编号可回溯；INCOMPARABLE / CONTROL_CONFLICT /
PENDING 如实呈现，绝不改写为 PASS；无结论时明确写"无结论不等于通过"。
报告只读数据库，不修改任何原始资料；导出后按 Excel 同一门禁登记为
conclusions_markdown 成果（源数据变化后旧报告标记 stale）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from jiadun import branding
from jiadun.core.contracts import rate_rules, run_contract
from jiadun.core.engine import control_baseline as cb
from jiadun.core.export.excel_export import _save_registered_artifact

_STATUS_ZH = {
    "PASS": "PASS（未超基准）",
    "FAIL": "FAIL（超基准，不构成违规或责任认定）",
    "PENDING": "PENDING（待人工确认，不得视为通过）",
    "INCOMPARABLE": "INCOMPARABLE（不可比，不得强行比较）",
    "CONTROL_CONFLICT": "CONTROL_CONFLICT（基准冲突，须人工裁决）",
}
_BASE_TYPE_ZH = {
    "unset": "未设置",
    "upward_settlement_amount": "对上结算合计",
    "upward_settlement_excl_tax": "对上结算合计（不含税）",
    "contract_amount": "合同价款（已确认事实）",
    "downward_settlement_amount": "对下结算合计",
    "custom": "人工自定义基数",
}
_RULE_STATUS_ZH = {
    "candidate": "候选（待人工确认，不参与计算）",
    "confirmed": "已确认",
    "rejected": "已拒绝",
}
_APPLY_STATUS_ZH = {
    "resolved": "已计算",
    "pending": "待补事实（未计算）",
    "incomparable": "不可比（未计算）",
    "conflict": "候选并存（未计算）",
    "manual_required": "需人工基数（未计算）",
}
_TAX_ZH = {"unknown": "未确认", "incl_tax": "含税", "excl_tax": "不含税"}


def _fmt_money(value) -> str:
    return "—" if value is None else f"{value} 元"


def _fmt_tax(value) -> str:
    return _TAX_ZH.get(cb.normalize_tax_basis(value), cb.normalize_tax_basis(value))


def build_conclusions_markdown(conn: sqlite3.Connection, project_id: int) -> str:
    """生成结论报告全文（不落盘、不写库，便于测试与预览）。"""
    project = conn.execute(
        "SELECT name FROM projects WHERE id=?", (int(project_id),)
    ).fetchone()
    project_name = project["name"] if project else f"项目 #{project_id}"
    version = run_contract._app_version()
    generated_at = datetime.now().isoformat(timespec="seconds")
    lines: list[str] = [
        f"# {branding.PRODUCT_DISPLAY_NAME}结算结论报告",
        "",
        f"- 项目：{project_name}",
        f"- 版本：v{version}",
        f"- 生成时间：{generated_at}",
        "- 结论范围：对上控制基准比较结论 + 框架/管理性协议费率规则与试算",
        "",
        "> 边界声明：本报告不构成违规、责任认定或业务批准结论；",
        "> INCOMPARABLE（不可比）与 CONTROL_CONFLICT（基准冲突）均不视为 PASS；",
        "> 每条结论附证据（Evidence）编号，可回溯至原始文件与计算过程。",
        "",
        "## 一、对上控制基准结论",
        "",
    ]
    conclusions = cb.list_control_conclusions(conn, int(project_id))
    if not conclusions:
        lines += [
            "暂无结论：本项目尚未执行对上控制基准比较。",
            "无结论不等于通过（fail-closed）。",
            "",
        ]
    else:
        lines += [
            "| 结论ID | 状态 | 基准（编号/金额/出处） | 结算期次 | 结算合计 | 差额 | 证据ID |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for c in conclusions:
            period_text = (
                f"第 {c['period_no']} 期·{c['period_title']}"
                if c.get("period_no") is not None else "（未绑定期次）"
            )
            base_text = (
                f"#{c['baseline_id']} / {c['baseline_amount']} 元"
                f" / {(c.get('baseline_source') or '').strip() or '—'}"
            )
            lines.append(
                f"| {c['id']} | {_STATUS_ZH.get(c['status'], c['status'])} "
                f"| {base_text} | {period_text} | {c['settlement_amount']} 元 "
                f"| {_fmt_money(c['delta'])} | {c['evidence_id']} |"
            )
        lines += ["", "说明（按结论ID）：", ""]
        for c in conclusions:
            lines.append(f"- 结论 {c['id']}：{c['reason']}（证据 {c['evidence_id']}，{c['created_at']}）")
        lines.append("")

    lines += ["## 二、框架/管理性协议费率规则与试算", ""]
    rules = rate_rules.list_rate_rules(conn, int(project_id))
    applications = rate_rules.list_rate_applications(conn, int(project_id))
    if not rules and not applications:
        lines += [
            "暂无费率规则：本项目尚未登记框架/管理性协议费率候选。",
            "无规则不等于免计取（fail-closed）。",
            "",
        ]
    if rules:
        lines += [
            "### 已登记规则",
            "",
            "| 规则ID | 比例% | 基数类型 | 基数说明 | 税口径 | 上限 | 下限 | 状态 | 证据原文 | 来源资料 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for r in rules:
            lines.append(
                f"| {r['id']} | {r['rate_percent'] or '（公式型，待人工解读）'} "
                f"| {_BASE_TYPE_ZH.get(r['base_type'], r['base_type'])} "
                f"| {(r['base_definition'] or '').strip() or '（未确认）'} "
                f"| {_fmt_tax(r['tax_basis'])} "
                f"| {_fmt_money(r['cap'])} | {_fmt_money(r['floor'])} "
                f"| {_RULE_STATUS_ZH.get(r['status'], r['status'])} "
                f"| {(r['quote_text'] or '')[:120]} | {r['original_name'] or ''} |"
            )
        lines.append("")
    if applications:
        lines += [
            "### 试算记录（时间倒序，含被阻断的尝试）",
            "",
            "| 试算ID | 规则ID | 比例% | 基数类型 | 基数金额 | 费用 | 结果 | 说明 | 证据ID | 时间 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for a in applications:
            lines.append(
                f"| {a['id']} | {a['rule_id']} | {a['rate_percent'] or '—'} "
                f"| {_BASE_TYPE_ZH.get(a['base_type'], a['base_type'])} "
                f"| {_fmt_money(a['base_amount'])} | {_fmt_money(a['fee'])} "
                f"| {_APPLY_STATUS_ZH.get(a['status'], a['status'])} "
                f"| {a['reason'] or '—'} | {a['evidence_id']} | {a['created_at']} |"
            )
        lines.append("")
    lines += [
        "---",
        "",
        "复核路径：工作台 →「对上控制基准…」/「费率规则…」查看候选与确认记录；",
        "Excel 审核底稿的「对上控制基准结论」「费率规则与试算」工作表含同源数据；",
        "证据索引表可按证据ID回溯到原始文件、Sheet 与计算过程。",
        "",
    ]
    return "\n".join(lines)


def export_conclusions_report(
    conn: sqlite3.Connection, project_id: int, out_dir: Path
) -> Path:
    """导出结算结论 Markdown 报告并登记为受控成果（源变化后旧报告失效）。"""
    run_contract.require_current_results_available(
        conn, project_id, operation="结论报告导出"
    )
    active_contract = run_contract.ensure_run_contract(conn, project_id)
    run_contract.require_current_results_available(
        conn, project_id, operation="结论报告导出"
    )
    content = build_conclusions_markdown(conn, project_id)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = out_dir / f"{branding.PRODUCT_DISPLAY_NAME}结论报告_{stamp}.md"
    return _save_registered_artifact(
        path,
        lambda temp_path: temp_path.write_text(content, encoding="utf-8"),
        lambda final_path: run_contract.register_export(
            conn, project_id, "conclusions_markdown", final_path,
            run_signature=active_contract.signature,
            metadata={"sections": ["control_conclusions", "rate_rules"]},
        ),
    )
