"""市场真实资料实测探针（local_private_data 专用，禁止入库）。

目的：用仓库现有导入/解析/门控/比较流程，实测市场上最真实的
对上结算（对业主报审）与对下结算（对分包/劳务）资料，尤其是房建类。

纪律（与 docs/REAL_DATA_ACCEPTANCE.md 一致）：
1. 原件只读：先记 SHA-256，复制到本轮 run/corpus/ 隔离副本再导入；
2. 每个文件独立全新项目（期次/合同语义隔离，不复用任何库）；
3. 任何一步失败记录为状态而不是中断；
4. 探针中的人工确认（页级复核/条款确认/基准确认）一律以
   actor="market-probe" 留痕，属于技术验证动作，不构成业务结论；
5. 测试后复核原件哈希，确认未被修改。

输出：run 目录内 probe_results.json + PROBE_REPORT.md。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

BASE = REPO_ROOT / "local_private_data" / "market_probe"
SAMPLES_FILE = BASE / "samples.json"

# 样本清单从 local_private_data/market_probe/samples.json 读取（不入库，
# 含真实项目名等业务信息）。结构：
#   {"version": 1, "samples": [{"id","kind","note","path"}, ...]}
# kind ∈ contract_docx / contract_pdf_scan / contract_doc_legacy / nonsettlement_xlsx。
# 结算（对下多期）由 scripts/real_acceptance_run.py 既有注册流程覆盖，不在此重复。


def _load_samples() -> list[dict]:
    if not SAMPLES_FILE.exists():
        raise SystemExit(
            f"缺少样本清单 {SAMPLES_FILE}。结构见本脚本头部注释；"
            "真实资料路径只登记在 local_private_data，不进入 Git。"
        )
    data = json.loads(SAMPLES_FILE.read_text(encoding="utf-8"))
    samples = data.get("samples") or []
    if not samples:
        raise SystemExit("samples.json 中没有样本")
    return samples



ACTOR = "market-probe"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _ocr_provider():
    try:
        from jiadun.platform.ocr import RapidOcrProvider

        return RapidOcrProvider()
    except Exception as exc:  # noqa: BLE001 - OCR 不可用是可记录状态
        return {"unavailable": f"{type(exc).__name__}: {exc}"}


def _doc_status(conn, project_id: int, file_id: int) -> dict | None:
    row = conn.execute(
        "SELECT category, parse_status, detail, parser FROM document_intake "
        "WHERE project_id=? AND file_id=?",
        (project_id, file_id),
    ).fetchone()
    return dict(row) if row else None


def _facts_summary(conn, project_id: int) -> list[dict]:
    from jiadun.core.contracts.extract import list_contract_facts

    return [
        {
            "id": f["id"],
            "key": f["fact_key"],
            "value": f["fact_value"],
            "location": f["location"],
            "confidence": f["confidence"],
            "review_status": f["review_status"],
            "quote": (f["quote_text"] or "")[:60],
        }
        for f in list_contract_facts(conn, project_id)
    ]


def _rate_rules_summary(conn, project_id: int) -> list[dict]:
    from jiadun.core.contracts.rate_rules import list_rate_rules

    return [
        {
            "id": r["id"],
            "rate_percent": r["rate_percent"],
            "status": r["status"],
            "basis_type": r.get("base_type"),
            "quote": (r.get("quote_text") or "")[:60],
        }
        for r in list_rate_rules(conn, project_id)
    ]


def _pdf_pages_summary(conn, file_id: int) -> dict:
    row = conn.execute(
        "SELECT status, stats_json FROM parse_batches WHERE file_id=? "
        "AND parser='pdf_hybrid' ORDER BY id DESC LIMIT 1",
        (file_id,),
    ).fetchone()
    if not row:
        return {"batches": 0}
    stats = json.loads(row["stats_json"] or "{}")
    return {
        "batches": 1,
        "batch_status": row["status"],
        "page_status_counts": stats.get("page_status_counts"),
        "pages": [
            {
                "no": p.get("page_no"),
                "status": p.get("status"),
                "method": p.get("extraction_method"),
                "conf": p.get("confidence"),
            }
            for p in (stats.get("pages") or [])
        ],
    }


def probe_contract(sample: dict, run_dir: Path, ocr) -> dict:
    from jiadun.core.contracts import extract as contract_extract
    from jiadun.core.models import project as pm
    from jiadun.core.models.source_file import import_file

    rec: dict = {"id": sample["id"], "note": sample["note"], "kind": sample["kind"]}
    target = run_dir / "projects" / f"探针-{sample['id']}"
    info = pm.create_project(target.name, run_dir / "projects")
    info, conn = pm.open_project(Path(info.workspace_path))
    rec["project"] = Path(info.workspace_path).name
    try:
        sf = import_file(conn, info.project_id, Path(info.workspace_path), sample["copy"])
        rec["file_type"] = sf.file_type
        t0 = time.time()
        try:
            contract_extract.import_contract(
                conn, info.project_id, Path(info.workspace_path), sample["copy"],
                document_category="upward_contract",
                ocr_provider=None if isinstance(ocr, dict) else ocr,
            )
            rec["import_contract"] = {"ok": True, "seconds": round(time.time() - t0, 1)}
        except Exception as exc:  # noqa: BLE001 - 阶段失败是记录项
            rec["import_contract"] = {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
                "seconds": round(time.time() - t0, 1),
            }
        rec["document_status"] = _doc_status(conn, info.project_id, sf.file_id)
        rec["pdf_pages"] = _pdf_pages_summary(conn, sf.file_id)
        rec["facts"] = _facts_summary(conn, info.project_id)

        # 费率候选扫描（框架/管理性协议费率；同一文件不重复登记）
        try:
            from jiadun.core.contracts.rate_rules import import_rate_candidates

            import_rate_candidates(
                conn, info.project_id, sf.file_id,
                ocr_provider=None if isinstance(ocr, dict) else ocr,
            )
        except Exception as exc:  # noqa: BLE001
            rec["rate_scan"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        rec["rate_rules"] = _rate_rules_summary(conn, info.project_id)

        # 含 needs_review 页的 PDF：走 C-2 逐页人工对照复核链
        if sample["kind"] == "contract_pdf_scan":
            rec["page_review"] = _exercise_page_review(conn, info, sf, sample)

        # 证据链可回溯性
        total = conn.execute(
            "SELECT COUNT(*) c FROM evidence WHERE project_id=?", (info.project_id,)
        ).fetchone()["c"]
        with_source = conn.execute(
            "SELECT COUNT(*) c FROM evidence WHERE project_id=? "
            "AND sources_json NOT IN ('[]','{}','','null')",
            (info.project_id,),
        ).fetchone()["c"]
        rec["evidence"] = {
            "records": total,
            "with_source": with_source,
            "status": "available" if with_source else (
                "none" if total == 0 else "needs_review"
            ),
        }
        return rec
    finally:
        conn.close()


def _probe_ocr_provider():
    try:
        from jiadun.platform.ocr import RapidOcrProvider

        return RapidOcrProvider()
    except Exception:  # noqa: BLE001
        return None


def _exercise_page_review(conn, info, sf, sample: dict) -> dict:
    """对 OCR/低置信页执行页级复核链（技术验证，非业务确认）。"""
    from jiadun.core.contracts import page_review
    from jiadun.core.contracts.extract import import_contract

    out: dict = {}
    try:
        pages = page_review.list_pdf_pages(conn, info.project_id, sf.file_id)
    except Exception as exc:  # noqa: BLE001
        return {"list_error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    out["batch_status"] = pages["batch_status"]
    out["pages_requiring_review"] = pages["pages_requiring_review"]
    for page in pages["pages"]:
        if page["requires_review"] and page["decision"] != "verified":
            try:
                page_review.set_page_review(
                    conn, info.project_id, sf.file_id, page["page_number"],
                    decision="verified",
                    reviewed_by=ACTOR,
                    reason="探针技术验证：对照只读原件核验该页文本",
                )
                out.setdefault("verified_pages", []).append(page["page_number"])
            except Exception as exc:  # noqa: BLE001
                out.setdefault("verify_errors", []).append(
                    {"page": page["page_number"], "error": f"{type(exc).__name__}: {exc}"}
                )
    try:
        result = page_review.mark_document_pages_reviewed(
            conn, info.project_id, sf.file_id, reviewed_by=ACTOR
        )
        out["document_review_completed"] = result
    except Exception as exc:  # noqa: BLE001
        out["document_review_error"] = f"{type(exc).__name__}: {str(exc)[:250]}"
    out["doc_status_after_review"] = _doc_status(conn, info.project_id, sf.file_id)
    # 复核完成后重新导入：观察条款事实是否补写（死循环疑点验证）
    try:
        import_contract(
            conn, info.project_id, Path(info.workspace_path), sample["copy"],
            document_category="upward_contract",
            ocr_provider=_probe_ocr_provider(),
        )
        out["reimport_after_review"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        out["reimport_after_review"] = {
            "ok": False, "error_type": type(exc).__name__, "error": str(exc)[:250],
        }
    out["doc_status_after_reimport"] = _doc_status(conn, info.project_id, sf.file_id)
    out["pdf_batches_after_reimport"] = conn.execute(
        "SELECT COUNT(*) c FROM parse_batches WHERE file_id=? AND parser='pdf_hybrid'",
        (sf.file_id,),
    ).fetchone()["c"]
    out["facts_after_reimport"] = _facts_summary(conn, info.project_id)
    return out


def probe_nonsettlement(sample: dict, run_dir: Path) -> dict:
    from jiadun.core.engine import settlement_io
    from jiadun.core.models import project as pm
    from jiadun.core.models.source_file import import_file

    rec: dict = {"id": sample["id"], "note": sample["note"], "kind": sample["kind"]}
    target = run_dir / "projects" / f"探针-{sample['id']}"
    info = pm.create_project(target.name, run_dir / "projects")
    info, conn = pm.open_project(Path(info.workspace_path))
    rec["project"] = Path(info.workspace_path).name
    try:
        sf = import_file(conn, info.project_id, Path(info.workspace_path), sample["copy"])
        rec["file_type"] = sf.file_type
        try:
            report = settlement_io.import_settlement_file(
                conn, info.project_id, Path(info.workspace_path), sample["copy"]
            )
            rec["settlement_parse"] = {
                "status": report.status,
                "needs_manual_review": bool(
                    getattr(report, "needs_manual_review", False)
                ),
                "sheets": [
                    {
                        "name": s.sheet_name,
                        "status": s.status,
                        "n_items": s.n_items,
                        "n_subtotal": s.n_subtotal,
                        "confidence": s.confidence,
                        "notes": (s.notes or "")[:80],
                    }
                    for s in report.sheets
                ],
            }
        except Exception as exc:  # noqa: BLE001
            rec["settlement_parse"] = {
                "error_type": type(exc).__name__, "error": str(exc)[:300]
            }
        # fail-closed 断言材料：非结算表单不得产出任何 canonical 明细
        canonical = conn.execute(
            "SELECT COUNT(*) c FROM line_items li JOIN settlement_periods sp "
            "ON sp.id=li.period_id WHERE sp.project_id=?",
            (info.project_id,),
        ).fetchone()["c"]
        rec["canonical_line_items"] = canonical
        return rec
    finally:
        conn.close()


def probe_control_baseline(run_dir: Path, samples: list[dict]) -> dict:
    """对上控制基准五态实测：真实合同金额 + CONTROL_CONFLICT/INCOMPARABLE/PENDING。

    使用前两个合同样本（主合同+补充协议）的真实金额构造场景：
    - 两个已确认基准无 supersedes → CONTROL_CONFLICT（补充协议改价的真实市场场景）；
    - 税口径不同 → INCOMPARABLE；
    - 基准未确认 → PENDING。
    本项目内没有对上结算期次，PASS/FAIL 无法用真实数据触发，如实记录。
    """
    from jiadun.core.contracts import extract as contract_extract
    from jiadun.core.engine import control_baseline
    from jiadun.core.models import project as pm

    out: dict = {"scenario": "主合同+补充协议真实金额五态边界"}
    target = run_dir / "projects" / "探针-CB"
    info = pm.create_project(target.name, run_dir / "projects")
    info, conn = pm.open_project(Path(info.workspace_path))
    try:
        for sample in samples:
            if sample["id"] not in ("U01", "U02"):
                continue
            try:
                contract_extract.import_contract(
                    conn, info.project_id, Path(info.workspace_path), sample["copy"],
                    document_category="upward_contract",
                )
                out.setdefault("imported", []).append(sample["id"])
            except Exception as exc:  # noqa: BLE001
                out.setdefault("import_errors", []).append(
                    {"id": sample["id"], "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                )
        facts = contract_extract.list_contract_facts(conn, info.project_id)
        money_facts = [
            f for f in facts
            if (f["fact_key"] == "contract_amount" or "金额" in (f["fact_key"] or ""))
            and f["fact_value"]
        ]
        out["money_facts"] = [
            {"id": f["id"], "key": f["fact_key"], "value": f["fact_value"],
             "doc": f["doc_title"], "quote": (f["quote_text"] or "")[:70]}
            for f in money_facts
        ]
        # 场景 1：主合同金额事实确认 → 基准候选 → 确认
        comparisons: list[dict] = []
        if money_facts:
            main_fact = money_facts[0]
            try:
                contract_extract.set_fact_review(
                    conn, info.project_id, main_fact["id"], "confirmed",
                    reviewed_by=ACTOR, reason="探针技术验证：对照原文确认金额事实",
                )
                cand1 = control_baseline.create_candidate_from_fact(
                    conn, info.project_id, main_fact["id"],
                    tax_basis="unknown", scope_note="EPC总承包（探针口径）",
                )
                control_baseline.set_baseline_review(
                    conn, info.project_id, cand1, "confirmed",
                    reviewed_by=ACTOR,
                    reason="探针技术验证：基准确认",
                )
                out["baseline_1"] = {"id": cand1, "from_fact": main_fact["id"]}
                # 基准未确认场景（先建第二个候选不确认）
                if len(money_facts) > 1:
                    supp_fact = money_facts[1]
                    contract_extract.set_fact_review(
                        conn, info.project_id, supp_fact["id"], "confirmed",
                        reviewed_by=ACTOR, reason="探针技术验证：对照原文确认金额事实",
                    )
                    cand2 = control_baseline.create_candidate_from_fact(
                        conn, info.project_id, supp_fact["id"],
                        tax_basis="unknown", scope_note="EPC总承包（探针口径）",
                    )
                    out["baseline_2_candidate_unconfirmed"] = {"id": cand2}
                    # PENDING：拿未确认基准比较
                    comparisons.append({
                        "case": "PENDING（基准未确认）",
                        "result": control_baseline.compare_upward_result(
                            conn, info.project_id, cand2,
                            _decimal_of(main_fact["fact_value"]),
                            settlement_tax_basis="unknown",
                        ),
                    })
                    # 确认第二基准（无 supersedes）→ CONTROL_CONFLICT
                    control_baseline.set_baseline_review(
                        conn, info.project_id, cand2, "confirmed",
                        reviewed_by=ACTOR, reason="探针技术验证：第二基准确认（无替代声明）",
                    )
                    comparisons.append({
                        "case": "CONTROL_CONFLICT（两个有效基准并存）",
                        "result": control_baseline.compare_upward_result(
                            conn, info.project_id, cand1,
                            _decimal_of(main_fact["fact_value"]),
                            settlement_tax_basis="unknown",
                        ),
                    })
                # INCOMPARABLE：税口径明确不同
                comparisons.append({
                    "case": "INCOMPARABLE（税口径不同）",
                    "result": control_baseline.compare_upward_result(
                        conn, info.project_id, cand1,
                        _decimal_of(main_fact["fact_value"]),
                        settlement_tax_basis="excluded" if _baseline_tax(conn, cand1) == "included"
                        else "included",
                    ),
                })
            except Exception as exc:  # noqa: BLE001
                out["scenario_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        for item in comparisons:
            result = item.pop("result", None)
            if isinstance(result, dict):
                item["status"] = result.get("status")
                item["reason"] = (result.get("reason") or "")[:160]
                item["delta"] = result.get("delta")
            else:
                item["result_repr"] = str(result)[:200]
        out["comparisons"] = comparisons
        out["upward_periods"] = control_baseline.list_upward_periods(
            conn, info.project_id
        )
        return out
    finally:
        conn.close()


def _baseline_tax(conn, baseline_id: int) -> str:
    row = conn.execute(
        "SELECT tax_basis FROM control_baselines WHERE id=?", (baseline_id,)
    ).fetchone()
    return str(row["tax_basis"]) if row else "unknown"


def _decimal_of(value):
    from decimal import Decimal, InvalidOperation

    try:
        return Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None


def write_report(run_dir: Path, results: list[dict], probe_meta: dict) -> Path:
    report_path = run_dir / "PROBE_REPORT.md"
    lines: list[str] = [
        "# 市场真实资料实测探针报告",
        "",
        f"- 运行时间：{probe_meta['ran_at']}",
        f"- 版本：{probe_meta['version']}",
        f"- OCR：{probe_meta['ocr']}",
        f"- 原件哈希前后一致：{probe_meta['originals_integrity']}",
        "",
        "## 逐文件结果",
        "",
    ]
    for rec in results:
        lines.append(f"### {rec['id']} {rec.get('note', '')}")
        lines.append(f"- kind: `{rec.get('kind')}`")
        if "import_contract" in rec:
            lines.append(f"- import_contract: `{rec['import_contract']}`")
        if "document_status" in rec and rec["document_status"]:
            lines.append(f"- document_status: `{rec['document_status']}`")
        if rec.get("pdf_pages", {}).get("batches"):
            pp = rec["pdf_pages"]
            lines.append(f"- PDF 页状态分布: `{pp.get('page_status_counts')}`")
        if "settlement_parse" in rec:
            lines.append(f"- settlement_parse: `{rec['settlement_parse']}`")
        if "facts" in rec:
            lines.append(f"- 条款事实 {len(rec['facts'])} 条:")
            for f in rec["facts"][:10]:
                lines.append(
                    f"  - [{f['review_status']}] {f['key']} = {f['value']}"
                    f"（{f['location']}，conf={f['confidence']}）"
                )
        if "rate_rules" in rec:
            lines.append(f"- 费率候选 {len(rec['rate_rules'])} 条: `{rec['rate_rules'][:8]}`")
        if "page_review" in rec:
            lines.append(f"- 页级复核链: `{rec['page_review']}`")
        if "evidence" in rec:
            lines.append(f"- 证据链: `{rec['evidence']}`")
        if "canonical_line_items" in rec:
            lines.append(f"- canonical 明细行: {rec['canonical_line_items']}")
        lines.append("")
    md = "\n".join(lines)
    report_path.write_text(md, encoding="utf-8")
    return report_path


def main() -> None:
    now = datetime.now()
    run_dir = BASE / f"run_{now.strftime('%Y%m%d_%H%M%S')}"
    (run_dir / "corpus").mkdir(parents=True, exist_ok=True)
    (run_dir / "projects").mkdir(parents=True, exist_ok=True)

    from jiadun.version import app_version

    samples = _load_samples()
    ocr = _ocr_provider()
    ocr_desc = dict(ocr.describe()) if not isinstance(ocr, dict) else ocr

    # 原件哈希（前）
    integrity: dict[str, dict] = {}
    for sample in samples:
        src = Path(sample["path"])
        entry: dict = {"exists": src.exists()}
        if src.exists():
            entry["sha256_before"] = sha256_of(src)
            copy = run_dir / "corpus" / f"{sample['id']}_{src.name}"
            shutil.copy2(src, copy)
            entry["copy"] = str(copy)
            entry["copy_sha256"] = sha256_of(copy)
            sample["copy"] = copy
        integrity[sample["id"]] = entry
        sample["_integrity"] = entry

    results: list[dict] = []
    for sample in samples:
        entry = sample["_integrity"]
        if not entry.get("exists"):
            results.append({
                "id": sample["id"], "note": sample["note"], "kind": sample["kind"],
                "missing_source": True,
            })
            continue
        print(f"[{sample['id']}] {sample['note']}", flush=True)
        try:
            if sample["kind"].startswith("contract"):
                results.append(probe_contract(sample, run_dir, ocr))
            else:
                results.append(probe_nonsettlement(sample, run_dir))
        except Exception as exc:  # noqa: BLE001 - 单文件失败不中断
            results.append({
                "id": sample["id"], "note": sample["note"],
                "probe_error": f"{type(exc).__name__}: {str(exc)[:300]}",
            })

    # 控制基准五态实测
    try:
        cb = probe_control_baseline(run_dir, samples)
    except Exception as exc:  # noqa: BLE001
        cb = {"probe_error": f"{type(exc).__name__}: {str(exc)[:300]}"}

    # 原件哈希（后）
    all_match = True
    for sample in samples:
        src = Path(sample["path"])
        entry = sample["_integrity"]
        if entry.get("exists"):
            after = sha256_of(src)
            entry["sha256_after"] = after
            entry["match"] = after == entry["sha256_before"]
            all_match = all_match and entry["match"]
        integrity[sample["id"]] = entry

    payload = {
        "ran_at": now.isoformat(timespec="seconds"),
        "version": app_version(),
        "ocr": ocr_desc,
        "originals_integrity": all_match,
        "integrity": integrity,
        "results": results,
        "control_baseline": cb,
    }
    json_path = run_dir / "probe_results.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    report_path = write_report(run_dir, results, payload)
    print(f"结果: {json_path}")
    print(f"报告: {report_path}")
    print(f"原件完整性: {'OK' if all_match else 'MISMATCH'}")


if __name__ == "__main__":
    main()
