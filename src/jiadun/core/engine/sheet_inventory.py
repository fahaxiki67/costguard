"""全工作簿 Sheet 清单与角色确认（任务书任务 B1-B5）。

- list_workbook_sheets：列出项目（或指定文件）最新批次的**全部** Sheet——
  不只待确认门控页，让用户在几十个 Sheet 里能定位该用哪张（用户反馈#2）。
  附可见状态、行列数、机器建议角色/置信度/理由与人工标注；SQL 均为两参数
  静态查询，最新批次解析与筛选在 Python 侧完成。
- suggest_list_kind：按 GB50500 表名与特征词给出清单类型建议，附理由与
  置信度（高/中/低，启发式分级，不是概率）；建议只是候选，人工确认
  （set_sheet_list_kind）才落库为标注，机器建议永不覆盖人工标注。
- carry_forward_sheet_decisions：重解析后按「同名 + 单元格摘要一致」把
  上一批次的人工决策（sheet_status 与 list_kind）结转到新批次；内容变化
  或找不到同名页一律不结转，宁缺勿错，批量写一条审计 Evidence。
- list_kind 是内容角色标注，不改变 sheet_status 门控语义；但 list_kind
  已纳入 Run Contract sheet_scope（B5），角色变更即签名变化、旧运行失效。
"""
from __future__ import annotations

import re
import sqlite3

from jiadun.core.engine.sheet_digest import sheet_cell_digest
from jiadun.core.evidence import evidence as evidence_api

LIST_KIND_UNKNOWN = "unknown"
LIST_KIND_BOQ = "boq_detail"            # 分部分项清单
LIST_KIND_MEASURE_UNIT = "measure_unit"  # 单价措施（量×价）
LIST_KIND_MEASURE_TOTAL = "measure_total"  # 总价措施（费率计取）
LIST_KIND_UPSTREAM_DETAIL = "upstream_detail"  # 对上明细
LIST_KIND_DOWNSTREAM_DETAIL = "downstream_detail"  # 对下明细
LIST_KIND_OTHER_FEE = "other_fee"       # 其他费用
LIST_KIND_SUMMARY = "summary"           # 汇总/控制页
LIST_KIND_NON_BUSINESS = "non_business"  # 非业务

LIST_KINDS = (
    LIST_KIND_UNKNOWN,
    LIST_KIND_BOQ,
    LIST_KIND_MEASURE_UNIT,
    LIST_KIND_MEASURE_TOTAL,
    LIST_KIND_UPSTREAM_DETAIL,
    LIST_KIND_DOWNSTREAM_DETAIL,
    LIST_KIND_OTHER_FEE,
    LIST_KIND_SUMMARY,
    LIST_KIND_NON_BUSINESS,
)

# 建议参与分析的明细类角色（B2「仅显示建议参与分析」的口径）：
# 汇总/控制页与非业务、未知不算建议参与。
DETAIL_KINDS = (
    LIST_KIND_BOQ,
    LIST_KIND_MEASURE_UNIT,
    LIST_KIND_MEASURE_TOTAL,
    LIST_KIND_UPSTREAM_DETAIL,
    LIST_KIND_DOWNSTREAM_DETAIL,
    LIST_KIND_OTHER_FEE,
)

# 置信度分级（启发式，不是概率；展示原样给用户）
CONFIDENCE_HIGH = "高"
CONFIDENCE_MEDIUM = "中"
CONFIDENCE_LOW = "低"

# 过滤模式（B2）
FILTER_ALL = "all"
FILTER_PENDING = "pending"      # 仅待确认（sheet_status=pending）
FILTER_SUGGESTED = "suggested"  # 仅建议参与分析（建议为明细类角色）
FILTER_MODES = (FILTER_ALL, FILTER_PENDING, FILTER_SUGGESTED)

# GB50500 标准表名与特征词（识别依据，附进建议理由）
_MEASURE_UNIT_PATTERN = re.compile(r"单价措施")
_MEASURE_TOTAL_PATTERN = re.compile(
    r"总价措施|安全文明|安全生产措施|夜间施工|二次搬运|冬雨季|已完工程.{0,6}保护"
)
_BOQ_PATTERN = re.compile(r"分部分项|工程量清单|清单计价")
_UPSTREAM_PATTERN = re.compile(r"对上.{0,4}(结算|台账|明细)|业主.{0,4}结算")
_DOWNSTREAM_PATTERN = re.compile(r"对下.{0,4}(结算|台账|明细)|分包.{0,4}结算|劳务.{0,4}结算")
_OTHER_FEE_PATTERN = re.compile(r"其他费用|其它费用|其他项目费|规费|独立费")
_SUMMARY_PATTERN = re.compile(r"汇总|核销|台账|summary|reconciliation|ledger", re.IGNORECASE)
_NON_BUSINESS_PATTERN = re.compile(
    r"封面|目录|编制说明|签字|盖章|审批表|承诺书|营业执照|资质|图纸会审", re.IGNORECASE
)


