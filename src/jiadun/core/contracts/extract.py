"""合同条款结构化提取（Phase 6）。

纪律（任务书六 / ADR-010）：
- 确定性词典 + 正则抽取，每条事实必须携带原文引用（quote + 段落位置）；
- 不使用 LLM 结论替代原文；未识别 = 未识别，不编造；
- 置信度：明确句式 0.9 / 关键词命中 0.6 / 仅关键词出现 0.4。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jiadun.core import document_intake
from jiadun.core.contracts import docx_parser, run_contract
from jiadun.core.evidence import evidence as evidence_api
from jiadun.core.evidence import finding_lifecycle
from jiadun.core.evidence.finding import Finding
from jiadun.core.parsing.pdf_pipeline import (
    PAGE_STATUSES,
    PARSEABLE_PAGE_STATUSES,
    PDF_PIPELINE_VERSION,
    TRUSTED_RAPIDOCR_MODEL_FILES,
    TRUSTED_RAPIDOCR_MODEL_SHA256,
    TRUSTED_RAPIDOCR_MODEL_SIZE_BYTES,
    OcrProvider,
    PdfExtractionPending,
    PdfExtractionReport,
    PdfPipelineError,
    PdfRenderer,
)

_MONEY = r"([¥￥]\s*[\d,，]+(?:\s*\.\s*\d+)?\s*(?:万元|亿元|元)?|\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?\s*(?:万元|亿元|元)?|\d{4,}(?:\.\d+)?\s*(?:万元|亿元|元)?)"
_DAYS = r"(\d+)\s*(?:个)?\s*(日历天|工作日|天|日内)"


@dataclass
class FactPattern:
    fact_key: str
    trigger: re.Pattern  # 是否进入提取
    value: re.Pattern | None  # 值提取（可选）
    confidence: float


FACT_PATTERNS: list[FactPattern] = [
    FactPattern("contractor_party", re.compile(r"(承包人|承包方|乙方|施工单位)"), re.compile(r"(?:承包人|承包方|乙方|施工单位)[：:为\s]*([^\s，。；、，]{2,30})"), 0.9),
    FactPattern("employer_party", re.compile(r"(发包人|发包方|甲方|建设单位)"), re.compile(r"(?:发包人|发包方|甲方|建设单位)[：:为\s]*([^\s，。；、，]{2,30})"), 0.9),
    FactPattern("contract_amount", re.compile(r"(合同价[款格]?|合同总价|签约合同价|合同金额)"), re.compile(r"(?:合同价[款格]?|合同总价|签约合同价|合同金额)[^0-9]{0,20}" + _MONEY), 0.9),
    FactPattern("duration", re.compile(r"(工期|计划开工|计划竣工|开工日期|竣工日期)"), re.compile(r"工期[^。]{0,60}?" + _DAYS), 0.8),
    FactPattern("pricing_method", re.compile(r"(固定总价|固定单价|可调价格|可调单价|成本加酬金|单价合同|总价合同)"), re.compile(r"(固定总价|固定单价|可调价格|可调单价|成本加酬金|单价合同|总价合同)"), 0.9),
    FactPattern("price_adjustment", re.compile(r"(调价|价格调整|价差|价格波动|人工费调整|信息价)"), None, 0.6),
    FactPattern("variation_clause", re.compile(r"(工程变更|设计变更|变更估价|变更指示)"), None, 0.6),
    FactPattern("visa_clause", re.compile(r"(现场签证|工程签证|签证单)"), None, 0.6),
    FactPattern("settlement_clause", re.compile(r"(竣工结算|结算审核|结算办理|结算书)"), re.compile(r"[^。]{0,50}" + _DAYS), 0.7),
    FactPattern("payment_clause", re.compile(r"(预付款|进度款|付款|支付)"), re.compile(r"[^。]{0,50}" + _DAYS), 0.7),
    FactPattern("tax_clause", re.compile(r"(增值税|税率|发票|含税|不含税|税金)"), re.compile(r"税率[为是：:\s]*([\d.]+\s*%?)"), 0.8),
    FactPattern("breach_clause", re.compile(r"(违约|罚款|赔偿金|履约保证金)"), None, 0.6),
    FactPattern("claim_clause", re.compile(r"(索赔)"), re.compile(r"[^。]{0,50}" + _DAYS), 0.7),
    FactPattern("approval_requirement", re.compile(r"(审批|审核批准|经.{0,6}批准|报.{0,6}审批)"), None, 0.5),
    FactPattern("responsible_party", re.compile(r"(由.{0,12}(承包人|发包人|监理|建设单位|施工单位)负责)"), re.compile(r"由[^。]{0,12}?((?:承包人|发包人|监理|建设单位|施工单位))负责"), 0.8),
]

MAX_QUOTE_LEN = 200
_DAY_RE = re.compile(_DAYS)
_TIME_LIMIT_KEYS = {"payment_clause", "settlement_clause", "claim_clause", "duration"}


def _norm_money(m: str) -> str:
    return re.sub(r"\s+", "", m or "").replace(",", "").replace("，", "")


def extract_facts(paras: list[dict]) -> list[dict]:
    """从段落流提取事实列表。未识别的 fact_key 不产生记录。"""
    facts: list[dict] = []
    for p in paras:
        text = p["text"]
        for pat in FACT_PATTERNS:
            if not pat.trigger.search(text):
                continue
            value = None
            conf = pat.confidence if pat.value else 0.4
            if pat.fact_key in _TIME_LIMIT_KEYS:
                # 时限类：整段独立搜索单位（避免前缀窗口贪婪回溯截断数字，如"2|8 天"）
                m2 = _DAY_RE.search(text)
                if m2:
                    value = f"{m2.group(1)} {m2.group(2)}"
                    conf = max(pat.confidence, 0.7)
                elif pat.confidence < 0.9:
                    conf = 0.4
            elif pat.value:
                m = pat.value.search(text)
                if m:
                    value = _norm_money(m.group(1)) if m.lastindex else None
                    if value is None:
                        value = m.group(0)[:60]
                    conf = pat.confidence
                elif pat.confidence < 0.9:
                    conf = 0.4  # 有值模式但未匹配到具体值
            facts.append(
                {
                    "fact_key": pat.fact_key,
                    "fact_value": value,
                    "quote_text": text[:MAX_QUOTE_LEN] + ("…" if len(text) > MAX_QUOTE_LEN else ""),
                    "location": str(p["index"]),
                    "confidence": conf,
                    "page_number": p.get("page_number"),
                    "page_status": p.get("page_status"),
                    "extraction_method": p.get("extraction_method"),
                    "ocr_confidence": p.get("ocr_confidence"),
                    "ocr_provider": p.get("ocr_provider"),
                    "ocr_model": p.get("ocr_model"),
                    "ocr_model_version": p.get("ocr_model_version"),
                }
            )
    return facts


def _pdf_processing_status(report: PdfExtractionReport) -> str:
    """将页级状态聚合为文件级可重试状态。"""
    statuses = {page.status for page in report.pages}
    if not report.coverage_complete:
        return "failed"
    if statuses & {"pending_ocr", "ocr_failed"}:
        return "pending_ocr"
    if statuses & {"needs_review", "ocr"}:
        # OCR 页即使置信度达到阈值，也不能跳过合同人工复核。
        return "needs_review"
    return "parsed"


def _persist_pdf_batch(
    conn: sqlite3.Connection,
    file_id: int,
    report: PdfExtractionReport,
    *,
    status: str,
    source_sha256: str,
) -> None:
    """把页级摘要写入现有解析批次表；不重复存储整页 OCR 原文。"""
    conn.execute(
        """INSERT INTO parse_batches(file_id, parser, parsed_at, status, stats_json)
           VALUES (?,?,?,?,?)""",
        (
            int(file_id),
            "pdf_hybrid",
            datetime.now().isoformat(timespec="seconds"),
            status,
            json.dumps(
                report.as_stats(source_sha256=source_sha256),
                ensure_ascii=False,
                default=str,
            ),
        ),
    )


def _reusable_pdf_batch(
    conn: sqlite3.Connection,
    file_id: int,
    source_sha256: str,
) -> sqlite3.Row | None:
    """只复用已证明全页覆盖的当前 PDF 批次，避免旧版部分解析快捷返回。"""
    row = conn.execute(
        """SELECT id, status, stats_json FROM parse_batches
           WHERE file_id=? AND parser='pdf_hybrid'
           ORDER BY parsed_at DESC, id DESC LIMIT 1""",
        (int(file_id),),
    ).fetchone()
    if row is None or row["status"] not in {"parsed", "needs_review"}:
        return None
    try:
        stats = json.loads(row["stats_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(stats, dict):
        return None
    if not _valid_pdf_batch_stats(stats, source_sha256=source_sha256, status=row["status"]):
        return None
    return row


def _valid_pdf_batch_stats(
    stats: object, *, source_sha256: str, status: str
) -> bool:
    """复用前重新验证持久化快照，数据库中任何不一致都拒绝复用。"""
    if not isinstance(stats, dict):
        return False
    if (
        stats.get("parser_version") != PDF_PIPELINE_VERSION
        or stats.get("source_sha256") != source_sha256
        or stats.get("coverage_complete") is not True
        or stats.get("parse_ready") is not True
        or stats.get("error") not in (None, "")
    ):
        return False
    page_count = stats.get("page_count")
    pages = stats.get("pages")
    if (
        isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or page_count <= 0
        or not isinstance(pages, list)
        or len(pages) != page_count
    ):
        return False
    expected_counts = {page_status: 0 for page_status in sorted(PAGE_STATUSES)}
    provider_metadata = stats.get("ocr_provider")
    page_numbers: list[int] = []
    for page in pages:
        if not isinstance(page, dict):
            return False
        page_number = page.get("page_no")
        page_status = page.get("status")
        method = page.get("extraction_method")
        text_hash = page.get("text_sha256")
        text_chars = page.get("text_char_count")
        line_count = page.get("line_count")
        if (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_status not in PARSEABLE_PAGE_STATUSES
            or not isinstance(text_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", text_hash)
            or isinstance(text_chars, bool)
            or not isinstance(text_chars, int)
            or text_chars <= 0
            or isinstance(line_count, bool)
            or not isinstance(line_count, int)
            or line_count <= 0
            or page.get("error") not in (None, "")
        ):
            return False
        expected_method = "native_text" if page_status == "native_text" else "ocr"
        if method != expected_method:
            return False
        if page_status == "native_text":
            if page.get("confidence") is not None:
                return False
        else:
            confidence = page.get("confidence")
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0.0 <= float(confidence) <= 1.0
                or not str(page.get("provider_id") or "").strip()
                or not str(page.get("model_id") or "").strip()
                or not str(page.get("model_version") or "").strip()
                or not isinstance(provider_metadata, dict)
                or page.get("provider_id") != provider_metadata.get("id")
                or page.get("model_id") != provider_metadata.get("model_id")
                or page.get("model_version") != provider_metadata.get("model_version")
            ):
                return False
        page_numbers.append(page_number)
        expected_counts[page_status] += 1
    coverage_complete = page_numbers == list(range(1, page_count + 1))
    parse_ready = coverage_complete and all(
        page.get("status") in PARSEABLE_PAGE_STATUSES for page in pages
    )
    if (
        not coverage_complete
        or not parse_ready
        or stats.get("page_status_counts") != expected_counts
    ):
        return False
    if status == "parsed" and expected_counts["ocr"]:
        return False
    if status == "needs_review" and not expected_counts["ocr"]:
        return False
    if expected_counts["ocr"]:
        provider = provider_metadata
        required_text = (
            "id", "engine", "engine_version", "model_id", "model_version",
            "source", "license",
        )
        if not isinstance(provider, dict) or not all(
            isinstance(provider.get(key), str) and provider[key].strip()
            for key in required_text
        ):
            return False
        if (
            provider.get("model_downloaded") is not False
            or not isinstance(provider.get("language"), list)
            or not provider["language"]
            or not all(isinstance(language, str) and language.strip() for language in provider["language"])
            or isinstance(provider.get("model_size_bytes"), bool)
            or not isinstance(provider.get("model_size_bytes"), int)
            or provider["model_size_bytes"] <= 0
            or not isinstance(provider.get("model_files"), list)
            or not provider["model_files"]
            or not isinstance(provider.get("model_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", provider["model_sha256"])
        ):
            return False
        if (
            provider.get("id") != "rapidocr_onnxruntime"
            or provider.get("engine") != "RapidOCR"
            or provider.get("engine_version") != "1.4.4"
            or provider.get("model_id") != "ch_PP-OCRv4_det-rec_cls"
            or provider.get("model_version") != "PP-OCRv4"
            or provider.get("model_size_bytes") != TRUSTED_RAPIDOCR_MODEL_SIZE_BYTES
            or provider.get("model_sha256") != TRUSTED_RAPIDOCR_MODEL_SHA256
        ):
            return False
        expected_model_files = [
            {
                "name": name,
                "filename": filename,
                "sha256": sha256,
                "size_bytes": size_bytes,
            }
            for name, filename, size_bytes, sha256 in TRUSTED_RAPIDOCR_MODEL_FILES
        ]
        if provider["model_files"] != expected_model_files:
            return False
        manifest_digest = hashlib.sha256()
        model_size = 0
        for model in provider["model_files"]:
            if not isinstance(model, dict):
                return False
            filename = model.get("filename")
            model_sha256 = model.get("sha256")
            size_bytes = model.get("size_bytes")
            if (
                not isinstance(model.get("name"), str)
                or not model["name"].strip()
                or not isinstance(filename, str)
                or not filename.strip()
                or isinstance(size_bytes, bool)
                or not isinstance(size_bytes, int)
                or size_bytes <= 0
                or not isinstance(model_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", model_sha256)
            ):
                return False
            model_size += size_bytes
            manifest_digest.update(f"{filename}:{model_sha256}\n".encode())
        if (
            model_size != provider["model_size_bytes"]
            or manifest_digest.hexdigest() != provider["model_sha256"]
        ):
            return False
    return True


def _retire_existing_pdf_contracts(
    conn: sqlite3.Connection,
    project_id: int,
    file_id: int,
    *,
    reason: str,
) -> None:
    """保留旧 Evidence 历史后移除旧 PDF 投影，允许新页级解析重新建立事实。"""
    docs = conn.execute(
        "SELECT id FROM contract_docs WHERE project_id=? AND file_id=?",
        (int(project_id), int(file_id)),
    ).fetchall()
    if not docs:
        return
    doc_ids = [int(row["id"]) for row in docs]
    placeholders = ",".join("?" for _ in doc_ids)
    evidence_rows = conn.execute(
        f"SELECT evidence_id FROM contract_facts WHERE doc_id IN ({placeholders})",
        doc_ids,
    ).fetchall()
    risk_rows = conn.execute(
        f"""SELECT id, finding_id, fingerprint, lifecycle_status, status,
                    evidence_id, run_signature, run_id
            FROM anomalies
            WHERE project_id=? AND rule_id='contract_risk'
              AND subject_type='contract_doc' AND subject_id IN ({placeholders})
              AND COALESCE(lifecycle_status, 'new') <> 'historical'""",
        (int(project_id), *doc_ids),
    ).fetchall()
    evidence_ids = {
        int(row["evidence_id"])
        for row in evidence_rows
        if row["evidence_id"] is not None
    }
    evidence_ids.update(
        int(row["evidence_id"])
        for row in risk_rows
        if row["evidence_id"] is not None
    )
    historical_reason = reason.strip()
    evidence_api.mark_historical(
        conn,
        project_id,
        evidence_ids,
        historical_reason,
        actor="system",
        commit=False,
    )
    now = datetime.now().isoformat(timespec="seconds")
    for row in risk_rows:
        conn.execute(
            """UPDATE anomalies
               SET status='stale', lifecycle_status='historical',
                   resolved_note=COALESCE(resolved_note, ?),
                   lifecycle_updated_at=?, lifecycle_updated_by='system'
               WHERE id=? AND project_id=?""",
            (historical_reason, now, int(row["id"]), int(project_id)),
        )
        conn.execute(
            """INSERT INTO finding_status_events(
                   project_id, anomaly_id, finding_id, fingerprint,
                   before_status, after_status, reason, actor, occurred_at,
                   run_signature, run_id, evidence_id, audit_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(project_id), int(row["id"]), row["finding_id"], row["fingerprint"],
                finding_lifecycle.lifecycle_status(row), "historical",
                historical_reason, "system", now, row["run_signature"], row["run_id"],
                row["evidence_id"], None,
            ),
        )
    conn.execute(
        f"DELETE FROM contract_facts WHERE doc_id IN ({placeholders})", doc_ids
    )
    conn.execute(
        f"DELETE FROM contract_docs WHERE id IN ({placeholders})", doc_ids
    )


