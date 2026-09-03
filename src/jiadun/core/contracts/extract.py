"""合同条款结构化提取（Phase 6）。

纪律（任务书六 / ADR-010）：
- 确定性词典 + 正则抽取，每条事实必须携带原文引用（quote + 段落位置）；
- 不使用 LLM 结论替代原文；未识别 = 未识别，不编造；
- 置信度：明确句式 0.9 / 关键词命中 0.6 / 仅关键词出现 0.4。
"""
from __future__ import annotations

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
                }
            )
    return facts


def import_contract(
    conn: sqlite3.Connection,
    project_id: int,
    project_dir: Path,
    src: Path,
    doc_type: str = "unknown",
    document_category: str = "upward_contract",
) -> int:
    """导入并解析一份合同/补充协议/纪要等。返回 contract_docs.id。"""
    from jiadun.core.models.source_file import import_file

    previous_contract = run_contract.get_current_contract(conn, project_id)
    sf = import_file(conn, project_id, project_dir, src)
    existing = conn.execute(
        "SELECT id FROM contract_docs WHERE project_id=? AND file_id=? ORDER BY id DESC LIMIT 1",
        (project_id, sf.file_id),
    ).fetchone()
    if existing is not None:
        # 同一 SHA 的合同不得重复写入 facts/Evidence。允许资料中心对其重新分类，
        # 但不会借“重新处理”制造第二份合同事实。
        document_intake.record_document(
            conn, project_id, sf.file_id, category=document_category,
            parse_status="parsed", detail="同一只读原件已解析，未重复写入合同事实",
            parser="contract_text",
        )
        return int(existing["id"])
    document_intake.record_document(
        conn, project_id, sf.file_id, category=document_category,
        parse_status="processing", parser="contract_text",
    )
    ftype = sf.file_type
    if ftype == "xlsx":  # 文本类导入兜底：txt 归入合同文本
        document_intake.mark_document_status(
            conn, project_id, sf.file_id, parse_status="failed",
            detail="合同资料不支持以 XLSX 文本解析；已保留只读原件，等待人工选择资料类别",
            parser="contract_text",
        )
        raise ValueError("xlsx is not a contract text file")
    try:
        paras = docx_parser.parse_contract(
            Path(sf.stored_path), ftype if ftype != "csv" else "txt"
        )
    except NotImplementedError as exc:
        # 扫描 PDF 不是“没有条款”。文件已安全登记，因此把能力边界持久化为
        # OCR 待处理；UI 重启后仍可看到原因，而不是只留下一个数字。
        document_intake.mark_document_status(
            conn, project_id, sf.file_id, parse_status="pending_ocr",
            detail=str(exc), parser="contract_text",
        )
        raise
    except Exception as exc:
        document_intake.mark_document_status(
            conn, project_id, sf.file_id, parse_status="failed",
            detail=f"{type(exc).__name__}: {exc}", parser="contract_text",
        )
        raise
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            "INSERT INTO contract_docs(project_id, file_id, doc_type, title, parsed_at) VALUES (?,?,?,?,?)",
            (project_id, sf.file_id, doc_type, Path(src).stem, now),
        )
        doc_id = cur.lastrowid
        facts = extract_facts(paras)
        for f in facts:
            ev_id = evidence_api.add_evidence(
                conn, project_id, "contract_fact",
                f"{f['fact_key']} = {f['fact_value'] or '(关键词命中)'}",
                steps=[{"step": "条款抽取", "pattern": f["fact_key"], "confidence": f["confidence"]}],
                sources=[{"file": sf.original_name, "location": f"段落/页 {f['location']}",
                          "quote": f["quote_text"]}],
            )
            conn.execute(
                """INSERT INTO contract_facts(doc_id, fact_key, fact_value, quote_text, location,
                   confidence, evidence_id) VALUES (?,?,?,?,?,?,?)""",
                (doc_id, f["fact_key"], f["fact_value"], f["quote_text"], f["location"],
                 f["confidence"], ev_id),
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
    document_intake.mark_document_status(
        conn, project_id, sf.file_id, parse_status="parsed", parser="contract_text",
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
    """按已导入合同检查关键条款覆盖：缺失即风险（不编造结论）。"""
    docs = conn.execute(
        "SELECT id, title FROM contract_docs WHERE project_id=?", (project_id,)
    ).fetchall()
    risks = []
    for doc in docs:
        keys = {
            r["fact_key"]
            for r in conn.execute("SELECT DISTINCT fact_key FROM contract_facts WHERE doc_id=?", (doc["id"],))
        }
        for key, severity, msg in RISK_CHECKS:
            if key not in keys:
                risks.append(
                    {"doc_id": doc["id"], "doc_title": doc["title"], "fact_key": key,
                     "severity": severity, "message": f"《{doc['title']}》：{msg}"}
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
