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
import shutil
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "local_private_data" / "real_acceptance"
CORPUS = BASE / "corpus"
WORK = BASE / "work"
MANIFEST = BASE / "manifest.csv"


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


def inspect_file(test_id: str, purpose: str, copy: Path, project_parent: Path) -> dict:
    """单文件独立项目全流程（项目建在 run 目录内）。任何一步失败都记录而非中断。"""
    rec: dict = {"test_id": test_id, "copy": copy.name, "purpose": purpose}

    from costguard.core.models import project as pm
    from costguard.core.models.source_file import SourceFileError, import_file

    target = project_parent / f"验收-{test_id}"
    if target.exists():
        shutil.rmtree(target)
    info = pm.create_project(f"验收-{test_id}", project_parent)
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
                    "ok": report.status != "failed",
                    "status": report.status,
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

            # ---- Decimal 复算（独立路径 1：清洗后明细累计）----
            from costguard.core.engine import aggregate, crosscheck
            try:
                aggs = aggregate.aggregate_project(conn, info.project_id)
                rec["decimal_recompute"] = {
                    "n_groups": len(aggs),
                    "groups": [
                        {"key": a.item_key, "name": a.name[:40], "cum_qty": str(a.cum_qty),
                         "cum_amount": str(a.cum_amount), "wavg": str(a.wavg_price),
                         "status": a.status, "warnings": a.warnings[:3]}
                        for a in aggs[:15]
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
                     "A": str(c.path_a_total), "B": str(c.path_b_total),
                     "C_subtotal": str(c.raw_subtotal), "diff": str(c.diff_ab),
                     "missing_rows": c.missing_rows}
                    for c in checks
                ]
            except Exception as exc:  # noqa: BLE001
                rec["dual_path_check"] = {"error": f"{type(exc).__name__}: {exc}"}

            # ---- 异常 + 匹配 ----
            from costguard.core.anomalies import engine as anomaly_engine
            from costguard.core.matching import matching
            findings = anomaly_engine.run_anomalies(conn, info.project_id)
            sev: dict[str, int] = {"high": 0, "medium": 0, "low": 0, "info": 0}
            by_rule: dict[str, int] = {}
            for f in findings:
                sev[f.severity] += 1
                by_rule[f.rule_id] = by_rule.get(f.rule_id, 0) + 1
            rec["anomalies"] = {"total": len(findings), **sev,
                                "top_rules": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])[:8])}
            groups = matching.match_items(conn, info.project_id)
            matching.save_matches(conn, info.project_id, groups)
            levels: dict[str, int] = {}
            for g in groups:
                levels[g.level] = levels.get(g.level, 0) + 1
            rec["matches"] = {"total": len(groups), **levels}

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


def main(run_dir: Path | None = None) -> None:
    """非破坏性验收执行：时间戳 run 目录 + 可恢复重跑。

    - 每次运行写入 work/run_<时间戳>/，既有 run 与基线绝不覆盖/删除；
    - 传入 run_dir（或目录已存在）时为续跑：已完成 test_id 跳过；
    - 旧版平铺 work/ 结构首次自动归档为 work/baseline_<stamp>/（mv，非删除）。
    """

    now = datetime.now()
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
        run_dir = WORK / f"run_{now.strftime('%Y%m%d_%H%M%S')}"
    run_dir = Path(run_dir)
    (run_dir / "done").mkdir(parents=True, exist_ok=True)

    results = []
    for rec in pre:
        done_marker = run_dir / "done" / f"{rec['test_id']}.json"
        if done_marker.exists():  # 可恢复重跑：跳过已完成
            results.append(json.loads(done_marker.read_text(encoding="utf-8")))
            continue
        copy = BASE / rec["copy_path"]
        result = inspect_file(rec["test_id"], rec["purpose"], copy, run_dir)
        # 分步状态（监督要求分列，文本只解析不算完整流程成功）
        imp = result.get("import", {})
        sp = result.get("settlement_parse")
        tp = result.get("text_parse")
        result["steps"] = {
            "import": bool(imp.get("ok")),
            "parse": bool((sp or {}).get("ok") or (tp or {}).get("ok")),
            "compute": bool(result.get("decimal_recompute", {}).get("n_groups")
                            or (sp or {}).get("ok")),
            "anomalies": "total" in (result.get("anomalies") or {}),
            "matches": "total" in (result.get("matches") or {}),
            "excel": "xlsx" in (result.get("export") or {}),
            "word": "docx" in (result.get("export") or {}),
            "wps": "pending_manual",  # WPS 实际打开为人工门槛
            "full_pipeline": bool(
                imp.get("ok") and (sp or {}).get("ok")
                and result.get("export", {}).get("xlsx")
                and result.get("export", {}).get("docx")),
        }
        done_marker.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                               encoding="utf-8")
        results.append(result)

    post = verify_corpus(records_raw)
    modified = [r["test_id"] for r in post if r["hash_before"] != r["sha256"]]

    report = {
        "generated_at": now.isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "hash_check": {"before_all_match": all(r["hash_match"] for r in pre),
                       "after_all_match": all(r["hash_match"] for r in post),
                       "modified_copies": modified},
        "per_file": results,
    }
    (run_dir / "acceptance_results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    full = sum(1 for r in results if (r.get("steps") or {}).get("full_pipeline"))
    print(json.dumps({
        "run_dir": str(run_dir),
        "hash_ok": report["hash_check"],
        "files_import_ok": sum(1 for r in results if (r.get("import") or {}).get("ok")),
        "files_full_pipeline": full,
        "files_total": len(results),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