def _refresh_contract_after_pdf_failure(
    conn: sqlite3.Connection, project_id: int
) -> None:
    """PDF 失败后让已物化的旧运行退出 current，不能继续引用旧事实。"""
    if run_contract.has_materialized_contract(conn, project_id):
        run_contract.ensure_if_materialized(conn, project_id)


def import_contract(
    conn: sqlite3.Connection,
    project_id: int,
    project_dir: Path,
    src: Path,
    doc_type: str = "unknown",
    document_category: str = "upward_contract",
    *,
    ocr_provider: OcrProvider | None = None,
    pdf_renderer: PdfRenderer | None = None,
) -> int:
    """导入并解析一份合同/补充协议/纪要等。返回 contract_docs.id。"""
    from jiadun.core.models.source_file import import_file

    previous_contract = run_contract.get_current_contract(conn, project_id)
    sf = import_file(conn, project_id, project_dir, src)
    ftype = sf.file_type
    parser_name = "pdf_hybrid" if ftype == "pdf" else "contract_text"
    existing = conn.execute(
        "SELECT id FROM contract_docs WHERE project_id=? AND file_id=? ORDER BY id DESC LIMIT 1",
        (project_id, sf.file_id),
    ).fetchone()
    if existing is not None:
        if ftype == "pdf":
            reusable = _reusable_pdf_batch(conn, sf.file_id, sf.sha256)
            if reusable is not None:
                batch_status = str(reusable["status"])
                detail = (
                    "同一只读原件已完成逐页 PDF 提取，未重复写入合同事实"
                    if batch_status == "parsed"
                    else "同一只读原件已完成逐页 PDF 提取；OCR 页面仍需人工复核，未重复写入合同事实"
                )
                document_intake.record_document(
                    conn, project_id, sf.file_id, category=document_category,
                    parse_status=batch_status, detail=detail, parser=parser_name,
                )
                return int(existing["id"])

            # 旧版本可能已经为混合 PDF 建立了不完整的合同投影。没有当前、
            # 全页覆盖的 pdf_hybrid 批次时，旧投影不能继续成为运行输入。
            # 只删除可重建的 contract_docs/facts，保留原件、parse_batches 和
            # 已写入的 Evidence（并将其历史化）。
            with conn:
                _retire_existing_pdf_contracts(
                    conn, project_id, sf.file_id,
                    reason="PDF 改用逐页混合提取；旧合同投影未证明覆盖全部页面",
                )
        else:
            # 同一 SHA 的非 PDF 合同不得重复写入 facts/Evidence。允许资料中心
            # 对其重新分类，但不会借“重新处理”制造第二份合同事实。
            document_intake.record_document(
                conn, project_id, sf.file_id, category=document_category,
                parse_status="parsed", detail="同一只读原件已解析，未重复写入合同事实",
                parser=parser_name,
            )
            return int(existing["id"])

    document_intake.record_document(
        conn, project_id, sf.file_id, category=document_category,
        parse_status="processing", parser=parser_name,
    )
    if ftype == "xlsx":  # 文本类导入兜底：txt 归入合同文本
        document_intake.mark_document_status(
            conn, project_id, sf.file_id, parse_status="failed",
            detail="合同资料不支持以 XLSX 文本解析；已保留只读原件，等待人工选择资料类别",
            parser="contract_text",
        )
        raise ValueError("xlsx is not a contract text file")
    try:
        parsed = docx_parser.parse_contract_result(
            Path(sf.stored_path), ftype if ftype != "csv" else "txt",
            renderer=pdf_renderer, ocr_provider=ocr_provider,
        )
    except PdfExtractionPending as exc:
        report = exc.report
        status = _pdf_processing_status(report)
        with conn:
            _persist_pdf_batch(
                conn, sf.file_id, report, status=status, source_sha256=sf.sha256
            )
            document_intake.mark_document_status(
                conn, project_id, sf.file_id, parse_status=status,
                detail=str(exc), parser=parser_name, commit=False,
            )
        _refresh_contract_after_pdf_failure(conn, project_id)
        raise
    except PdfPipelineError as exc:
        report = exc.report
        if report is not None:
            with conn:
                _persist_pdf_batch(
                    conn, sf.file_id, report, status="failed", source_sha256=sf.sha256
                )
                document_intake.mark_document_status(
                    conn, project_id, sf.file_id, parse_status="failed",
                    detail=str(exc), parser=parser_name, commit=False,
                )
        else:
            document_intake.mark_document_status(
                conn, project_id, sf.file_id, parse_status="failed",
                detail=str(exc), parser=parser_name,
            )
        if ftype == "pdf":
            _refresh_contract_after_pdf_failure(conn, project_id)
        raise
    except NotImplementedError as exc:
        # 扫描 PDF 不是“没有条款”。文件已安全登记，因此把能力边界持久化为
        # OCR 待处理；UI 重启后仍可看到原因，而不是只留下一个数字。
        document_intake.mark_document_status(
            conn, project_id, sf.file_id, parse_status="pending_ocr",
            detail=str(exc), parser=parser_name,
        )
        if ftype == "pdf":
            _refresh_contract_after_pdf_failure(conn, project_id)
        raise
    except Exception as exc:
        document_intake.mark_document_status(
            conn, project_id, sf.file_id, parse_status="failed",
            detail=f"{type(exc).__name__}: {exc}", parser=parser_name,
        )
        if ftype == "pdf":
            _refresh_contract_after_pdf_failure(conn, project_id)
        raise

    paras = parsed.paragraphs
    pdf_report = parsed.pdf_report
    final_status = _pdf_processing_status(pdf_report) if pdf_report else "parsed"
    if pdf_report:
        ocr_pages = [
            page.page_number
            for page in pdf_report.pages
            if page.extraction_method == "ocr"
        ]
        if ocr_pages:
            final_detail = (
                f"PDF 已逐页提取 {pdf_report.page_count} 页；OCR 页面 "
                f"{','.join(str(page) for page in ocr_pages)} 需人工复核，"
                "候选条款未进入运行契约"
            )
        else:
            final_detail = f"PDF 已逐页提取 {pdf_report.page_count} 页原生文本"
    else:
        final_detail = "合同文本已解析"

    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        if pdf_report is not None:
            _persist_pdf_batch(
                conn, sf.file_id, pdf_report,
                status=final_status, source_sha256=sf.sha256,
            )
        cur = conn.execute(
            "INSERT INTO contract_docs(project_id, file_id, doc_type, title, parsed_at) VALUES (?,?,?,?,?)",
            (project_id, sf.file_id, doc_type, Path(src).stem, now),
        )
        doc_id = cur.lastrowid
        facts = extract_facts(paras)
        for f in facts:
            source = {
                "file": sf.original_name,
                "file_id": sf.file_id,
                "source_sha256": sf.sha256,
                "location": f"段落/页 {f['location']}",
                "quote": f["quote_text"],
            }
            step = {
                "step": "条款抽取",
                "pattern": f["fact_key"],
                "confidence": f["confidence"],
            }
            if f.get("page_number") is not None:
                page_metadata = {
                    "page_no": f["page_number"],
                    "page_status": f.get("page_status"),
                    "extraction_method": f.get("extraction_method"),
                }
                source.update(page_metadata)
                step.update(page_metadata)
                for key in ("ocr_confidence", "ocr_provider", "ocr_model", "ocr_model_version"):
                    if f.get(key) is not None:
                        source[key] = f[key]
                        step[key] = f[key]
            ev_id = evidence_api.add_evidence(
                conn, project_id, "contract_fact",
                f"{f['fact_key']} = {f['fact_value'] or '(关键词命中)'}",
                steps=[step], sources=[source], commit=False,
            )
            conn.execute(
                """INSERT INTO contract_facts(doc_id, fact_key, fact_value, quote_text, location,
                   confidence, evidence_id, review_status) VALUES (?,?,?,?,?,?,?,?)""",
                (doc_id, f["fact_key"], f["fact_value"], f["quote_text"], f["location"],
                 f["confidence"], ev_id, FACT_REVIEW_CANDIDATE),
            )
        document_intake.mark_document_status(
            conn, project_id, sf.file_id, parse_status=final_status,
            detail=final_detail, parser=parser_name, commit=False,
        )
    # 合同事实是运行输入的一部分；已有计算结果的项目在合同资料变化后
    # 立即切换到新签名，旧成果保留但不再作为当前结论使用。
    current_contract = run_contract.ensure_if_materialized(conn, project_id)
    if current_contract is not None and previous_contract is not None:
        # 合同事实会使 Run Contract 产生新运行，但不改变既有结算工作表、
        # 原始网格或逐行覆盖分类。旧 proof Evidence 先历史化，再以新运行
        # 身份追加不可变副本，避免用户随后重跑 A/B/C 时因契约切换而失去
        # 对取数范围的证明。
        from jiadun.core.engine import settlement_io

        settlement_io._rebind_coverage_proofs_for_run(
            conn,
            project_id,
            previous_contract,
            current_contract,
            reason="合同资料进入运行契约；结算 Sheet 原始网格和逐行覆盖分类未改变",
        )
    return doc_id


