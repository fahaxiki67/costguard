"""全工作簿 Sheet 清单与类型建议（任务书任务 B1/B2/B3）。

- list_workbook_sheets：列出项目（或指定文件）最新批次的**全部** Sheet——
  不只待确认门控页，让用户在几十个 Sheet 里能定位该用哪张（用户反馈#2）。
  SQL 均为两参数静态查询；最新批次解析与筛选在 Python 侧完成。
- suggest_list_kind：按 GB50500 表名与特征词给出清单类型建议
  （分部分项/单价措施/总价措施/汇总页/非业务），附理由；建议只是候选，
  人工确认（set_sheet_list_kind）才落库为标注。
- list_kind 只是内容类型标注，不改变 sheet_status 门控语义。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from jiadun.core.evidence import evidence as evidence_api

LIST_KIND_UNKNOWN = "unknown"
LIST_KIND_BOQ = "boq_detail"            # 分部分项清单
LIST_KIND_MEASURE_UNIT = "measure_unit"  # 单价措施（量×价）
LIST_KIND_MEASURE_TOTAL = "measure_total"  # 总价措施（费率计取）
LIST_KIND_SUMMARY = "summary"           # 汇总/控制页
LIST_KIND_NON_BUSINESS = "non_business"  # 非业务

LIST_KINDS = (
    LIST_KIND_UNKNOWN,
    LIST_KIND_BOQ,
    LIST_KIND_MEASURE_UNIT,
    LIST_KIND_MEASURE_TOTAL,
    LIST_KIND_SUMMARY,
    LIST_KIND_NON_BUSINESS,
)

# GB50500 标准表名与特征词（识别依据，附进建议理由）
_MEASURE_UNIT_PATTERN = re.compile(r"单价措施")
_MEASURE_TOTAL_PATTERN = re.compile(
    r"总价措施|安全文明|安全生产措施|夜间施工|二次搬运|冬雨季|已完工程.{0,6}保护"
)
_BOQ_PATTERN = re.compile(r"分部分项")
_SUMMARY_PATTERN = re.compile(r"汇总|核销|台账|summary|reconciliation|ledger", re.IGNORECASE)
_NON_BUSINESS_PATTERN = re.compile(
    r"封面|目录|编制说明|签字|盖章|审批表|承诺书|营业执照|资质|图纸会审", re.IGNORECASE
)


def suggest_list_kind(sheet_name: str, status_reason: str = "") -> tuple[str, str]:
    """按 Sheet 名与既有状态理由给出清单类型建议与理由（候选，非结论）。"""
    name = str(sheet_name or "")
    text = f"{name} {status_reason or ''}"
    if _MEASURE_UNIT_PATTERN.search(text) or (
        _MEASURE_TOTAL_PATTERN.search(name) and "单价" in name
    ):
        return LIST_KIND_MEASURE_UNIT, "名称/理由命中单价措施特征"
    if _MEASURE_TOTAL_PATTERN.search(text):
        return (
            LIST_KIND_MEASURE_TOTAL,
            "命中总价措施特征（费率计取，通常无数量单价）",
        )
    if _BOQ_PATTERN.search(text):
        return LIST_KIND_BOQ, "名称命中分部分项清单特征"
    if _SUMMARY_PATTERN.search(text):
        return LIST_KIND_SUMMARY, "名称命中汇总/台账特征"
    if _NON_BUSINESS_PATTERN.search(text):
        return LIST_KIND_NON_BUSINESS, "名称命中非业务表特征"
    return LIST_KIND_UNKNOWN, "无可识别特征；请人工判断"


def list_workbook_sheets(
    conn: sqlite3.Connection,
    project_id: int,
    file_id: int | None = None,
    status: str | None = None,
    keyword: str | None = None,
) -> list[dict]:
    """列出项目全部 Sheet（每文件最新批次），附清单类型建议与人工标注。

    file_id=None 时覆盖项目内全部已导入文件；status/keyword 在 Python 侧过滤。
    """
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
            if status is not None and item["sheet_status"] != status:
                continue
            if keyword is not None and keyword not in str(item["sheet_name"]):
                continue
            if item["list_kind"] and item["list_kind"] != LIST_KIND_UNKNOWN:
                # B4：人工标注不能被机器建议覆盖
                item["suggested_kind"] = item["list_kind"]
                item["suggest_reason"] = "已人工标注清单类型"
            else:
                kind, reason = suggest_list_kind(
                    item["sheet_name"], item["sheet_status_reason"]
                )
                item["suggested_kind"] = kind
                if item["sheet_status"] == "confirmed":
                    reason += "（该页已人工确认进入结算模型）"
                item["suggest_reason"] = reason
            result.append(item)
    result.sort(key=lambda item: (item["file_id"], item["sheet_id"]))
    return result


def set_sheet_list_kind(
    conn: sqlite3.Connection,
    project_id: int,
    sheet_id: int,
    kind: str,
    *,
    reviewed_by: str = "user",
    reason: str = "",
) -> dict:
    """人工标注 Sheet 清单类型；理由必填（原则 14），写审计 Evidence。"""
    if kind not in LIST_KINDS:
        raise ValueError(f"未知的清单类型：{kind}")
    if not (reason or "").strip():
        raise ValueError("标注清单类型必须填写理由（写入审计）")
    row = conn.execute(
        """SELECT rs.id, rs.sheet_name, rs.list_kind, sf.project_id
           FROM raw_sheets rs JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE rs.id=? AND sf.project_id=?""",
        (int(sheet_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise ValueError(f"Sheet 不存在或不属于当前项目：sheet_id={sheet_id}")
    before = row["list_kind"] or LIST_KIND_UNKNOWN
    now = datetime.now().isoformat(timespec="seconds")
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
                "before": before,
                "after": kind,
                "reviewed_by": reviewed_by,
                "reason": (reason or "").strip(),
            }],
            sources=[{"sheet_id": int(sheet_id), "sheet_name": row["sheet_name"]}],
            commit=False,
        )
    return {"sheet_id": int(sheet_id), "before": before, "after": kind}
