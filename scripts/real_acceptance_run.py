"""真实资料端到端验收执行器（正式版，local_private_data 专用，禁止入库）。

协议（docs/REAL_DATA_ACCEPTANCE.md）：
1. 测试前核对 13 个副本 SHA-256（对 manifest.csv）；
2. **每个文件独立全新项目**（work/<test_id>/，期次语义隔离，不复用任何库）；
3. 逐文件：导入→解析→结构化→Decimal 复算→双路径校核→异常→匹配→导出；
4. 逐文件记录：成功/失败/差异/证据位置/限制；
5. 测试后复核哈希，确认副本未被修改。

所有结果只是测试记录，不构成已批准业务结论。
"""
from __future__ import annotations

import csv
import hashlib
import json
import platform
import shutil
import sys
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "local_private_data" / "real_acceptance"
CORPUS = BASE / "corpus"
WORK = BASE / "work"
MANIFEST = BASE / "manifest.csv"
DECISIONS = BASE / "manual_sheet_decisions.json"


def costguard_version() -> str:
    try:
        return version("costguard")
    except PackageNotFoundError:
        return "source-checkout"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def verify_corpus(records: list[dict]) -> list[dict]:
    out = []
    for rec in records:
        p = BASE / rec["copy_path"]
        actual = sha256_of(p) if p.exists() else None
        out.append({**rec, "exists": p.exists(), "hash_before": actual,
                    "hash_match": actual == rec["sha256"]})
    return out


def load_manual_decisions() -> dict[str, list[dict]]:
    """读取仅存于 local_private_data 的人工 sheet 角色/映射决定。"""
    if not DECISIONS.exists():
        return {}
    data = json.loads(DECISIONS.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("files"), dict):
        raise ValueError("manual_sheet_decisions.json 格式无效：需要 version=1 与 files 对象")
    return data["files"]


def load_acceptance_controls() -> dict[str, dict]:
    """读取人工确认的控制值差异与金额桥接；只作验收证据，不改原数。"""
    if not DECISIONS.exists():
        return {}
    data = json.loads(DECISIONS.read_text(encoding="utf-8"))
    controls = data.get("acceptance_controls", {})
    if not isinstance(controls, dict):
        raise ValueError("manual_sheet_decisions.json acceptance_controls 必须为对象")
    return controls


def record_acceptance_controls(
    conn, project_id: int, controls: dict | None
) -> list[dict]:
    """把已人工确认的源表差异/桥接写入证据链，差异保持 open 待复核。"""
    from costguard.core.evidence import evidence as evidence_api

    recorded: list[dict] = []
    for bridge in (controls or {}).get("bridges", []):
        evidence_id = evidence_api.add_evidence(
            conn,
            project_id,
            "acceptance_bridge",
            bridge["summary"],
            steps=bridge.get("steps", []),
            sources=bridge.get("sources", []),
        )
        recorded.append({
            "kind": "bridge", "evidence_id": evidence_id,
            "summary": bridge["summary"], "status": "evidence_recorded",
        })
    for difference in (controls or {}).get("differences", []):
        evidence_id = evidence_api.add_evidence(
            conn,
            project_id,
            "acceptance_control_difference",
            difference["summary"],
            steps=difference.get("steps", []),
            sources=difference.get("sources", []),
        )
        now = datetime.now().isoformat(timespec="seconds")
        with conn:
            conn.execute(
                """INSERT INTO anomalies(project_id, rule_id, severity, subject_type,
                   subject_id, evidence_id, message, status, created_at)
                   VALUES (?, 'acceptance_control_difference', ?, 'source_control',
                           ?, ?, ?, 'open', ?)""",
                (
                    project_id,
                    difference.get("severity", "medium"),
                    int(difference.get("subject_id", 0)),
                    evidence_id,
                    difference["summary"],
                    now,
                ),
            )
        recorded.append({
            "kind": "difference", "evidence_id": evidence_id,
            "summary": difference["summary"], "status": "open_pending_review",
        })
    return recorded


