"""框架/管理性协议费率规则（宪章 §五：费率不是一个百分比）。

- 抽取只产候选：触发词命中的句子里的每一个百分比各成一条候选，
  绝不"取第一个百分比"充当答案（多天数/多百分比条款是已知陷阱）；
- 候选确认时必须人工设定 base_type 与 base_definition——"按结算价 3%"
  与"按不含甲供材税前建安费 3%"不是同一条规则；
- 试算用 Decimal 确定性计算，支持上限（cap）/下限（floor）；
- 确认后的规则可按 base_type 从对上/对下期次合计或唯一已确认合同价款
  事实解析基数（resolve_rate_base）：税口径未确认/混用/不一致一律阻断
  （PENDING/INCOMPARABLE），金额缺失行阻断合计（缺失绝不按 0 参与计算），
  多条候选并存时阻断（CONFLICT，不自动挑选）；
- 全部流转与试算尝试（含被阻断的）写审计 Evidence，应用快照只追加不覆盖。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from decimal import Decimal

from jiadun.core.engine.control_baseline import normalize_tax_basis
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

# 基数解析状态：resolved=可确定性解析；其余一律阻断试算（fail-closed）。
BASE_RESOLVED = "resolved"
BASE_PENDING = "pending"            # 事实未确认/数据缺失，待人工补足
BASE_INCOMPARABLE = "incomparable"  # 税口径不一致/混用，不得加总或换算
BASE_CONFLICT = "conflict"          # 多个候选并存，禁止自动挑选
BASE_MANUAL_REQUIRED = "manual_required"  # custom 基数必须人工给出金额

# base_type → 期次方向映射（实体工程量清单算法不在此模块，不混用）。
_BASE_DIRECTION = {
    "upward_settlement_amount": "upward",
    "upward_settlement_excl_tax": "upward",
    "downward_settlement_amount": "downward",
}

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
    tax_basis_canonical = normalize_tax_basis(tax_basis)
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
            (base_type, base_definition.strip(), tax_basis_canonical,
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
                "tax_basis": tax_basis_canonical,
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


def _compute_fee(rate_percent: str, base: Decimal, cap, floor) -> tuple[Decimal, dict[str, str]]:
    """确定性费用计算：base × rate%，先 floor 后 cap（Decimal，round2）。"""
    rate = to_decimal(rate_percent) / Decimal("100")
    fee = round2(base * rate)
    detail: dict[str, str] = {"base_amount": str(base), "rate_percent": str(rate_percent)}
    if floor is not None:
        floor_dec = to_decimal(floor)
        if fee < floor_dec:
            detail["floor_applied"] = str(floor_dec)
            fee = floor_dec
    if cap is not None:
        cap_dec = to_decimal(cap)
        if fee > cap_dec:
            detail["cap_applied"] = str(cap_dec)
            fee = cap_dec
    return fee, detail


def _period_amount_facts(
    conn: sqlite3.Connection, project_id: int, direction: str, period_id: int | None
) -> dict:
    """收集某方向（或指定期次）的期次合计事实：总额、缺失金额行数、税口径集合。"""
    where = "sp.project_id=? AND sp.direction=?"
    params: list[object] = [int(project_id), direction]
    if period_id is not None:
        where += " AND sp.id=?"
        params.append(int(period_id))
    periods = conn.execute(
        f"""SELECT sp.id, sp.period_no, sp.title, sp.tax_mode
            FROM settlement_periods sp WHERE {where} ORDER BY sp.period_no""",
        params,
    ).fetchall()
    breakdown: list[dict] = []
    tax_modes: set[str] = set()
    missing_rows = 0
    total = Decimal("0")
    for p in periods:
        rows = conn.execute(
            "SELECT amount FROM line_items WHERE period_id=?",
            (p["id"],),
        ).fetchall()
        missing = sum(1 for r in rows if r["amount"] is None)
        known = sum(
            (to_decimal(r["amount"]) for r in rows if r["amount"] is not None),
            Decimal("0"),
        )
        missing_rows += missing
        mode = normalize_tax_basis(p["tax_mode"])
        tax_modes.add(mode)
        breakdown.append({
            "period_id": int(p["id"]),
            "period_no": int(p["period_no"]),
            "title": p["title"],
            "tax_mode": mode,
            "detail_rows": len(rows),
            "missing_amount_rows": missing,
            "amount_total": str(known),
        })
        total += known
    return {
        "direction": direction,
        "periods": breakdown,
        "tax_modes": sorted(tax_modes),
        "missing_amount_rows": missing_rows,
        "total": total,
    }


def resolve_rate_base(
    conn: sqlite3.Connection,
    project_id: int,
    rule_id: int,
    *,
    period_id: int | None = None,
    custom_amount=None,
) -> dict:
    """按已确认规则的 base_type 解析计取基数（只产事实，不做费用计算）。

    - custom：必须由人工给出 custom_amount；
    - upward/downward 期次合计：税口径未确认→pending；混用→incomparable；
      与规则税口径不一致→incomparable；存在金额缺失行→pending（缺失≠0）；
    - contract_amount：只有唯一一条已确认合同价款事实才 resolved，
      零条 pending、多条 conflict（禁止自动挑选）。
    """
    row = conn.execute(
        "SELECT * FROM rate_rules WHERE id=? AND project_id=?",
        (int(rule_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise ValueError(f"费率规则不存在或不属于当前项目：id={rule_id}")
    if row["status"] != RATE_CONFIRMED:
        raise ValueError("只有已确认的费率规则才能解析基数；候选不得参与金额计算")
    base_type = str(row["base_type"])
    rule_tax = normalize_tax_basis(row["tax_basis"])
    result: dict = {
        "rule_id": int(rule_id),
        "base_type": base_type,
        "rule_tax_basis": rule_tax,
        "base_amount": None,
        "status": None,
        "reason": "",
        "breakdown": {},
    }

    if base_type == "custom":
        if custom_amount is None or str(custom_amount).strip() == "":
            result["status"] = BASE_MANUAL_REQUIRED
            result["reason"] = "custom 基数必须由人工给出金额；程序不猜测"
        else:
            try:
                base = to_decimal(custom_amount)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"人工基数不是可识别数值：{custom_amount!r}") from exc
            result["status"] = BASE_RESOLVED
            result["base_amount"] = str(base)
            result["breakdown"] = {"custom_amount": str(base)}
        return result

    if base_type in _BASE_DIRECTION:
        direction = _BASE_DIRECTION[base_type]
        facts = _period_amount_facts(conn, int(project_id), direction, period_id)
        result["breakdown"] = facts
        if not facts["periods"]:
            result["status"] = BASE_PENDING
            result["reason"] = f"尚无{('对上' if direction == 'upward' else '对下')}结算期次资料"
            return result
        if facts["missing_amount_rows"] > 0:
            result["status"] = BASE_PENDING
            result["reason"] = (
                f"期次明细存在 {facts['missing_amount_rows']} 行金额缺失；"
                "缺失不按 0 参与合计，请先补齐或人工处理"
            )
            return result
        modes = facts["tax_modes"]
        if "unknown" in modes:
            result["status"] = BASE_PENDING
            result["reason"] = "期次税口径未确认；请先在期次上人工确认含税/不含税口径"
            return result
        if len(modes) > 1:
            result["status"] = BASE_INCOMPARABLE
            result["reason"] = f"期次税口径混用（{('、'.join(modes))}）；不同口径不得直接加总"
            return result
        period_mode = modes[0]
        if base_type == "upward_settlement_excl_tax" and period_mode != "excl_tax":
            # 不含税基数不得从含税合计自动换算——换算需要税率事实，禁止猜测。
            result["status"] = BASE_INCOMPARABLE
            result["reason"] = (
                f"不含税基数要求期次为 excl_tax，当前为 {period_mode}；"
                "禁止用猜测的税率自动换算"
            )
            return result
        if rule_tax != "unknown" and rule_tax != period_mode:
            result["status"] = BASE_INCOMPARABLE
            result["reason"] = f"费率规则税口径（{rule_tax}）与期次口径（{period_mode}）不一致"
            return result
        if rule_tax == "unknown":
            result["status"] = BASE_PENDING
            result["reason"] = "费率规则的税口径未确认；请先在确认时选择含税/不含税"
            return result
        result["status"] = BASE_RESOLVED
        result["base_amount"] = str(facts["total"])
        return result

    if base_type == "contract_amount":
        rows = conn.execute(
            """SELECT cf.id, cf.fact_value, cd.title AS doc_title
               FROM contract_facts cf JOIN contract_docs cd ON cd.id=cf.doc_id
               WHERE cd.project_id=? AND cf.fact_key='contract_amount'
                 AND cf.review_status='confirmed'""",
            (int(project_id),),
        ).fetchall()
        # 占位型事实（fact_value 为空）不是金额事实：不参与挑选，但在
        # breakdown 里如实记录，不静默消失。
        amount_facts: list[tuple[int, Decimal, str]] = []
        excluded: list[dict] = []
        for f in rows:
            try:
                amount_facts.append((int(f["id"]), to_decimal(f["fact_value"]), f["doc_title"]))
            except Exception:  # noqa: BLE001 — 非金额事实显式排除
                excluded.append({"fact_id": int(f["id"]), "doc_title": f["doc_title"],
                                 "reason": "非可解析金额"})
        result["breakdown"] = {
            "confirmed_contract_amount_facts": [
                {"fact_id": fid, "doc_title": title} for fid, _, title in amount_facts
            ],
            "excluded_non_amount_facts": excluded,
        }
        if not amount_facts:
            result["status"] = BASE_PENDING
            result["reason"] = (
                "没有已确认（confirmed）且为可解析金额的合同价款事实；"
                "候选事实或空值事实不得参与计算"
            )
            return result
        if len(amount_facts) > 1:
            result["status"] = BASE_CONFLICT
            result["reason"] = (
                f"存在 {len(amount_facts)} 条已确认合同价款金额事实；"
                "请人工明确以哪份合同为准（系统不自动挑选）"
            )
            return result
        fact_id, base, _ = amount_facts[0]
        result["status"] = BASE_RESOLVED
        result["base_amount"] = str(base)
        result["breakdown"]["fact_id"] = fact_id
        return result

    raise ValueError(f"未知基数类型：{base_type!r}（请重新确认该费率规则）")


def apply_rate_rule(
    conn: sqlite3.Connection,
    project_id: int,
    rule_id: int,
    base_amount,
) -> dict:
    """对人工给定基数做确定性费率试算（仅 confirmed 规则；Decimal 精确）。"""
    row = conn.execute(
        "SELECT * FROM rate_rules WHERE id=? AND project_id=?",
        (int(rule_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise ValueError(f"费率规则不存在或不属于当前项目：id={rule_id}")
    if row["status"] != RATE_CONFIRMED:
        raise ValueError("只有已确认的费率规则才能试算；候选不得参与金额计算")
    base = to_decimal(base_amount)
    fee, detail = _compute_fee(row["rate_percent"], base, row["cap"], row["floor"])
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


def apply_rate_rule_to_settlement(
    conn: sqlite3.Connection,
    project_id: int,
    rule_id: int,
    *,
    period_id: int | None = None,
    custom_amount=None,
) -> dict:
    """按规则 base_type 从项目事实解析基数并试算（框架/管理性协议专用路径）。

    与实体工程量清单算法完全独立：基数只来自期次合计/已确认合同事实/人工
    输入，税口径与缺失数据 fail-closed。基数解析被阻断时不计算费用，
    但仍把阻断事实写入 rate_rule_applications 与 Evidence（失败尝试可审计）。
    """
    resolution = resolve_rate_base(
        conn, int(project_id), int(rule_id),
        period_id=period_id, custom_amount=custom_amount,
    )
    row = conn.execute(
        "SELECT rate_percent, cap, floor, quote_text FROM rate_rules WHERE id=?",
        (int(rule_id),),
    ).fetchone()
    fee: Decimal | None = None
    detail: dict = {}
    if resolution["status"] == BASE_RESOLVED:
        base = to_decimal(resolution["base_amount"])
        fee, detail = _compute_fee(row["rate_percent"], base, row["cap"], row["floor"])
    now = datetime.now().isoformat(timespec="seconds")
    detail_payload = {
        "resolution": resolution["breakdown"],
        "compute": detail,
        "rule_tax_basis": resolution["rule_tax_basis"],
    }
    with conn:
        ev_id = evidence_api.add_evidence(
            conn, int(project_id), "rate_rule_apply_settlement",
            f"费率规则 #{rule_id} 按基数类型 {resolution['base_type']} 试算："
            + (f"基数 {resolution['base_amount']} × {row['rate_percent']}% = {fee}"
               if fee is not None
               else f"未计算（{resolution['status']}：{resolution['reason']}）"),
            steps=[{
                "step": "费率按确认基数试算",
                "rule_id": int(rule_id),
                "base_type": resolution["base_type"],
                "base_status": resolution["status"],
                "base_amount": resolution["base_amount"],
                "rate_percent": str(row["rate_percent"]),
                "fee": None if fee is None else str(fee),
                "reason": resolution["reason"],
                **({"compute": detail} if detail else {}),
            }],
            sources=[{"rule_id": int(rule_id)}],
            commit=False,
        )
        cur = conn.execute(
            """INSERT INTO rate_rule_applications(
                   project_id, rule_id, base_type, base_amount, fee, status,
                   reason, detail_json, evidence_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (int(project_id), int(rule_id), resolution["base_type"],
             resolution["base_amount"], None if fee is None else str(fee),
             resolution["status"], resolution["reason"],
             json.dumps(detail_payload, ensure_ascii=False, sort_keys=True, default=str),
             ev_id, now),
        )
        application_id = int(cur.lastrowid)
    return {
        "application_id": application_id,
        "rule_id": int(rule_id),
        "base_type": resolution["base_type"],
        "base_status": resolution["status"],
        "base_amount": resolution["base_amount"],
        "reason": resolution["reason"],
        "rate_percent": str(row["rate_percent"]),
        "fee": None if fee is None else str(fee),
        "compute_detail": detail,
        "breakdown": resolution["breakdown"],
        "evidence_id": ev_id,
    }


def list_rate_applications(
    conn: sqlite3.Connection, project_id: int, *, rule_id: int | None = None
) -> list[dict]:
    """费率试算应用快照（只追加历史，按时间倒序返回全部，含被阻断尝试）。"""
    sql = """SELECT ra.id, ra.rule_id, ra.base_type, ra.base_amount, ra.fee,
                    ra.status, ra.reason, ra.detail_json, ra.evidence_id,
                    ra.created_at, rr.rate_percent, rr.quote_text, rr.status AS rule_status
             FROM rate_rule_applications ra
             LEFT JOIN rate_rules rr ON rr.id=ra.rule_id
             WHERE ra.project_id=?"""
    params: list[object] = [int(project_id)]
    if rule_id is not None:
        sql += " AND ra.rule_id=?"
        params.append(int(rule_id))
    sql += " ORDER BY ra.id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