# ---- 合同风险检查 ----

RISK_CHECKS = [
    ("payment_clause", "high", "缺少付款/支付条款"),
    ("settlement_clause", "high", "缺少结算条款"),
    ("duration", "medium", "未识别工期约定"),
    ("contract_amount", "medium", "未识别合同金额（待补资料）"),
    ("tax_clause", "medium", "未识别税务条款（税率/含税口径不明）"),
    ("pricing_method", "medium", "未识别计价方式"),
]


def contract_risks(conn: sqlite3.Connection, project_id: int) -> list[dict]:
    """按已导入合同检查关键条款覆盖：缺失即风险（不编造结论）。

    已被人工拒绝（rejected）的条款视为缺失；候选（candidate/needs_review）
    条款仍算覆盖但必须给出"待人工确认"提示——确认前不能当作已确认事实。
    """
    docs = conn.execute(
        """SELECT cd.id, cd.title
           FROM contract_docs cd
           LEFT JOIN document_intake di
             ON di.file_id=cd.file_id AND di.project_id=cd.project_id
           WHERE cd.project_id=?
             AND di.file_id IS NOT NULL
             AND di.category='upward_contract'
             AND di.parse_status='parsed'""",
        (project_id,),
    ).fetchall()
    risks = []
    for doc in docs:
        facts = conn.execute(
            """SELECT fact_key, review_status FROM contract_facts WHERE doc_id=?""",
            (doc["id"],),
        ).fetchall()
        keys = {
            r["fact_key"]
            for r in facts
            if (r["review_status"] or FACT_REVIEW_CANDIDATE) != FACT_REVIEW_REJECTED
        }
        for key, severity, msg in RISK_CHECKS:
            if key not in keys:
                risks.append(
                    {"doc_id": doc["id"], "doc_title": doc["title"], "fact_key": key,
                     "severity": severity, "message": f"《{doc['title']}》：{msg}"}
                )
        pending = {
            r["fact_key"]
            for r in facts
            if (r["review_status"] or FACT_REVIEW_CANDIDATE) in
            (FACT_REVIEW_CANDIDATE, FACT_REVIEW_NEEDS_REVIEW)
        }
        if pending:
            risks.append(
                {"doc_id": doc["id"], "doc_title": doc["title"], "fact_key": "fact_review_pending",
                 "severity": "low",
                 "message": f"《{doc['title']}》：{len(pending)} 项条款仍为候选，"
                            "未经人工确认不得作为已确认合同事实"}
            )
    return risks


