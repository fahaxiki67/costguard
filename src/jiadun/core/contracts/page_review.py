"""PDF 逐页人工对照复核（阶段 C-2）。

含 OCR 页的合同停在 needs_review：候选条款未进入运行契约。用户对照只读
原件逐页核实 OCR 结果后，全部应复核页 verified 的文档才允许转为 parsed；
复核决定与理由写入审计 Evidence，原文件保持只读。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from jiadun.core import document_intake
from jiadun.core.contracts import run_contract
from jiadun.core.evidence import evidence as evidence_api

PAGE_REVIEW_VERIFIED = "verified"
PAGE_REVIEW_NEEDS_REVIEW = "needs_review"
PAGE_DECISIONS = (PAGE_REVIEW_VERIFIED, PAGE_REVIEW_NEEDS_REVIEW)

# 这些页状态的 OCR/混合内容必须经人工对照核实；native_text 页无需复核。
PAGES_REQUIRING_REVIEW = {"needs_review", "ocr"}


def _latest_batch(conn: sqlite3.Connection, project_id: int, file_id: int) -> dict | None:
    row = conn.execute(
        """SELECT pb.id, pb.status, pb.stats_json
           FROM parse_batches pb
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE pb.file_id=? AND sf.project_id=? AND pb.parser='pdf_hybrid'
           ORDER BY pb.parsed_at DESC, pb.id DESC LIMIT 1""",
        (int(file_id), int(project_id)),
    ).fetchone()
    if row is None:
        return None
    try:
        stats = json.loads(row["stats_json"] or "{}")
    except json.JSONDecodeError:
        stats = {}
    return {"batch_id": int(row["id"]), "status": str(row["status"]), "stats": stats}


def _page_decisions(conn: sqlite3.Connection, project_id: int, file_id: int) -> dict[int, dict]:
    rows = conn.execute(
        """SELECT page_number, decision, reviewed_by, reviewed_at, reason
           FROM pdf_page_reviews WHERE project_id=? AND file_id=?""",
        (int(project_id), int(file_id)),
    ).fetchall()
    return {int(r["page_number"]): dict(r) for r in rows}


def _facts_by_page(conn: sqlite3.Connection, project_id: int, file_id: int) -> dict[int, list[dict]]:
    """把该文件的候选合同条款按页归组（依据 Evidence sources 的 page_no）。"""
    rows = conn.execute(
        """SELECT cf.id, cf.fact_key, cf.fact_value, cf.quote_text, cf.review_status,
                  e.sources_json
           FROM contract_facts cf
           JOIN contract_docs cd ON cd.id=cf.doc_id
           JOIN evidence e ON e.id=cf.evidence_id
           WHERE cd.project_id=? AND cd.file_id=?""",
        (int(project_id), int(file_id)),
    ).fetchall()
    by_page: dict[int, list[dict]] = {}
    for row in rows:
        try:
            sources = json.loads(row["sources_json"] or "[]")
        except json.JSONDecodeError:
            sources = []
        page_no = next(
            (
                int(s["page_no"])
                for s in sources
                if isinstance(s, dict) and s.get("page_no") is not None
            ),
            None,
        )
        if page_no is None:
            continue
        by_page.setdefault(page_no, []).append(
            {
                "fact_id": int(row["id"]),
                "fact_key": row["fact_key"],
                "fact_value": row["fact_value"],
                "quote_text": row["quote_text"],
                "review_status": row["review_status"] or "candidate",
            }
        )
    return by_page


def list_pdf_pages(conn: sqlite3.Connection, project_id: int, file_id: int) -> dict:
    """列出该文件的页级提取与人工复核状态；无 PDF 批次时 fail-closed 报告。"""
    batch = _latest_batch(conn, project_id, file_id)
    if batch is None:
        raise ValueError("该资料没有逐页 PDF 提取批次，无法进行页级复核")
    stats = batch["stats"]
    pages = stats.get("pages") or []
    if not stats.get("coverage_complete", False):
        raise ValueError("该批次的页覆盖不完整（缺页/错序），不能进行页级复核")
    decisions = _page_decisions(conn, project_id, file_id)
    facts_by_page = _facts_by_page(conn, project_id, file_id)
    items = []
    for page in pages:
        page_no = int(page["page_no"])
        decision = decisions.get(page_no)
        status = str(page.get("status") or "")
        items.append(
            {
                "page_number": page_no,
                "status": status,
                "extraction_method": page.get("extraction_method"),
                "confidence": page.get("confidence"),
                "requires_review": status in PAGES_REQUIRING_REVIEW,
                "decision": (decision or {}).get("decision"),
                "reviewed_by": (decision or {}).get("reviewed_by"),
                "reviewed_at": (decision or {}).get("reviewed_at"),
                "review_reason": (decision or {}).get("reason"),
                "facts": facts_by_page.get(page_no, []),
            }
        )
    pending = [p["page_number"] for p in items if p["requires_review"] and p["decision"] != PAGE_REVIEW_VERIFIED]
    return {
        "file_id": int(file_id),
        "batch_status": batch["status"],
        "pages": items,
        "pages_requiring_review": sorted(pending),
        "all_review_pages_verified": not pending,
    }


def set_page_review(
    conn: sqlite3.Connection,
    project_id: int,
    file_id: int,
    page_number: int,
    decision: str,
    *,
    reviewed_by: str = "user",
    reason: str = "",
) -> dict:
    """记录单页人工对照复核决定；核实该页必须给出对照依据。"""
    if decision not in PAGE_DECISIONS:
        raise ValueError(f"未知的页复核决定：{decision}")
    info = list_pdf_pages(conn, project_id, file_id)
    page = next((p for p in info["pages"] if p["page_number"] == int(page_number)), None)
    if page is None:
        raise ValueError(f"该批次没有第 {page_number} 页")
    if not page["requires_review"]:
        raise ValueError(f"第 {page_number} 页为原生文本页（{page['status']}），无需人工复核")
    if decision == PAGE_REVIEW_VERIFIED and not (reason or "").strip():
        raise ValueError("核实一页必须填写对照依据（写入审计）")

    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute(
            """INSERT INTO pdf_page_reviews(
                   project_id, file_id, page_number, decision,
                   reviewed_by, reviewed_at, reason, evidence_id)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(project_id, file_id, page_number) DO UPDATE SET
                   decision=excluded.decision,
                   reviewed_by=excluded.reviewed_by,
                   reviewed_at=excluded.reviewed_at,
                   reason=excluded.reason,
                   evidence_id=excluded.evidence_id""",
            (
                int(project_id), int(file_id), int(page_number), decision,
                reviewed_by, now, (reason or "").strip(), None,
            ),
        )
        ev_id = evidence_api.add_evidence(
            conn,
            int(project_id),
            "pdf_page_review",
            f"第 {page_number} 页对照复核：{(decision or '').strip()}"
            + (f"（{(reason or '').strip()}）" if (reason or "").strip() else ""),
            steps=[{
                "step": "人工逐页对照复核",
                "file_id": int(file_id),
                "page_number": int(page_number),
                "page_status": page["status"],
                "decision": decision,
                "reviewed_by": reviewed_by,
                "reason": (reason or "").strip(),
            }],
            sources=[{"file_id": int(file_id), "page_no": int(page_number)}],
            commit=False,
        )
        conn.execute(
            """UPDATE pdf_page_reviews SET evidence_id=?
               WHERE project_id=? AND file_id=? AND page_number=?""",
            (ev_id, int(project_id), int(file_id), int(page_number)),
        )
    return {"page_number": int(page_number), "decision": decision, "evidence_id": ev_id}


def mark_document_pages_reviewed(
    conn: sqlite3.Connection,
    project_id: int,
    file_id: int,
    *,
    reviewed_by: str = "user",
) -> dict:
    """全部应复核页 verified 后，把文档从 needs_review 转为 parsed（fail-closed）。

    - 批次状态必须是 needs_review（pending_ocr/failed 需先重新解析）；
    - 每一个 ocr/needs_review 页都必须已 verified；
    - 转换只改变文档门控状态；条款仍是候选，需按条款逐条确认。
    """
    info = list_pdf_pages(conn, project_id, file_id)
    if info["batch_status"] != "needs_review":
        raise ValueError(
            f"当前批次状态为 {info['batch_status']}；只有 needs_review 文档可以完成页级复核"
        )
    if not info["all_review_pages_verified"]:
        raise ValueError(
            "还有应复核页未核实：" + ",".join(str(n) for n in info["pages_requiring_review"])
        )
    verified_pages = [
        p["page_number"] for p in info["pages"] if p["requires_review"]
    ]
    document_intake.mark_document_status(
        conn, project_id, file_id,
        parse_status="parsed",
        detail=(
            f"人工逐页对照复核完成（第 {','.join(str(n) for n in verified_pages)} 页）；"
            "候选条款需按条款逐条人工确认"
        ),
        parser="pdf_hybrid",
    )
    evidence_api.add_evidence(
        conn,
        int(project_id),
        "pdf_page_review",
        f"文档逐页对照复核完成（第 {','.join(str(n) for n in verified_pages)} 页），"
        "文档门控由 needs_review 转为 parsed",
        steps=[{
            "step": "页级复核完成",
            "file_id": int(file_id),
            "verified_pages": verified_pages,
            "reviewed_by": reviewed_by,
        }],
        sources=[{"file_id": int(file_id)}],
    )
    run_contract.ensure_run_contract(conn, project_id)
    return {"file_id": int(file_id), "verified_pages": verified_pages}
