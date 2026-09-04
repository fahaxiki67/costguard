"""框架/管理性协议费率规则（宪章 §五：费率不是一个百分比）。

- 抽取只产候选：触发词命中的句子里的每一个百分比各成一条候选，
  绝不"取第一个百分比"充当答案（多天数/多百分比条款是已知陷阱）；
- 候选确认时必须人工设定 base_type 与 base_definition——"按结算价 3%"
  与"按不含甲供材税前建安费 3%"不是同一条规则；
- 试算用 Decimal 确定性计算，支持上限（cap）/下限（floor）；
- 全部流转写入审计 Evidence。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from decimal import Decimal

from jiadun.core.engine.money import round2, to_decimal
from jiadun.core.evidence import evidence as evidence_api

BASE_TYPES = (
    "unset",
    "upward_settlement_amount",      # 按对上结算价
    "upward_settlement_excl_tax",    # 按对上结算价（不含税）
    "contract_amount",               # 按合同价款
    "downward_settlement_amount",    # 按对下结算价
    "custom",                        # 人工自定义基数说明
)

RATE_CANDIDATE = "candidate"
RATE_CONFIRMED = "confirmed"
RATE_REJECTED = "rejected"

# 触发词：出现即认为该句可能包含费率约定（保守收集，宁可多出候选）。
# 真实协议语料（民权框架协议）实测补充：利润率/采保/上缴/缴纳/保证金。
_RATE_TRIGGER = re.compile(
    r"管理费|协作费|配合费|费率|收取|计取|点数|个百分点"
    r"|利润率|采保|上缴|缴纳|保证金|奖励"
)
# 百分比形态：3%、3 个点、百分之三；全角％兼容（真实 docx 语料实测存在）。
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
_POINTS = re.compile(r"(\d+(?:\.\d+)?)\s*个(?:点|百分点)")
_CHINESE_PERCENT = re.compile(r"百分之([零一二三四五六七八九十\d]+(?:\.[\d]+)?)")
# 公式型比例：如"（9-X）%"——无数值可提取，但必须出候选交人工解读。
_FORMULA_PERCENT = re.compile(r"[%％]")
_CHINESE_DIGITS = str.maketrans({"零": "0", "一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": ""})
_SENTENCE_SPLIT = re.compile(r"[。；;！？\n]")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def _chinese_percent_to_number(text: str) -> str | None:
    """简单的中文数字百分比换算（百分之三→3；百分之三点五→3.5）。"""
    raw = _CHINESE_PERCENT.match(text)
    if not raw:
        return None
    body = raw.group(1)
    if body.isdigit():
        return body
    translated = body.translate(_CHINESE_DIGITS)
    if translated.isdigit():
        return translated
    if "点" in translated:
        whole, _, frac = translated.partition("点")
        if whole.isdigit() and frac and all(c in "0123456789" for c in frac):
            return f"{whole}.{frac}"
    return None


def extract_rate_candidates_from_paragraphs(paragraphs: list[dict]) -> list[dict]:
    """扫描段落流，产出费率候选（不落库）。

    句子级拆分 + 触发词 + 该句全部百分比：同段多比例各成一条，quote 随身携带。
    """
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for para in paragraphs:
        text = str(para.get("text") or "")
        if not _RATE_TRIGGER.search(text):
            continue
        for sentence in _sentences(text):
            if not _RATE_TRIGGER.search(sentence):
                continue
            # 数值比例：3%／全角％／3 个点／百分之三；同句去重。
            rates = [m.group(1) for m in _PERCENT.finditer(sentence)]
            rates += [m.group(1) for m in _POINTS.finditer(sentence)]
            for m in _CHINESE_PERCENT.finditer(sentence):
                zh = _chinese_percent_to_number(m.group(0))
                if zh:
                    rates.append(zh)
            if rates:
                for rate in dict.fromkeys(rates):
                    key = (sentence[:400], rate)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(
                        {
                            "rate_percent": rate,
                            "needs_manual_rate": False,
                            "quote_text": sentence[:400],
                            "location": str(para.get("index") or para.get("page_number") or ""),
                            "page_number": para.get("page_number"),
                            "page_status": para.get("page_status"),
                        }
                    )
            elif _FORMULA_PERCENT.search(sentence):
                # 公式型比例（如"按结算金额的（9-X）%"）：无数值可提取，
                # 但必须出候选交人工解读，不能静默丢弃。
                key = (sentence[:400], "")
                if key not in seen:
                    seen.add(key)
                    candidates.append(
                        {
                            "rate_percent": None,
                            "needs_manual_rate": True,
                            "quote_text": sentence[:400],
                            "location": str(para.get("index") or para.get("page_number") or ""),
                            "page_number": para.get("page_number"),
                            "page_status": para.get("page_status"),
                        }
                    )
    return candidates


def import_rate_candidates(
    conn: sqlite3.Connection,
    project_id: int,
    file_id: int,
    *,
    renderer=None,
    ocr_provider=None,
) -> int:
    """解析只读副本并登记费率候选（同一文件重复扫描不重复写候选）。"""
    from pathlib import Path

    from jiadun.core.contracts import docx_parser

    src_row = conn.execute(
        "SELECT stored_path, file_type, sha256 FROM source_files WHERE id=? AND project_id=?",
        (int(file_id), int(project_id)),
    ).fetchone()
    if src_row is None:
        raise ValueError(f"资料不存在或不属于当前项目：file_id={file_id}")
    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM rate_rules WHERE project_id=? AND file_id=?",
        (int(project_id), int(file_id)),
    ).fetchone()["n"]
    if existing:
        raise ValueError("该资料已登记过费率候选；如需重扫请先处理既有候选")

    ftype = str(src_row["file_type"])
    ftype = "txt" if ftype == "csv" else ftype
    parsed = docx_parser.parse_contract_result(
        Path(str(src_row["stored_path"])), ftype,
        renderer=renderer, ocr_provider=ocr_provider,
    )
    candidates = extract_rate_candidates_from_paragraphs(parsed.paragraphs)
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        for cand in candidates:
            evidence_api.add_evidence(
                conn, int(project_id), "rate_rule_candidate",
                f"费率候选 {cand['rate_percent']}%：{cand['quote_text'][:80]}",
                steps=[{
                    "step": "框架协议费率扫描",
                    "file_id": int(file_id),
                    "rate_percent": cand["rate_percent"],
                    "page_number": cand.get("page_number"),
                }],
                sources=[{
                    "file_id": int(file_id),
                    "source_sha256": src_row["sha256"],
                    "page_no": cand.get("page_number"),
                    "quote": cand["quote_text"],
                }],
                commit=False,
            )
            conn.execute(
                """INSERT INTO rate_rules(
                       project_id, file_id, rate_percent, quote_text, created_at)
                   VALUES (?,?,?,?,?)""",
                (int(project_id), int(file_id), cand["rate_percent"],
                 cand["quote_text"], now),
            )
    return len(candidates)


def list_rate_rules(
    conn: sqlite3.Connection, project_id: int, status: str | None = None
) -> list[dict]:
    sql = """SELECT rr.id, rr.doc_id, rr.file_id, rr.rate_percent, rr.base_type,
                    rr.base_definition, rr.tax_basis, rr.cap, rr.floor,
                    rr.effective_scope, rr.priority, rr.quote_text, rr.status,
                    rr.reviewed_at, rr.reviewed_by, rr.review_reason,
                    sf.original_name
             FROM rate_rules rr
             LEFT JOIN source_files sf ON sf.id=rr.file_id
             WHERE rr.project_id=?"""
    params: list[object] = [int(project_id)]
    if status is not None:
        sql += " AND rr.status=?"
        params.append(status)
    sql += " ORDER BY rr.id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def confirm_rate_rule(
    conn: sqlite3.Connection,
    project_id: int,
    rule_id: int,
    *,
    base_type: str,
    base_definition: str,
    tax_basis: str = "unknown",
    cap=None,
    floor=None,
    effective_scope: str = "",
    priority: int = 0,
    rate_percent: str | None = None,
    reviewed_by: str = "user",
) -> dict:
    """人工把候选确认为费率规则；base_type/base_definition 必填（宪章 §五）。

    公式型候选（如"按结算金额的（9-X）%"）无数值比例，确认时必须由人工
    通过 rate_percent 给出按协议解读后的比例。
    """
    if base_type not in BASE_TYPES or base_type == "unset":
        raise ValueError("必须选择计取基数类型（base_type），不能留空")
    if not (base_definition or "").strip():
        raise ValueError("必须填写计取基数说明（base_definition）")
    row = conn.execute(
        "SELECT id, status, rate_percent FROM rate_rules WHERE id=? AND project_id=?",
        (int(rule_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise ValueError(f"费率候选不存在或不属于当前项目：id={rule_id}")
    final_rate = str(rate_percent).strip() if rate_percent is not None else row["rate_percent"]
    if not final_rate:
        raise ValueError("该候选没有数值比例（公式型条款）；确认时必须人工填写比例")
    try:
        to_decimal(final_rate)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"比例不是可识别数值：{final_rate!r}") from exc
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute(
            """UPDATE rate_rules
               SET status='confirmed', base_type=?, base_definition=?, tax_basis=?,
                   cap=?, floor=?, effective_scope=?, priority=?, rate_percent=?,
                   reviewed_at=?, reviewed_by=?, review_reason=?
               WHERE id=?""",
            (base_type, base_definition.strip(), tax_basis,
             None if cap is None else str(cap), None if floor is None else str(floor),
             effective_scope.strip(), int(priority), final_rate,
             now, reviewed_by, base_definition.strip(),
             int(rule_id)),
        )
        evidence_api.add_evidence(
            conn, int(project_id), "rate_rule_review",
            f"费率规则 #{rule_id}：{final_rate}% 确认（基数={base_type}）",
            steps=[{
                "step": "人工确认费率规则",
                "rule_id": int(rule_id),
                "rate_percent": final_rate,
                "base_type": base_type,
                "base_definition": base_definition.strip(),
                "tax_basis": tax_basis,
                "reviewed_by": reviewed_by,
            }],
            sources=[{"rule_id": int(rule_id)}],
            commit=False,
        )
    return {"rule_id": int(rule_id), "status": "confirmed", "rate_percent": final_rate}


def reject_rate_rule(
    conn: sqlite3.Connection,
    project_id: int,
    rule_id: int,
    *,
    reason: str,
    reviewed_by: str = "user",
) -> dict:
    if not (reason or "").strip():
        raise ValueError("拒绝费率候选必须填写理由")
    with conn:
        conn.execute(
            """UPDATE rate_rules SET status='rejected', reviewed_at=?,
                   reviewed_by=?, review_reason=? WHERE id=? AND project_id=?""",
            (datetime.now().isoformat(timespec="seconds"), reviewed_by,
             reason.strip(), int(rule_id), int(project_id)),
        )
    return {"rule_id": int(rule_id), "status": "rejected"}


def apply_rate_rule(
    conn: sqlite3.Connection,
    project_id: int,
    rule_id: int,
    base_amount,
) -> dict:
    """对给定基数做确定性费率试算（仅 confirmed 规则；Decimal 精确）。"""
    row = conn.execute(
        "SELECT * FROM rate_rules WHERE id=? AND project_id=?",
        (int(rule_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise ValueError(f"费率规则不存在或不属于当前项目：id={rule_id}")
    if row["status"] != RATE_CONFIRMED:
        raise ValueError("只有已确认的费率规则才能试算；候选不得参与金额计算")
    base = to_decimal(base_amount)
    rate = to_decimal(row["rate_percent"]) / Decimal("100")
    fee = round2(base * rate)
    detail: dict[str, str] = {"base_amount": str(base), "rate_percent": str(row["rate_percent"])}
    if row["floor"] is not None:
        floor = to_decimal(row["floor"])
        if fee < floor:
            detail["floor_applied"] = str(floor)
            fee = floor
    if row["cap"] is not None:
        cap = to_decimal(row["cap"])
        if fee > cap:
            detail["cap_applied"] = str(cap)
            fee = cap
    with conn:
        ev_id = evidence_api.add_evidence(
            conn, int(project_id), "rate_rule_apply",
            f"费率规则 #{rule_id} 试算：基数 {base} × {row['rate_percent']}% = {fee}",
            steps=[{
                "step": "费率确定性试算",
                "rule_id": int(rule_id),
                **detail,
                "fee": str(fee),
            }],
            sources=[{"rule_id": int(rule_id)}],
            commit=False,
        )
    return {
        "rule_id": int(rule_id),
        "base_amount": str(base),
        "rate_percent": str(row["rate_percent"]),
        "fee": str(fee),
        "detail": detail,
        "evidence_id": ev_id,
    }