def suggest_list_kind(sheet_name: str, status_reason: str = "") -> tuple[str, str, str]:
    """按 Sheet 名与既有状态理由给出清单类型建议、理由与置信度（候选，非结论）。

    置信度是启发式分级：GB50500 特征词命中 Sheet 名为「高」；仅命中状态
    理由文本，或对上/对下/其他费用等弱特征命中为「中」；无特征为「低」。
    """
    name = str(sheet_name or "")
    if _MEASURE_UNIT_PATTERN.search(name) or (
        _MEASURE_TOTAL_PATTERN.search(name) and "单价" in name
    ):
        return LIST_KIND_MEASURE_UNIT, "名称命中单价措施特征", CONFIDENCE_HIGH
    if _MEASURE_TOTAL_PATTERN.search(name):
        return (
            LIST_KIND_MEASURE_TOTAL,
            "命中总价措施特征（费率计取，通常无数量单价）",
            CONFIDENCE_HIGH,
        )
    if _BOQ_PATTERN.search(name):
        return LIST_KIND_BOQ, "名称命中分部分项/工程量清单特征", CONFIDENCE_HIGH
    if _UPSTREAM_PATTERN.search(name):
        return LIST_KIND_UPSTREAM_DETAIL, "名称命中对上结算/台账特征", CONFIDENCE_MEDIUM
    if _DOWNSTREAM_PATTERN.search(name):
        return LIST_KIND_DOWNSTREAM_DETAIL, "名称命中对下/分包结算特征", CONFIDENCE_MEDIUM
    if _OTHER_FEE_PATTERN.search(name):
        return LIST_KIND_OTHER_FEE, "名称命中其他费用特征", CONFIDENCE_MEDIUM
    if _NON_BUSINESS_PATTERN.search(name):
        return LIST_KIND_NON_BUSINESS, "名称命中非业务表特征", CONFIDENCE_MEDIUM
    if _SUMMARY_PATTERN.search(name):
        return LIST_KIND_SUMMARY, "名称命中汇总/台账特征", CONFIDENCE_MEDIUM
    # 名称无特征时退而求其次看既有状态理由（解析器写下的识别备注）
    text = status_reason or ""
    if _MEASURE_TOTAL_PATTERN.search(text):
        return LIST_KIND_MEASURE_TOTAL, "状态备注命中总价措施特征", CONFIDENCE_MEDIUM
    if _BOQ_PATTERN.search(text):
        return LIST_KIND_BOQ, "状态备注命中分部分项特征", CONFIDENCE_MEDIUM
    if _MEASURE_UNIT_PATTERN.search(text):
        return LIST_KIND_MEASURE_UNIT, "状态备注命中单价措施特征", CONFIDENCE_MEDIUM
    return LIST_KIND_UNKNOWN, "无可识别特征；请人工判断", CONFIDENCE_LOW


