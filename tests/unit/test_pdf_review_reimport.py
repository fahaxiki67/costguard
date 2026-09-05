"""人工页级复核后的重导入行为测试（宪章：人工确认结果优先）。

背景（2026-09-05 市场真实资料探针 U05 实测发现）：
- 含低置信 OCR 页的扫描合同首次导入停在 needs_review，条款事实未落库；
- 用户逐页人工对照复核完成后文档转 parsed；
- 此时重新导入同一文件会忽略已验证的复核决定，把文档打回
  pending_ocr / needs_review，且条款事实永远无法落库（死循环）。

本文件锁定期望行为：
1. 已验证页的重导入必须尊重人工复核（宪章原则 8：人工确认结果优先）；
2. 事实仍以候选落库（needs_review 标记），不会被静默当作已确认；
3. 未完成复核的文档重导入仍保持 fail-closed，不得借复核通道放行。
"""

from __future__ import annotations

import pytest
from tests.unit.test_pdf_page_pipeline import (
    FakeOcrProvider,
    FakeRenderer,
    _ocr_result,
    _page,
    _pdf_copy,
)

from jiadun.core.contracts import extract, page_review
from jiadun.core.db import migrations
from jiadun.core.document_intake import list_documents
from jiadun.core.parsing.pdf_pipeline import PdfExtractionPending