def persist_risks(conn: sqlite3.Connection, project_id: int, risks: list[dict]) -> int:
    active_contract = run_contract.ensure_run_contract(conn, project_id)
    now = datetime.now().isoformat(timespec="seconds")
    findings = [
        Finding(
            "contract_risk",
            risk["severity"],
            "contract_doc",
            risk["doc_id"],
            risk["message"],
            {"missing": risk["fact_key"], "doc_title": risk["doc_title"]},
        )
        for risk in risks
    ]
    fingerprints = {finding.fingerprint for finding in findings if finding.fingerprint}
    repeated_history: dict[str, list[dict]] = {}
    if fingerprints:
        placeholders = ",".join("?" for _ in fingerprints)
        for row in conn.execute(
            f"""SELECT id, finding_id, fingerprint, status, lifecycle_status,
                       resolved_note, created_at, run_signature, run_id
                FROM anomalies
                WHERE project_id=? AND rule_id='contract_risk'
                  AND fingerprint IN ({placeholders}) ORDER BY id""",
            (project_id, *sorted(fingerprints)),
        ).fetchall():
            repeated_history.setdefault(row["fingerprint"], []).append({
                "anomaly_id": int(row["id"]),
                "finding_id": row["finding_id"],
                "legacy_status": row["status"],
                "lifecycle_status": row["lifecycle_status"],
                "reason": row["resolved_note"],
                "created_at": row["created_at"],
                "run_signature": row["run_signature"],
                "run_id": row["run_id"],
            })
    n = 0
    with run_contract._transaction(conn, "persist_contract_risks"):
        old_rows = conn.execute(
            """SELECT id, finding_id, fingerprint, lifecycle_status, status,
                      evidence_id, run_signature, run_id
               FROM anomalies
               WHERE project_id=? AND rule_id='contract_risk'
                 AND COALESCE(lifecycle_status, 'new') <> 'historical'""",
            (project_id,),
        ).fetchall()
        evidence_api.mark_historical(
            conn,
            project_id,
            {
                int(row["evidence_id"])
                for row in old_rows if row["evidence_id"] is not None
            },
            "本次合同风险检查已生成新的快照，旧 Finding 保留为历史",
            actor="system",
            commit=False,
        )
        for row in old_rows:
            conn.execute(
                """UPDATE anomalies
                   SET status='stale', lifecycle_status='historical',
                       resolved_note=COALESCE(
                           resolved_note,
                           '本次合同风险检查已生成新的快照，旧 Finding 保留为历史'
                       ), lifecycle_updated_at=?, lifecycle_updated_by='system'
                   WHERE id=? AND project_id=?""",
                (now, int(row["id"]), project_id),
            )
            conn.execute(
                """INSERT INTO finding_status_events(
                       project_id, anomaly_id, finding_id, fingerprint,
                       before_status, after_status, reason, actor, occurred_at,
                       run_signature, run_id, evidence_id, audit_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    project_id, int(row["id"]), row["finding_id"], row["fingerprint"],
                    finding_lifecycle.lifecycle_status(row), "historical",
                    "本次合同风险检查已生成新的快照，旧 Finding 保留为历史",
                    "system", now, row["run_signature"], row["run_id"],
                    row["evidence_id"], None,
                ),
            )
        for finding, risk in zip(findings, risks, strict=True):
            ev_id = evidence_api.add_evidence(
                conn,
                project_id,
                "contract_risk",
                finding.message,
                steps=[{
                    "step": "风险检查",
                    "missing": risk["fact_key"],
                    "finding_id": finding.finding_id,
                    "fingerprint": finding.fingerprint,
                    "impact": finding.impact,
                    "limitations": finding.limitations,
                    "recommendation": finding.recommendation,
                }],
                sources=[{"doc": risk["doc_title"], "doc_id": risk["doc_id"]}],
                commit=False,
                run_signature=active_contract.signature,
                run_id=active_contract.run_id,
                finding_id=finding.finding_id,
                scope="current",
            )
            conn.execute(
                """INSERT INTO anomalies(
                       project_id, rule_id, severity, subject_type, subject_id,
                       evidence_id, message, status, created_at, run_signature, run_id,
                       finding_id, fingerprint, confidence, detection_mode,
                       raw_values_json, normalized_values_json, impact,
                       limitations_json, recommendation, lifecycle_status,
                       repeat_history_json)
                   VALUES (?,?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, "contract_risk", risk["severity"], "contract_doc", risk["doc_id"],
                 ev_id, risk["message"], now, active_contract.signature, active_contract.run_id,
                 finding.finding_id, finding.fingerprint, finding.confidence,
                 finding.detection_mode,
                 json.dumps(finding.raw_values, ensure_ascii=False, default=str),
                 json.dumps(finding.normalized_values, ensure_ascii=False, default=str),
                 finding.impact,
                 json.dumps(finding.limitations, ensure_ascii=False, default=str),
                   finding.recommendation, "new",
                   json.dumps(
                       repeated_history.get(finding.fingerprint, []),
                       ensure_ascii=False,
                       default=str,
                   )),
            )
            n += 1
    return n