def list_workbook_sheets(
    conn: sqlite3.Connection,
    project_id: int,
    file_id: int | None = None,
    status: str | None = None,
    keyword: str | None = None,
    filter_mode: str = FILTER_ALL,
) -> list[dict]:
    """列出项目全部 Sheet（每文件最新批次），附建议、置信度与人工标注。

    file_id=None 时覆盖项目内全部已导入文件；status/keyword/filter_mode
    在 Python 侧过滤。历史批次没有 visible_state（解析器当时未捕获），
    该字段为 None，展示层必须作「未知」，不得默认可见。
    """
    if filter_mode not in FILTER_MODES:
        raise ValueError(f"未知的过滤模式：{filter_mode}")
    batch_rows = conn.execute(
        """SELECT pb.file_id, pb.id AS batch_id, sf.original_name
           FROM parse_batches pb
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE sf.project_id=?
           ORDER BY pb.parsed_at DESC, pb.id DESC""",
        (int(project_id),),
    ).fetchall()

    seen_files: set[int] = set()
    batches: list[tuple[int, int, str]] = []
    for row in batch_rows:
        fid = int(row["file_id"])
        if fid in seen_files:
            continue
        if file_id is not None and fid != int(file_id):
            continue
        seen_files.add(fid)
        batches.append((int(row["batch_id"]), fid, str(row["original_name"] or "")))

    result: list[dict] = []
    for batch_id, fid, original_name in batches:
        for row in conn.execute(
            """SELECT rs.id AS sheet_id, rs.sheet_name, rs.n_rows, rs.n_cols,
                      rs.sheet_status, rs.sheet_status_reason, rs.list_kind,
                      rs.visible_state,
                      th.needs_review, th.col_map_json
               FROM raw_sheets rs
               LEFT JOIN table_headers th ON th.sheet_id=rs.id
               WHERE rs.batch_id=?
               ORDER BY rs.sheet_index""",
            (batch_id,),
        ).fetchall():
            item = dict(row)
            item["file_id"] = fid
            item["original_name"] = original_name
            item["batch_id"] = batch_id
            if item["list_kind"] and item["list_kind"] != LIST_KIND_UNKNOWN:
                # B4：人工标注不能被机器建议覆盖
                item["suggested_kind"] = item["list_kind"]
                item["suggest_reason"] = "已人工标注清单类型"
                item["suggest_confidence"] = CONFIDENCE_HIGH
            else:
                kind, reason, confidence = suggest_list_kind(
                    item["sheet_name"], item["sheet_status_reason"]
                )
                item["suggested_kind"] = kind
                item["suggest_confidence"] = confidence
                if item["sheet_status"] == "confirmed":
                    reason += "（该页已人工确认进入结算模型）"
                header_hint = _header_feature_hint(item.get("col_map_json"))
                if header_hint:
                    reason += f"；{header_hint}"
                item["suggest_reason"] = reason
            if status is not None and item["sheet_status"] != status:
                continue
            if keyword is not None and keyword not in str(item["sheet_name"]):
                continue
            if filter_mode == FILTER_PENDING and item["sheet_status"] != "pending":
                continue
            if filter_mode == FILTER_SUGGESTED and item["suggested_kind"] not in DETAIL_KINDS:
                continue
            result.append(item)
    result.sort(key=lambda item: (item["file_id"], item["sheet_id"]))
    return result


def _header_feature_hint(col_map_json: str | None) -> str:
    """从表头列映射提取可读特征提示（如检测到合价/金额列）。"""
    if not col_map_json:
        return ""
    try:
        import json

        col_map = json.loads(col_map_json)
    except (TypeError, ValueError):
        return ""
    if not isinstance(col_map, dict):
        return ""
    labels = " ".join(str(v) for v in col_map.values())
    hints = []
    if "合价" in labels or "总价" in labels or "金额" in labels:
        hints.append("检测到合价/金额列")
    if "单价" in labels:
        hints.append("检测到单价列")
    if "工程量" in labels or "数量" in labels:
        hints.append("检测到工程量/数量列")
    if "税" in labels:
        hints.append("检测到税金相关列")
    return "；".join(hints)