@pytest.fixture()
def project_db(tmp_path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        project_id = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("复核重导入测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
    yield conn, int(project_id), tmp_path
    conn.close()


def _scan_pages() -> list:
    """两页纯扫描件：第 1 页低置信 OCR，第 2 页高置信 OCR。"""
    return [_page(1, image=True), _page(2, image=True)]


def _scan_provider() -> FakeOcrProvider:
    return FakeOcrProvider({
        1: _ocr_result("合同编号：WH-P051 补19", confidence=0.55),
        2: _ocr_result("合同价款为人民币 1200000 元", confidence=0.95),
    })


def _file_id(conn, project_id) -> int:
    row = conn.execute(
        "SELECT id FROM source_files WHERE project_id=? ORDER BY id DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    return int(row["id"])


def _intake(conn, project_id, file_id) -> dict:
    return {d["file_id"]: d for d in list_documents(conn, project_id)}[file_id]


def _n_facts(conn, project_id) -> int:
    return conn.execute(
        """SELECT COUNT(*) AS n FROM contract_facts cf
           JOIN contract_docs cd ON cd.id=cf.doc_id WHERE cd.project_id=?""",
        (project_id,),
    ).fetchone()["n"]


class TestVerifiedReviewRespectedOnReimport:
    def test_low_confidence_scan_facts_recovered_after_human_review(self, project_db):
        """低置信扫描件：人工逐页复核完成后重导入，事实应以候选落库。"""
        conn, pid, pdir = project_db
        source = _pdf_copy(pdir)

        with pytest.raises(PdfExtractionPending):
            extract.import_contract(
                conn, pid, pdir, source,
                document_category="upward_contract",
                pdf_renderer=FakeRenderer(_scan_pages()),
                ocr_provider=_scan_provider(),
            )
        fid = _file_id(conn, pid)
        assert _intake(conn, pid, fid)["parse_status"] == "needs_review"
        assert _n_facts(conn, pid) == 0  # 门控期不落库（现状正确）

        page_review.set_page_review(conn, pid, fid, 1, "verified", reason="对照原件第1页核对一致")
        page_review.set_page_review(conn, pid, fid, 2, "verified", reason="对照原件第2页核对一致")
        page_review.mark_document_pages_reviewed(conn, pid, fid)
        assert _intake(conn, pid, fid)["parse_status"] == "parsed"

        # 人工复核后重导入：不得再次抛 pending，事实以候选写入
        extract.import_contract(
            conn, pid, pdir, source,
            document_category="upward_contract",
            pdf_renderer=FakeRenderer(_scan_pages()),
            ocr_provider=_scan_provider(),
        )
        intake = _intake(conn, pid, fid)
        assert intake["parse_status"] == "parsed"
        facts = extract.list_contract_facts(conn, pid)
        assert facts, "人工复核解除门控后，扫描件条款事实必须可以落库（候选）"
        assert all(f["review_status"] == "candidate" for f in facts)

    def test_reimport_after_review_keeps_parsed_status(self, project_db):
        """高置信 OCR 文档完成复核转 parsed 后，重导入不得回退为 needs_review。"""
        conn, pid, pdir = project_db
        source = _pdf_copy(pdir)
        pages = [_page(1, "签约合同价为 10000 元"), _page(2, image=True)]
        provider = FakeOcrProvider({2: _ocr_result("付款比例 80%", confidence=0.95)})

        extract.import_contract(
            conn, pid, pdir, source,
            document_category="upward_contract",
            pdf_renderer=FakeRenderer(pages),
            ocr_provider=provider,
        )
        fid = _file_id(conn, pid)
        assert _intake(conn, pid, fid)["parse_status"] == "needs_review"
        page_review.set_page_review(conn, pid, fid, 2, "verified", reason="对照原件核对一致")
        page_review.mark_document_pages_reviewed(conn, pid, fid)
        assert _intake(conn, pid, fid)["parse_status"] == "parsed"

        extract.import_contract(
            conn, pid, pdir, source,
            document_category="upward_contract",
            pdf_renderer=FakeRenderer(pages),
            ocr_provider=FakeOcrProvider({2: _ocr_result("付款比例 80%", confidence=0.95)}),
        )
        assert _intake(conn, pid, fid)["parse_status"] == "parsed"

    def test_partial_review_still_fail_closed(self, project_db):
        """只复核部分应复核页时，重导入必须仍然 pending（不借通道放行）。"""
        conn, pid, pdir = project_db
        source = _pdf_copy(pdir)

        with pytest.raises(PdfExtractionPending):
            extract.import_contract(
                conn, pid, pdir, source,
                document_category="upward_contract",
                pdf_renderer=FakeRenderer(_scan_pages()),
                ocr_provider=_scan_provider(),
            )
        fid = _file_id(conn, pid)
        # 只核实第 2 页，第 1 页（低置信）未复核
        page_review.set_page_review(conn, pid, fid, 2, "verified", reason="对照原件第2页核对一致")

        with pytest.raises(PdfExtractionPending):
            extract.import_contract(
                conn, pid, pdir, source,
                document_category="upward_contract",
                pdf_renderer=FakeRenderer(_scan_pages()),
                ocr_provider=_scan_provider(),
            )
        assert _intake(conn, pid, fid)["parse_status"] == "needs_review"
        assert _n_facts(conn, pid) == 0

    def test_unreviewed_scan_reimport_stays_pending(self, project_db):
        """未做任何复核的扫描件，重导入保持 pending_ocr/needs_review。"""
        conn, pid, pdir = project_db
        source = _pdf_copy(pdir)
        provider = _scan_provider()

        with pytest.raises(PdfExtractionPending):
            extract.import_contract(
                conn, pid, pdir, source,
                document_category="upward_contract",
                pdf_renderer=FakeRenderer(_scan_pages()),
                ocr_provider=provider,
            )
        fid = _file_id(conn, pid)

        with pytest.raises(PdfExtractionPending):
            extract.import_contract(
                conn, pid, pdir, source,
                document_category="upward_contract",
                pdf_renderer=FakeRenderer(_scan_pages()),
                ocr_provider=FakeOcrProvider({
                    1: _ocr_result("合同编号：WH-P051 补19", confidence=0.55),
                    2: _ocr_result("合同价款为人民币 1200000 元", confidence=0.95),
                }),
            )
        assert _intake(conn, pid, fid)["parse_status"] == "needs_review"