# ---- 合同事实确认生命周期（阶段 C）----
# 抽取产出只是候选；只有人工确认（confirmed）的事实才能视为已确认合同事实。
# 历史事实无法证明经过人工确认，迁移回填为 candidate，不自动升 confirmed。

FACT_REVIEW_CANDIDATE = "candidate"
FACT_REVIEW_CONFIRMED = "confirmed"
FACT_REVIEW_REJECTED = "rejected"
FACT_REVIEW_NEEDS_REVIEW = "needs_review"
_FACT_REVIEW_DECISIONS = (
    FACT_REVIEW_CONFIRMED,
    FACT_REVIEW_REJECTED,
    FACT_REVIEW_NEEDS_REVIEW,
    FACT_REVIEW_CANDIDATE,
)


def list_contract_facts(
    conn: sqlite3.Connection,
    project_id: int,
    review_status: str | None = None,
) -> list[dict]:
    """列出合同事实（含确认状态）；rejected 默认仍返回，由调用方决定是否展示。"""
    sql = """SELECT cf.id, cf.doc_id, cd.title AS doc_title, cf.fact_key, cf.fact_value,
                    cf.quote_text, cf.location, cf.confidence, cf.evidence_id,
                    cf.review_status, cf.reviewed_at, cf.reviewed_by, cf.review_reason
             FROM contract_facts cf
             JOIN contract_docs cd ON cd.id = cf.doc_id
             WHERE cd.project_id=?"""
    params: list[object] = [project_id]
    if review_status is not None:
        sql += " AND cf.review_status=?"
        params.append(review_status)
    sql += " ORDER BY cf.doc_id, cf.id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def set_fact_review(
    conn: sqlite3.Connection,
    project_id: int,
    fact_id: int,
    decision: str,
    *,
    reviewed_by: str = "user",
    reason: str = "",
) -> dict:
    """人工确认/拒绝/标记待复核一条合同事实，并写入审计 Evidence。

    推翻已确认/已拒绝结论必须给出理由；所有流转都留下前后值与操作者。
    合同事实是运行输入，状态变化后按既有机制产生新的 Run Contract 签名。
    """
    if decision not in _FACT_REVIEW_DECISIONS:
        raise ValueError(f"未知的确认决定：{decision}")
    row = conn.execute(
        """SELECT cf.id, cf.review_status, cf.fact_key, cf.fact_value, cd.title AS doc_title,
                  cd.id AS doc_id
           FROM contract_facts cf JOIN contract_docs cd ON cd.id=cf.doc_id
           WHERE cf.id=? AND cd.project_id=?""",
        (fact_id, project_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"合同事实不存在或不属于当前项目：fact_id={fact_id}")
    before = row["review_status"] or FACT_REVIEW_CANDIDATE
    if before in (FACT_REVIEW_CONFIRMED, FACT_REVIEW_REJECTED) and not (reason or "").strip():
        raise ValueError(f"推翻既有结论（{before}）必须填写理由")
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute(
            """UPDATE contract_facts
               SET review_status=?, reviewed_at=?, reviewed_by=?, review_reason=?
               WHERE id=?""",
            (decision, now, reviewed_by, (reason or "").strip(), fact_id),
        )
        evidence_api.add_evidence(
            conn,
            project_id,
            "contract_fact_review",
            f"《{row['doc_title']}》{row['fact_key']}：{before} → {decision}"
            + (f"（{reason.strip()}）" if (reason or "").strip() else ""),
            steps=[{
                "step": "人工确认合同事实",
                "fact_id": fact_id,
                "before": before,
                "after": decision,
                "reviewed_by": reviewed_by,
                "reason": (reason or "").strip(),
            }],
            sources=[{
                "doc": row["doc_title"],
                "doc_id": row["doc_id"],
                "fact_key": row["fact_key"],
                "fact_value": row["fact_value"],
            }],
            commit=False,
        )
    run_contract.ensure_run_contract(conn, project_id)
    return {
        "fact_id": fact_id,
        "before": before,
        "after": decision,
        "reviewed_at": now,
        "reviewed_by": reviewed_by,
    }