def set_sheet_list_kind(
    conn: sqlite3.Connection,
    project_id: int,
    sheet_id: int,
    kind: str,
    *,
    reviewed_by: str = "user",
    reason: str = "",
) -> dict:
    """人工标注 Sheet 清单类型；理由必填（原则 14），写审计 Evidence。

    Evidence 记录任务书 B5 要求的完整链路：来源文件与哈希、机器建议与
    依据、人工最终角色与理由、时间戳。
    """
    if kind not in LIST_KINDS:
        raise ValueError(f"未知的清单类型：{kind}")
    if not (reason or "").strip():
        raise ValueError("标注清单类型必须填写理由（写入审计）")
    row = conn.execute(
        """SELECT rs.id, rs.sheet_name, rs.list_kind, rs.sheet_status,
                  sf.original_name, sf.sha256
           FROM raw_sheets rs
           JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE rs.id=? AND sf.project_id=?""",
        (int(sheet_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise ValueError(f"Sheet 不存在或不属于当前项目：sheet_id={sheet_id}")
    before = row["list_kind"] or LIST_KIND_UNKNOWN
    suggested_kind, suggest_reason, suggest_confidence = suggest_list_kind(
        row["sheet_name"]
    )
    with conn:
        conn.execute(
            "UPDATE raw_sheets SET list_kind=? WHERE id=?",
            (kind, int(sheet_id)),
        )
        evidence_api.add_evidence(
            conn, int(project_id), "sheet_list_kind",
            f"Sheet「{row['sheet_name']}」清单类型：{before} → {kind}"
            f"（{(reason or '').strip()}）",
            steps=[{
                "step": "人工标注清单类型",
                "sheet_id": int(sheet_id),
                "source_file": row["original_name"],
                "source_sha256": row["sha256"],
                "before": before,
                "after": kind,
                "suggested_kind": suggested_kind,
                "suggest_reason": suggest_reason,
                "suggest_confidence": suggest_confidence,
                "sheet_status": row["sheet_status"],
                "reviewed_by": reviewed_by,
                "reason": (reason or "").strip(),
            }],
            sources=[{
                "sheet_id": int(sheet_id),
                "sheet_name": row["sheet_name"],
                "source_file": row["original_name"],
                "source_sha256": row["sha256"],
            }],
            commit=False,
        )
    return {"sheet_id": int(sheet_id), "before": before, "after": kind}


def carry_forward_sheet_decisions(
    conn: sqlite3.Connection,
    project_id: int,
    file_id: int,
    new_batch_id: int,
    *,
    actor: str = "system",
) -> dict:
    """重解析后把上一批次的人工决策结转到新批次（任务书 B4）。

    结转条件（全部满足才结转，宁缺勿错）：
    - 新旧 Sheet 同名（同名多个时按 sheet_index 精确配对，配不上的不结转）；
    - 两者原始单元格摘要一致（sheet_cell_digest，与 Run Contract 同一实现）
      ——内容变了的 Sheet 必须重新人工确认。

    结转内容：sheet_status 全套字段与 list_kind。批量写一条 Evidence，
    列出每张结转 Sheet 的新旧 id、状态与角色。
    """
    prev_batch = conn.execute(
        """SELECT id FROM parse_batches
           WHERE file_id=? AND id<?
           ORDER BY id DESC LIMIT 1""",
        (int(file_id), int(new_batch_id)),
    ).fetchone()
    if prev_batch is None:
        return {"carried": 0, "skipped_content_changed": 0, "skipped_unmatched": 0}

    def _sheet_rows(batch_id: int) -> dict[str, dict]:
        rows = conn.execute(
            """SELECT id, sheet_name, sheet_index, sheet_status, sheet_status_reason,
                      sheet_status_updated_at, sheet_status_actor, list_kind
               FROM raw_sheets WHERE batch_id=? ORDER BY sheet_index""",
            (int(batch_id),),
        ).fetchall()
        by_name: dict[str, dict] = {}
        for r in rows:
            by_name.setdefault(str(r["sheet_name"]), dict(r))
        return by_name

    old_sheets = _sheet_rows(int(prev_batch["id"]))
    new_rows = conn.execute(
        """SELECT id, sheet_name, sheet_index FROM raw_sheets
           WHERE batch_id=? ORDER BY sheet_index""",
        (int(new_batch_id),),
    ).fetchall()

    carried_steps: list[dict] = []
    skipped_content = 0
    skipped_unmatched = 0
    for new_row in new_rows:
        name = str(new_row["sheet_name"])
        old = old_sheets.get(name)
        if old is None:
            skipped_unmatched += 1
            continue
        old_digest = sheet_cell_digest(conn, int(old["id"]))
        new_digest = sheet_cell_digest(conn, int(new_row["id"]))
        if old_digest != new_digest:
            skipped_content += 1
            continue
        conn.execute(
            """UPDATE raw_sheets SET sheet_status=?, sheet_status_reason=?,
                   sheet_status_updated_at=?, sheet_status_actor=?, list_kind=?
               WHERE id=?""",
            (old["sheet_status"], old["sheet_status_reason"],
             old["sheet_status_updated_at"], old["sheet_status_actor"],
             old["list_kind"], int(new_row["id"])),
        )
        carried_steps.append({
            "step": "重解析人工决策结转",
            "old_sheet_id": int(old["id"]),
            "new_sheet_id": int(new_row["id"]),
            "sheet_name": name,
            "sheet_status": old["sheet_status"],
            "list_kind": old["list_kind"],
            "content_sha256": old_digest,
        })

    summary = {
        "carried": len(carried_steps),
        "skipped_content_changed": skipped_content,
        "skipped_unmatched": skipped_unmatched,
    }
    if carried_steps:
        # 步骤清单截断保护：超大工作簿不产生无界 JSON，截断数在摘要里如实说明
        max_steps = 50
        evidence_steps = carried_steps[:max_steps]
        truncated = len(carried_steps) - len(evidence_steps)
        note = f"（另 {truncated} 张详见数据库）" if truncated > 0 else ""
        evidence_api.add_evidence(
            conn, int(project_id), "sheet_decision_carry_forward",
            f"重解析后人工决策结转：{summary['carried']} 张沿用原确认状态与角色"
            f"；{skipped_content} 张内容变化待重新确认；"
            f"{skipped_unmatched} 张无同名旧页不结转{note}",
            steps=evidence_steps,
            sources=[{
                "file_id": int(file_id),
                "new_batch_id": int(new_batch_id),
                "prev_batch_id": int(prev_batch["id"]),
            }],
            commit=False,
        )
    return summary
