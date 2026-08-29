"""真实资料端到端验收执行器（local_private_data 专用，禁止入库）。

流程（docs/REAL_DATA_ACCEPTANCE.md）：
1. 测试前核对 13 个副本的 SHA-256（对 manifest.csv）；
2. 全新测试项目，逐文件导入→解析→结构化→Decimal 复算→双路径校核→异常→匹配→导出；
3. 逐文件记录结果（JSON + Markdown 报告）；
4. 测试后复核哈希，确认副本未被修改。

所有结果只是测试记录，不构成已批准业务结论。
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from decimal import Decimal
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
        out.append({
            **rec,
            "exists": p.exists(),
            "hash_before": actual,
            "hash_match": actual == rec["sha256"],
        })
    return out


def safe_json(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(type(obj).__name__)


def inspect_file(conn: sqlite3.Connection, project_id: int, pdir: Path,
                 copy: Path, test_id: str, purpose: str) -> dict:
    """单文件全流程，返回结构化记录。任何一步失败都记录而非中断。"""
    rec: dict = {"test_id": test_id, "copy": copy.name, "purpose": purpose}
    suffix = copy.suffix.lower()

    # ---- 导入 ----
    from costguard.core.models.source_file import SourceFileError, import_file
    try:
        sf = import_file(conn, project_id, pdir, copy)
        rec["import"] = {"ok": True, "file_id": sf.file_id, "file_type": sf.file_type,
                         "sha256": sf.sha256[:16] + "…"}
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
            report = settlement_io.import_settlement_file(conn, project_id, pdir, copy)
            rec["settlement_parse"] = {
                "ok": report.status != "failed",
                "status": report.status,
                "sheets": [
                    {"name": s.sheet_name, "status": s.status, "n_items": s.n_items,
                     "n_subtotal": s.n_subtotal, "confidence": s.confidence, "notes": s.notes}
                    for s in report.sheets
                ],
            }
            period_ids = [int(r["id"]) for r in conn.execute(
                "SELECT DISTINCT sp.id FROM settlement_periods sp JOIN raw_sheets rs ON rs.period_id=sp.id"
                " WHERE sp.project_id=?", (project_id,))]
        except Exception as exc:  # noqa: BLE001
            rec["settlement_parse"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            period_ids = []

        # ---- Decimal 复算 + 双路径 ----
        from costguard.core.engine import aggregate, crosscheck
        try:
            aggs = aggregate.aggregate_project(conn, project_id)
            rec["decimal_recompute"] = {
                "n_groups": len(aggs),
                "groups": [
                    {"key": a.item_key, "name": a.name[:40], "cum_qty": str(a.cum_qty),
                     "cum_amount": str(a.cum_amount), "wavg": str(a.wavg_price),
                     "status": a.status, "warnings": a.warnings[:3]}
                    for a in aggs[:12]
                ],
            }
        except Exception as exc:  # noqa: BLE001
            rec["decimal_recompute"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            pnos = [int(r["period_no"]) for r in conn.execute(
                "SELECT DISTINCT period_no FROM settlement_periods WHERE project_id=?", (project_id,))]
            by_dir: dict[str, list[int]] = {}
            for r in conn.execute(
                "SELECT period_no, direction FROM settlement_periods WHERE project_id=?", (project_id,)):
                by_dir.setdefault(r["direction"], []).append(int(r["period_no"]))
            checks = []
            for direction, plist in by_dir.items():
                checks.extend(crosscheck.run_crosscheck(conn, project_id, sorted(set(plist)), direction=direction))
            rec["dual_path_check"] = [
                {"period_no": r.period_no, "direction": r.direction, "status": r.status,
                 "A": str(r.path_a_total), "B": str(r.path_b_total), "C_subtotal": str(r.raw_subtotal),
                 "diff": str(r.diff_ab), "missing_rows": r.missing_rows}
                for r in checks
            ]
        except Exception as exc:  # noqa: BLE001
            rec["dual_path_check"] = {"error": f"{type(exc).__name__}: {exc}"}
        return rec

    # ---- 合同/文本解析（docx/pdf/txt）----
    if sf.file_type in ("docx", "pdf", "txt"):
        from costguard.core.contracts import docx_parser
        from costguard.core.contracts import extract as contract_extract
        try:
            paras = docx_parser.parse_contract(stored, sf.file_type)
            facts = contract_extract.extract_facts(paras)
            keys = sorted({f["fact_key"] for f in facts})
            rec["text_parse"] = {
                "ok": True, "n_paragraphs": len(paras), "n_facts": len(facts),
                "fact_keys": keys,
                "samples": [
                    {"key": f["fact_key"], "value": f["fact_value"], "location": f["location"],
                     "quote": f["quote_text"][:80], "confidence": f["confidence"]}
                    for f in facts[:6]
                ],
            }
        except NotImplementedError as exc:
            rec["text_parse"] = {"ok": False, "expected_limit": str(exc)}
        except Exception as exc:  # noqa: BLE001
            rec["text_parse"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return rec

    # ---- 不支持类型（doc/image）----
    rec["text_parse"] = {"ok": False, "expected_limit": f"type '{sf.file_type}' parser not implemented"}
    return rec


def main() -> None:
    now = datetime.now()
    records_raw = []
    with open(MANIFEST, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            records_raw.append(row)
    assert len(records_raw) == 13, f"manifest 应有 13 行，实际 {len(records_raw)}"

    pre = verify_corpus(records_raw)
    bad = [r for r in pre if not r["hash_match"] or not r["exists"]]
    assert not bad, f"哈希不符或缺失: {[r['test_id'] for r in bad]}"

    # 全新测试项目
    if WORK.exists():
        shutil.rmtree(WORK)
    project_dir = WORK / "真实资料验收项目"
    from costguard.core.models import project as pm
    info = pm.create_project("真实资料验收-私有副本", WORK)
    info, conn = pm.open_project(Path(info.workspace_path))

    from costguard.core.anomalies import engine as anomaly_engine
    from costguard.core.export import excel_export
    from costguard.core.matching import matching

    results = []
    for rec in pre:
        copy = BASE / rec["copy_path"]
        result = inspect_file(conn, info.project_id, Path(info.workspace_path), copy,
                              rec["test_id"], rec["purpose"])
        results.append(result)

    # 项目级：异常 + 匹配 + 导出
    findings = anomaly_engine.run_anomalies(conn, info.project_id)
    sev = {"high": 0, "medium": 0, "low": 0, "info": 0}
    for f in findings:
        sev[f.severity] += 1
    groups = matching.match_items(conn, info.project_id)
    matching.save_matches(conn, info.project_id, groups)
    by_level: dict[str, int] = {}
    for g in groups:
        by_level[g.level] = by_level.get(g.level, 0) + 1
    xlsx = excel_export.export_workbook(conn, info.project_id, WORK)
    docx = excel_export.export_management_summary_docx(conn, info.project_id, WORK)

    # 测试后哈希复核
    post = verify_corpus(records_raw)
    modified = [r["test_id"] for r in post if r["hash_before"] != r["sha256"]]

    report = {
        "generated_at": now.isoformat(timespec="seconds"),
        "project": {"id": info.project_id, "path": info.workspace_path},
        "hash_check": {
            "before_all_match": all(r["hash_match"] for r in pre),
            "after_all_match": all(r["hash_match"] for r in post),
            "modified_copies": modified,
        },
        "per_file": results,
        "project_level": {
            "anomalies": {"total": len(findings), **sev},
            "matches": {"total": len(groups), **by_level},
            "exports": {"xlsx": xlsx.name, "docx": docx.name},
        },
    }
    (BASE / "acceptance_results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=safe_json), encoding="utf-8")
    print(json.dumps({
        "hash_ok": report["hash_check"],
        "anomalies": report["project_level"]["anomalies"],
        "matches": report["project_level"]["matches"],
        "files_ok": sum(1 for r in results if (r.get("import") or {}).get("ok")),
        "files_total": len(results),
    }, ensure_ascii=False, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