def write_acceptance_report(report: dict) -> Path:
    """生成仅含验收状态与证据位置的本地报告，不复制私有原文。"""
    results = report["per_file"]
    lines = [
        "# CostGuard 本地真实资料验收报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 软件版本：CostGuard {report['environment']['costguard_version']}",
        f"- 测试环境：{report['environment']['system']} "
        f"{report['environment']['machine']}，Python {report['environment']['python']}",
        f"- 测试副本数量：{len(results)}",
        "- 资料范围：仅 `local_private_data/real_acceptance/corpus/` 副本",
        "- 安全结论：原始副本前后哈希一致；未上传云端或 GitHub。",
        "- 业务边界：自动计算结果仅为测试结果，不构成批准的结算、付款、责任或管理结论。",
        "",
        "## 总体状态",
        "",
        "技术流程与 WPS 人工门槛分列记录。对适用的表格导出，只要 WPS 尚未完成实际打开、"
        "重算、保存、重开，整体不得标记为通过。",
        "",
        "| 测试编号 | 导入 | 技术执行 | 技术校验 | A/B独立复算 | C/源表控制 | 异常 | 证据链 | WPS | 整体状态 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for rec in results:
        steps = rec.get("steps") or {}
        lines.append(
            f"| {rec.get('test_id')} | {'通过' if steps.get('import') else '未通过'} "
            f"| {steps.get('technical_execution_status', '未运行')} "
            f"| {steps.get('technical_validation_status', '未运行')} "
            f"| {steps.get('ab_check_status', '未运行')} "
            f"| {steps.get('control_status', '未运行')} "
            f"| {steps.get('anomaly_status', '未运行')} "
            f"| {steps.get('evidence_trace_status', '未运行')} "
            f"| {steps.get('wps', '未记录')} "
            f"| {steps.get('overall_acceptance_status', '未记录')} |"
        )
    lines.extend(["", "## 逐文件限制", ""])
    for rec in results:
        steps = rec.get("steps") or {}
        limitations = []
        if steps.get("overall_acceptance_status") in {
            "pending_wps", "pending_wps_with_findings"
        }:
            limitations.append("等待 WPS 实际打开/重算/保存/重开")
        if steps.get("overall_acceptance_status") == "pending_wps_with_findings":
            limitations.append("技术执行已完成，但控制值或异常发现尚未闭合")
        if steps.get("overall_acceptance_status") == "needs_manual_review":
            limitations.append("存在未完成的表单或表格角色人工确认")
        if steps.get("overall_acceptance_status") == "parsed_needs_manual_review":
            limitations.append("文本解析已执行，自动提取事实必须回到原页或原段落人工确认")
        text_parse = rec.get("text_parse") or {}
        if text_parse.get("expected_limit"):
            limitations.append(f"当前支持边界：{text_parse['expected_limit']}")
        elif text_parse.get("error"):
            limitations.append(f"文本解析失败：{text_parse['error']}")
        if rec.get("source_evidence_status"):
            limitations.append(f"资料边界：{rec['source_evidence_status']}")
        for item in rec.get("acceptance_controls") or []:
            if item.get("status") == "open_pending_review":
                limitations.append("存在源表控制值差异，待人工复核")
        if rec.get("dual_path_check") and isinstance(rec["dual_path_check"], list):
            for check in rec["dual_path_check"]:
                if check.get("C_subtotal") not in (None, "None") and check.get("A") != check.get("C_subtotal"):
                    limitation = (
                        f"第{check.get('period_no')}期 A/B 与 C 控制额需按桥接解释"
                    )
                    if limitation not in limitations:
                        limitations.append(limitation)
        if not limitations:
            limitations.append("无额外自动限制记录")
        lines.append(f"- **{rec.get('test_id')}**：" + "；".join(dict.fromkeys(limitations)))
    lines.extend(["", "## 测试副本哈希", "",
                  "以下仅列测试编号和 SHA-256，不复制原始路径或私有内容。", ""])
    for test_id, digest in sorted((report.get("corpus_sha256") or {}).items()):
        lines.append(f"- `{test_id}`：`{digest}`")
    path = BASE / "LOCAL_ACCEPTANCE_REPORT.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def inspect_file(test_id: str, purpose: str, copy: Path, project_parent: Path,
                 category: str = "", evidence_status: str = "",
                 manual_decisions: list[dict] | None = None,
                 acceptance_controls: dict | None = None) -> dict:
    """单文件独立项目全流程（项目建在 run 目录内）。任何一步失败都记录而非中断。"""
    rec: dict = {
        "test_id": test_id,
        "copy": copy.name,
        "category": category,
        "purpose": purpose,
        "source_evidence_status": evidence_status,
    }

    from costguard.core.models import project as pm
    from costguard.core.models.source_file import SourceFileError, import_file

    # 中断安全：旧现场一律保留，重试目录带序号（验收-<tid>、_r2、_r3…）
    target = project_parent / f"验收-{test_id}"
    if target.exists():
        n = 2
        while (project_parent / f"验收-{test_id}_r{n}").exists():
            n += 1
        target = project_parent / f"验收-{test_id}_r{n}"
    info = pm.create_project(target.name, project_parent)
    info, conn = pm.open_project(Path(info.workspace_path))
    pdir = Path(info.workspace_path)
    rec["project"] = pdir.name
    try:
        # ---- 导入 ----
        try:
            sf = import_file(conn, info.project_id, pdir, copy)
            rec["import"] = {"ok": True, "file_type": sf.file_type}
        except SourceFileError as exc:
            rec["import"] = {"ok": False, "error": str(exc)}
            return rec
        except Exception as exc:  # noqa: BLE001
            rec["import"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return rec

        stored = Path(sf.stored_path)

        # ---- 结算解析（xlsx/xls/csv）----
        if sf.file_type in ("xlsx", "xls", "csv"):
            from costguard.core.engine import settlement_io
            try:
                report = settlement_io.import_settlement_file(
                    conn, info.project_id, pdir, copy)
                rec["settlement_parse"] = {
                    # 口径：仅 status=='ok'（真实清单解析成功）算 parse 成功；
                    # partial（纯表单待人工）/failed 均不算（监督第七轮）
                    "ok": report.status == "ok",
                    "status": report.status,
                    "needs_manual_review": bool(getattr(report, "needs_manual_review", False)),
                    "sheets": [
                        {"name": s.sheet_name, "status": s.status, "n_items": s.n_items,
                         "n_subtotal": s.n_subtotal, "confidence": s.confidence,
                         "notes": s.notes}
                        for s in report.sheets
                    ],
                }
            except Exception as exc:  # noqa: BLE001
                rec["settlement_parse"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                return rec

            if getattr(report, "needs_manual_review", False):
                # 分列：表单路由 / 角色审阅，互不误标（监督第十轮 A）
                form_sheets = [x.sheet_name for x in report.sheets
                               if x.status == "non_settlement_form"]
                gated_sheets = [x.sheet_name for x in report.sheets
                                if x.status == "needs_role_review"]
                unresolved_form = set(form_sheets)
                unresolved_gated = set(gated_sheets)
                status_by_sheet = {x.sheet_name: x.status for x in report.sheets}
                # 无自动表头的 sheet 仅在私有决定显式给出列/表头/数据范围时，
                # 才加入人工抽取候选；其他 no_header 仍保持原样，不扩大任务范围。
                for decision in manual_decisions or []:
                    if (decision.get("action") == "extract"
                            and status_by_sheet.get(decision.get("sheet")) == "no_header"):
                        unresolved_gated.add(decision["sheet"])
                decision_results: list[dict] = []
                decision_errors: list[dict] = []
                pending = unresolved_form | unresolved_gated
                seen: set[str] = set()
                for decision in manual_decisions or []:
                    sheet_name = decision.get("sheet")
                    action = decision.get("action")
                    if not sheet_name or sheet_name in seen:
                        decision_errors.append({
                            "sheet": sheet_name, "error": "sheet 名缺失或决定重复"})
                        continue
                    seen.add(sheet_name)
                    if sheet_name not in pending:
                        decision_errors.append({
                            "sheet": sheet_name, "error": "该 sheet 不在本轮待人工列表中"})
                        continue
                    row = conn.execute(
                        "SELECT id FROM raw_sheets WHERE batch_id=? AND sheet_name=?",
                        (report.batch_id, sheet_name),
                    ).fetchone()
                    if not row:
                        decision_errors.append({"sheet": sheet_name, "error": "未找到 raw sheet"})
                        continue
                    sheet_id = int(row["id"])
                    try:
                        if action == "extract":
                            if sheet_name not in unresolved_gated:
                                raise ValueError("表单类 sheet 不允许直接作为结算明细抽取")
                            n = settlement_io.confirm_sheet_role_and_extract(
                                conn, info.project_id, sheet_id,
                                actor=decision.get("actor", "acceptance-reviewer"),
                                reason=decision.get("reason", ""),
                                direction=decision.get("direction", "unknown"),
                                period_no=decision.get("period_no"),
                                confirmed_col_map=decision.get("col_map"),
                                confirmed_header_range=(
                                    tuple(decision["header_range"])
                                    if decision.get("header_range") is not None else None),
                                confirmed_data_range=(
                                    tuple(decision["data_range"])
                                    if decision.get("data_range") is not None else None),
                            )
                            decision_results.append({
                                "sheet": sheet_name, "action": action,
                                "status": "confirmed", "n_items": n,
                                "period_no": decision.get("period_no"),
                                "direction": decision.get("direction", "unknown"),
                            })
                        elif action == "evidence_only":
                            settlement_io.confirm_sheet_non_settlement_role(
                                conn, info.project_id, sheet_id,
                                actor=decision.get("actor", "acceptance-reviewer"),
                                confirmed_role=decision.get("role", "other_non_settlement"),
                                reason=decision.get("reason", ""),
                            )
                            decision_results.append({
                                "sheet": sheet_name, "action": action,
                                "role": decision.get("role", "other_non_settlement"),
                                "status": "confirmed",
                            })
                        else:
                            raise ValueError("action 必须是 extract 或 evidence_only")
                    except Exception as exc:  # noqa: BLE001
                        decision_errors.append({
                            "sheet": sheet_name,
                            "error": f"{type(exc).__name__}: {exc}",
                        })
                        continue
                    unresolved_form.discard(sheet_name)
                    unresolved_gated.discard(sheet_name)

                if decision_results:
                    rec["manual_sheet_decisions"] = decision_results
                if decision_errors:
                    rec["manual_decision_errors"] = decision_errors
                if unresolved_form:
                    rec["form_route"] = {"needs_manual_review": True,
                                         "sheets": sorted(unresolved_form)}
                if unresolved_gated:
                    rec["role_review"] = {"needs_manual_review": True,
                                          "sheets": sorted(unresolved_gated)}
                if unresolved_form or unresolved_gated or decision_errors:
                    return rec

                canonical_count = conn.execute(
                    """SELECT COUNT(*) c FROM line_items li JOIN settlement_periods sp
                       ON sp.id=li.period_id WHERE sp.project_id=?""",
                    (info.project_id,),
                ).fetchone()["c"]
                rec["settlement_parse"]["needs_manual_review"] = False
                if canonical_count == 0:
                    rec["settlement_parse"]["status"] = "reviewed_non_settlement"
                    return rec
                rec["settlement_parse"]["ok"] = True
                rec["settlement_parse"]["status"] = "ok_after_manual_confirmation"

            # ---- Decimal 复算（独立路径 1：清洗后明细累计）----
            from costguard.core.engine import aggregate, crosscheck
            try:
                directions = [
                    r["direction"] or "unknown"
                    for r in conn.execute(
                        """SELECT DISTINCT COALESCE(direction, 'unknown') AS direction
                           FROM settlement_periods WHERE project_id=? ORDER BY direction""",
                        (info.project_id,),
                    ).fetchall()
                ]
                directional_aggs = [
                    (direction, agg)
                    for direction in directions
                    for agg in aggregate.aggregate_project(
                        conn, info.project_id, direction=direction
                    )
                ]
                persisted = sum(
                    aggregate.persist_period_totals(
                        conn,
                        info.project_id,
                        [agg for agg_direction, agg in directional_aggs
                         if agg_direction == direction],
                    )
                    for direction in directions
                )
                status_counts: dict[str, int] = {}
                amount_source_counts: dict[str, int] = {}
                warning_groups = 0
                for _direction, agg in directional_aggs:
                    status_counts[agg.status] = status_counts.get(agg.status, 0) + 1
                    amount_source_counts[agg.amount_source] = (
                        amount_source_counts.get(agg.amount_source, 0) + 1
                    )
                    if agg.warnings:
                        warning_groups += 1
                rec["decimal_recompute"] = {
                    "n_groups": len(directional_aggs),
                    "period_totals_persisted": persisted,
                    "status_counts": status_counts,
                    "amount_source_counts": amount_source_counts,
                    "groups_with_warnings": warning_groups,
                    "groups": [
                        {"direction": direction, "key": a.item_key,
                         "name": a.name[:40], "cum_qty": str(a.cum_qty),
                         "cum_amount": str(a.cum_amount), "wavg": str(a.wavg_price),
                         "status": a.status, "warnings": a.warnings[:3]}
                        for direction, a in directional_aggs[:15]
                    ],
                }
            except Exception as exc:  # noqa: BLE001
                rec["decimal_recompute"] = {"error": f"{type(exc).__name__}: {exc}"}

            # ---- 双路径校核（路径2：原始网格重算 + 路径3：原表小计）----
            try:
                by_dir: dict[str, list[int]] = {}
                for r in conn.execute(
                    "SELECT period_no, direction FROM settlement_periods WHERE project_id=?",
                    (info.project_id,)):
                    by_dir.setdefault(r["direction"], []).append(int(r["period_no"]))
                checks = []
                for direction, plist in by_dir.items():
                    checks.extend(crosscheck.run_crosscheck(
                        conn, info.project_id, sorted(set(plist)), direction=direction))
                rec["dual_path_check"] = [
                    {"period_no": c.period_no, "direction": c.direction, "status": c.status,
                     "A": str(c.path_a_total) if c.path_a_total is not None else None,
                     "B": str(c.path_b_total) if c.path_b_total is not None else None,
                     "C_subtotal": str(c.raw_subtotal) if c.raw_subtotal is not None else None,
                     "diff_ab": str(c.diff_ab) if c.diff_ab is not None else None,
                     "control_diff": (
                         str(c.control_diff) if c.control_diff is not None else None
                     ),
                     "control_status": c.control_status,
                     "missing_rows": c.missing_rows}
                    for c in checks
                ]
            except Exception as exc:  # noqa: BLE001
                rec["dual_path_check"] = {"error": f"{type(exc).__name__}: {exc}"}

            # ---- 异常 + 匹配 ----
            from costguard.core.anomalies import engine as anomaly_engine
            from costguard.core.matching import matching
            anomaly_engine.run_anomalies(conn, info.project_id)
            controls_recorded = record_acceptance_controls(
                conn, info.project_id, acceptance_controls
            )
            if controls_recorded:
                rec["acceptance_controls"] = controls_recorded
            sev: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "info": 0}
            by_rule: dict[str, int] = {}
            anomaly_rows = conn.execute(
                "SELECT severity, rule_id FROM anomalies WHERE project_id=?",
                (info.project_id,),
            ).fetchall()
            for finding in anomaly_rows:
                sev[finding["severity"]] = sev.get(finding["severity"], 0) + 1
                by_rule[finding["rule_id"]] = by_rule.get(finding["rule_id"], 0) + 1
            rec["anomalies"] = {"total": len(anomaly_rows), **sev,
                                "top_rules": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])[:8])}
            groups = matching.match_items(conn, info.project_id)
            matching.save_matches(conn, info.project_id, groups)
            levels: dict[str, int] = {}
            for g in groups:
                levels[g.level] = levels.get(g.level, 0) + 1
            rec["matches"] = {
                "total": len(groups),
                "automated_level_counts": levels,
                "human_review_status": "pending",
            }

            # ---- 导出（Excel 审核底稿 + Word 管理层摘要）----
            from costguard.core.export import excel_export
            try:
                xlsx = excel_export.export_workbook(conn, info.project_id, pdir / "exports")
                rec["export"] = {"xlsx": xlsx.name}
            except Exception as exc:  # noqa: BLE001
                rec["export"] = {"error": f"{type(exc).__name__}: {exc}"}
                return rec
            try:
                docx = excel_export.export_management_summary_docx(
                    conn, info.project_id, pdir / "exports")
                rec["export"]["docx"] = docx.name
            except Exception as exc:  # noqa: BLE001
                rec["export"]["docx_error"] = f"{type(exc).__name__}: {exc}"
            evidence_count = conn.execute(
                "SELECT COUNT(*) c FROM evidence WHERE project_id=?", (info.project_id,)
            ).fetchone()["c"]
            traceable_evidence_count = conn.execute(
                """SELECT COUNT(*) c FROM evidence
                   WHERE project_id=? AND sources_json NOT IN ('[]', '{}', '', 'null')""",
                (info.project_id,),
            ).fetchone()["c"]
            rec["evidence_trace"] = {
                "records": evidence_count,
                "records_with_source": traceable_evidence_count,
                "status": "available" if traceable_evidence_count else "needs_review",
            }
            return rec

        # ---- 合同/文本解析（docx/pdf/txt）----
        if sf.file_type in ("docx", "pdf", "txt"):
            from costguard.core.contracts import docx_parser
            from costguard.core.contracts import extract as contract_extract
            try:
                paras = docx_parser.parse_contract(stored, sf.file_type)
                facts = contract_extract.extract_facts(paras)
                rec["text_parse"] = {
                    "ok": True, "n_paragraphs": len(paras), "n_facts": len(facts),
                    "fact_keys": sorted({f["fact_key"] for f in facts}),
                    "samples": [
                        {"key": f["fact_key"], "value": f["fact_value"],
                         "location": f"段落/页 {f['location']}",
                         "quote": f["quote_text"][:80], "confidence": f["confidence"]}
                        for f in facts[:6]
                    ],
                }
            except NotImplementedError as exc:
                rec["text_parse"] = {"ok": False, "expected_limit": str(exc)}
            except Exception as exc:  # noqa: BLE001
                rec["text_parse"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return rec

        rec["text_parse"] = {"ok": False,
                             "expected_limit": f"type '{sf.file_type}' parser not implemented"}
        return rec
    finally:
        conn.close()


def classify_technical_validation(
    *,
    technical_execution_complete: bool,
    ab_check_status: str,
    evidence_status: str,
    high_findings: int,
    control_status: str,
    anomaly_total: int | None,
    decimal_warning_groups: int,
) -> str:
    """把验收事实分类为技术验证状态。

    Decimal 复算警告、控制差异或规则发现都只能得到
    ``with_findings``，不得被技术执行完成所掩盖。
    """
    if not technical_execution_complete:
        return "not_run_or_incomplete"
    if ab_check_status != "ab_passed" or evidence_status != "available" or high_findings:
        return "failed"
    if (
        control_status in {"difference_open", "bridged_pending_review"}
        or (isinstance(anomaly_total, int) and anomaly_total > 0)
        or decimal_warning_groups
    ):
        return "with_findings"
    return "passed"


def main(run_dir: Path | None = None) -> None:
    """非破坏性验收执行：时间戳 run 目录 + 可恢复重跑。

    - 每次运行写入 work/run_<时间戳>/，既有 run 与基线绝不覆盖/删除；
    - 传入 run_dir（或目录已存在）时为续跑：已完成 test_id 跳过；
    - 旧版平铺 work/ 结构首次自动归档为 work/baseline_<stamp>/（mv，非删除）。
    """

    now = datetime.now()
    manual_by_test = load_manual_decisions()
    controls_by_test = load_acceptance_controls()
    with open(MANIFEST, encoding="utf-8") as f:
        records_raw = list(csv.DictReader(f))
    assert len(records_raw) == 13, f"manifest 应有 13 行，实际 {len(records_raw)}"

    pre = verify_corpus(records_raw)
    bad = [r for r in pre if not r["hash_match"] or not r["exists"]]
    assert not bad, f"哈希不符或缺失: {[r['test_id'] for r in bad]}"

    WORK.mkdir(parents=True, exist_ok=True)
    # 旧版平铺结构一次性归档（mv 保留，不删除）
    legacy = [d for d in WORK.iterdir()
              if d.is_dir() and not d.name.startswith(("run_", "baseline_"))]
    if legacy and not run_dir:
        baseline = WORK / f"baseline_{now.strftime('%Y%m%d_%H%M%S')}"
        baseline.mkdir()
        for d in legacy:
            shutil.move(str(d), str(baseline / d.name))

    if run_dir is None:
        run_dir = WORK / f"run_{now.strftime('%Y%m%d_%H%M%S_%f')}"  # 微秒防同秒碰撞
    run_dir = Path(run_dir)
    (run_dir / "done").mkdir(parents=True, exist_ok=True)

    results = []
    for rec in pre:
        done_marker = run_dir / "done" / f"{rec['test_id']}.json"
        if done_marker.exists():  # 可恢复重跑：跳过已完成
            results.append(json.loads(done_marker.read_text(encoding="utf-8")))
            continue
        copy = BASE / rec["copy_path"]
        result = inspect_file(
            rec["test_id"], rec["purpose"], copy, run_dir,
            category=rec.get("category", ""),
            evidence_status=rec.get("evidence_status", ""),
            manual_decisions=manual_by_test.get(rec["test_id"]),
            acceptance_controls=controls_by_test.get(rec["test_id"]),
        )
        # 分步状态（监督要求分列，文本只解析不算完整流程成功）
        imp = result.get("import", {})
        file_type = imp.get("file_type")
        spreadsheet_file = file_type in {"xlsx", "xls", "csv"}
        sp = result.get("settlement_parse")
        tp = result.get("text_parse")
        decimal_ok = bool(
            (result.get("decimal_recompute") or {}).get("n_groups")
            and result["decimal_recompute"]["n_groups"] > 0
        )
        dual_checks = result.get("dual_path_check")
        dual_ran = bool(isinstance(dual_checks, list) and dual_checks)
        compute_ok = decimal_ok and dual_ran
        anomalies_ok = "total" in (result.get("anomalies") or {})
        matches_ok = "total" in (result.get("matches") or {})
        excel_ok = "xlsx" in (result.get("export") or {})
        word_ok = "docx" in (result.get("export") or {})
        manual_pending = bool(
            (result.get("form_route") or {}).get("needs_manual_review")
            or (result.get("role_review") or {}).get("needs_manual_review")
            or (sp or {}).get("needs_manual_review")
        )
        technical_execution_complete = bool(
            imp.get("ok") and (sp or {}).get("ok") and compute_ok
            and anomalies_ok and matches_ok and excel_ok and word_ok
            and not manual_pending
        )
        if isinstance(dual_checks, list) and dual_checks:
            ab_check_status = (
                "ab_passed" if all(c.get("status") == "match" for c in dual_checks)
                else "ab_checked_with_differences"
            )
        elif isinstance(dual_checks, dict) and dual_checks.get("error"):
            ab_check_status = "failed"
        else:
            ab_check_status = "not_applicable_or_not_run"

        control_records = result.get("acceptance_controls") or []
        has_bridge = any(c.get("kind") == "bridge" for c in control_records)
        has_open_control = any(
            c.get("status") == "open_pending_review" for c in control_records
        )
        c_statuses = [c.get("control_status") for c in dual_checks] if dual_ran else []
        if has_open_control:
            control_status = "difference_open"
        elif any(status == "diff" for status in c_statuses):
            control_status = "bridged_pending_review" if has_bridge else "difference_open"
        elif any(status == "match" for status in c_statuses):
            control_status = "passed"
        elif has_bridge:
            control_status = "bridged_pending_review"
        else:
            control_status = "not_available"
        anomaly_total = (result.get("anomalies") or {}).get("total")
        anomaly_status = (
            "checked_with_findings" if isinstance(anomaly_total, int) and anomaly_total > 0
            else "checked_no_rule_findings" if anomaly_total == 0
            else "not_run"
        )
        high_findings = (result.get("anomalies") or {}).get("high", 0)
        decimal_warning_groups = (result.get("decimal_recompute") or {}).get(
            "groups_with_warnings", 0
        )
        evidence_status = (result.get("evidence_trace") or {}).get(
            "status", "not_applicable_or_not_run"
        )
        technical_validation_status = classify_technical_validation(
            technical_execution_complete=technical_execution_complete,
            ab_check_status=ab_check_status,
            evidence_status=evidence_status,
            high_findings=high_findings,
            control_status=control_status,
            anomaly_total=anomaly_total,
            decimal_warning_groups=decimal_warning_groups,
        )
        if not spreadsheet_file and (tp or {}).get("ok"):
            technical_validation_status = "manual_source_review_required"
            overall_status = "parsed_needs_manual_review"
        elif manual_pending:
            overall_status = "needs_manual_review"
        elif technical_execution_complete and technical_validation_status == "passed":
            overall_status = "pending_wps"
        elif technical_execution_complete and technical_validation_status == "with_findings":
            overall_status = "pending_wps_with_findings"
        else:
            overall_status = "not_passed"
        technical_execution_status = (
            "settlement_pipeline_complete" if technical_execution_complete
            else "text_parse_complete" if (tp or {}).get("ok")
            else "incomplete_or_unsupported"
        )
        result["steps"] = {
            "import": bool(imp.get("ok")),
            "parse": bool((sp or {}).get("ok") or (tp or {}).get("ok")),
            "compute": compute_ok,
            "anomalies": anomalies_ok,
            "matches": matches_ok,
            "excel": excel_ok,
            "word": word_ok,
            # WPS 只验收表格导出；文档、文本、图片不制造无关门槛。
            "wps": "pending_manual" if spreadsheet_file else "not_applicable",
            "technical_execution_complete": technical_execution_complete,
            "technical_execution_status": technical_execution_status,
            "technical_validation_status": technical_validation_status,
            "ab_check_status": ab_check_status,
            "control_status": control_status,
            "anomaly_status": anomaly_status,
            "evidence_trace_status": evidence_status,
            "overall_acceptance_status": overall_status,
            "non_settlement_form_needs_manual_review": bool(
                (result.get("form_route") or {}).get("needs_manual_review")),
            "non_settlement_spreadsheet_needs_role_review": bool(
                (result.get("role_review") or {}).get("needs_manual_review")),
        }
        done_marker.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                               encoding="utf-8")
        results.append(result)

    post = verify_corpus(records_raw)
    modified = [r["test_id"] for r in post if r["hash_before"] != r["sha256"]]

    report = {
        "generated_at": now.isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "environment": {
            "costguard_version": costguard_version(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "hash_check": {"before_all_match": all(r["hash_match"] for r in pre),
                       "after_all_match": all(r["hash_match"] for r in post),
                       "modified_copies": modified},
        "corpus_sha256": {r["test_id"]: r["hash_before"] for r in post},
        "per_file": results,
    }
    (run_dir / "acceptance_results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    acceptance_report = write_acceptance_report(report)
    technical = sum(
        1 for r in results if (r.get("steps") or {}).get("technical_execution_complete")
    )
    print(json.dumps({
        "run_dir": str(run_dir),
        "hash_ok": report["hash_check"],
        "files_import_ok": sum(1 for r in results if (r.get("import") or {}).get("ok")),
        "files_technical_execution_complete": technical,
        "files_pending_wps": sum(
            1 for r in results
            if (r.get("steps") or {}).get("overall_acceptance_status")
            in {"pending_wps", "pending_wps_with_findings"}
        ),
        "files_total": len(results),
        "report": str(acceptance_report),
    }, ensure_ascii=False, indent=2))


def force_utf8_stdio() -> None:
    """CLI 入口专用：Windows 控制台默认 cp1252，中文输出会 UnicodeEncodeError。

    重配置为 UTF-8（errors=replace）保证中文可输出；仅在脚本自身作为命令行
    入口运行时调用，不做 import 期全局副作用（库内/测试进程不受影响）。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    force_utf8_stdio()
    main()
