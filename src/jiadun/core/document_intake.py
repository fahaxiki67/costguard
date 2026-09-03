"""资料接收台账：分类是人工可复核的元数据，不从文件名推断业务事实。

这个模块只登记资料的来源、用户确认的类别、处理状态及错误边界。它不抽取
金额、不匹配清单，也不改变原始文件；结算/合同的实际解析仍由各自的 core
模块负责。这样“付款台账”“审计报告”等资料不会被误投进结算计算模型。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DocumentCategory:
    code: str
    label: str
    direction: str = "unknown"
    parse_strategy: str = "evidence_only"  # settlement / contract / evidence_only
    description: str = ""


# 顺序即资料中心推荐的接收顺序；它不构成导入前置条件。
DOCUMENT_CATEGORIES: tuple[DocumentCategory, ...] = (
    DocumentCategory("unclassified", "待人工分类", description="仅登记并保留原件，不自动形成业务结论"),
    DocumentCategory("upward_bid", "对上中标及投标文件", direction="upward", description="招投标/中标资料"),
    DocumentCategory("upward_contract", "对上合同（含补充协议）", direction="upward", parse_strategy="contract", description="优先提取可追溯合同条款"),
    DocumentCategory("upward_framework_management", "对上框架/管理性协议", direction="upward", description="协作费率、计取基数、税口径及适用条件的控制规则候选；须人工确认后才参与可复算控制"),
    DocumentCategory("meeting_minutes", "会议纪要/补充约定", description="可能补充、变更或解释合同条款；须人工确认适用范围和优先级"),
    DocumentCategory("other_agreement", "其他关联协议/文件", description="未能归入合同、框架管理协议或纪要的关联资料"),
    DocumentCategory("upward_settlement", "对上结算资料", direction="upward", parse_strategy="settlement", description="结算清单、送审/审定资料"),
    DocumentCategory("upward_audit_report", "对上竣工结算/终审（审计/审核）报告", direction="upward", parse_strategy="control_candidate", description="含工程量/金额清单时作为对上控制基准候选；需确认范围、税口径、变更和版本后才可作为上限预警"),
    DocumentCategory("upward_receipt_ledger", "对上财务收款情况/台账", direction="upward", description="财务收款与台账资料"),
    DocumentCategory("downward_payment_ledger", "对下资金支付台账", direction="downward", description="支付及已结算金额台账"),
    DocumentCategory("downward_material_settlement", "对下物资结算/已完工未结算", direction="downward", parse_strategy="settlement", description="物资结算及已完工未结算清单"),
    DocumentCategory("downward_subcontract_settlement", "对下分包结算", direction="downward", parse_strategy="settlement", description="分包结算清单"),
)

_CATEGORIES = {item.code: item for item in DOCUMENT_CATEGORIES}
_PARSE_STATUSES = frozenset({
    "registered", "processing", "parsed", "needs_review", "pending_ocr", "control_candidate",
    "evidence_only", "failed",
})
_CLASSIFICATION_STATUSES = frozenset({"unclassified", "user_confirmed"})


def category_for(code: str | None) -> DocumentCategory:
    """返回受控类别；未知值一律 fail-closed 为待人工分类。"""
    return _CATEGORIES.get(str(code or ""), _CATEGORIES["unclassified"])


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _validate_status(value: str, allowed: frozenset[str], label: str) -> str:
    if value not in allowed:
        raise ValueError(f"invalid document intake {label}: {value!r}")
    return value


def record_document(
    conn: sqlite3.Connection,
    project_id: int,
    file_id: int,
    *,
    category: str | None = None,
    classification_status: str | None = None,
    parse_status: str | None = None,
    detail: str | None = None,
    parser: str | None = None,
    commit: bool = True,
) -> None:
    """插入或更新资料接收状态，并校验 ``file_id`` 的项目归属。"""
    item = category_for(category)
    classification = classification_status or (
        "unclassified" if item.code == "unclassified" else "user_confirmed"
    )
    status = parse_status or {
        "evidence_only": "evidence_only",
        "control_candidate": "control_candidate",
    }.get(item.parse_strategy, "registered")
    _validate_status(classification, _CLASSIFICATION_STATUSES, "classification status")
    _validate_status(status, _PARSE_STATUSES, "parse status")
    source = conn.execute(
        "SELECT id FROM source_files WHERE id=? AND project_id=?", (int(file_id), int(project_id))
    ).fetchone()
    if source is None:
        raise ValueError("资料文件不属于当前项目，拒绝登记")
    params = (
        int(file_id), int(project_id), item.code, classification, item.direction,
        status, str(detail or ""), str(parser or ""), now_text(),
    )
    conn.execute(
        """INSERT INTO document_intake
           (file_id, project_id, category, classification_status, direction,
            parse_status, detail, parser, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(file_id) DO UPDATE SET
             category=excluded.category,
             classification_status=excluded.classification_status,
             direction=excluded.direction,
             parse_status=excluded.parse_status,
             detail=excluded.detail,
             parser=excluded.parser,
             updated_at=excluded.updated_at""",
        params,
    )
    if commit and not conn.in_transaction:
        conn.commit()


def mark_document_status(
    conn: sqlite3.Connection,
    project_id: int,
    file_id: int,
    *,
    parse_status: str,
    detail: str = "",
    parser: str = "",
    commit: bool = True,
) -> None:
    """更新已有资料状态；缺少分类记录时保守补为待人工分类。"""
    _validate_status(parse_status, _PARSE_STATUSES, "parse status")
    existing = conn.execute(
        "SELECT category, classification_status FROM document_intake WHERE file_id=? AND project_id=?",
        (int(file_id), int(project_id)),
    ).fetchone()
    record_document(
        conn, project_id, file_id,
        category=existing["category"] if existing else "unclassified",
        classification_status=existing["classification_status"] if existing else "unclassified",
        parse_status=parse_status, detail=detail, parser=parser, commit=commit,
    )


def list_documents(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    """返回资料中心所需只读台账；历史资料无元数据时显式标为待人工分类。"""
    return conn.execute(
        """SELECT sf.id AS file_id, sf.original_name, sf.original_path, sf.stored_path,
                  sf.sha256, sf.file_type, sf.size_bytes, sf.imported_at,
                  COALESCE(di.category, 'unclassified') AS category,
                  COALESCE(di.classification_status, 'unclassified') AS classification_status,
                  COALESCE(di.direction, 'unknown') AS direction,
                  COALESCE(di.parse_status, 'registered') AS parse_status,
                  COALESCE(di.detail, '') AS detail, COALESCE(di.parser, '') AS parser,
                  COALESCE(di.updated_at, sf.imported_at) AS updated_at,
                  EXISTS(SELECT 1 FROM contract_docs cd WHERE cd.file_id=sf.id) AS has_contract_doc,
                  (SELECT pb.status FROM parse_batches pb WHERE pb.file_id=sf.id
                   ORDER BY pb.parsed_at DESC, pb.id DESC LIMIT 1) AS parser_batch_status
           FROM source_files sf
           LEFT JOIN document_intake di ON di.file_id=sf.id AND di.project_id=sf.project_id
           WHERE sf.project_id=? ORDER BY sf.imported_at DESC, sf.id DESC""",
        (int(project_id),),
    ).fetchall()


__all__ = [
    "DOCUMENT_CATEGORIES", "DocumentCategory", "category_for", "list_documents",
    "mark_document_status", "record_document",
]
