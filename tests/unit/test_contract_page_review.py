"""PDF 逐页人工对照复核测试（阶段 C-2）。

含 OCR 页的合同停在 needs_review；全部应复核页 verified 后才允许转 parsed；
原文件保持只读；复核决定与理由写入审计 Evidence。
"""

import pytest
from tests.unit.test_pdf_page_pipeline import (
    FakeOcrProvider,
    FakeRenderer,
    _mixed_pages,
    _ocr_result,
    _pdf_copy,
)

from jiadun.core.contracts import extract, page_review, run_contract
from jiadun.core.db import migrations
from jiadun.core.document_intake import list_documents


@pytest.fixture()
def project_db(tmp_path):
    """与页级管线测试相同的库与工程夹具（自包含，避免跨文件调用夹具）。"""
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        project_id = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("页级复核测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
    yield conn, int(project_id), tmp_path
    conn.close()


@pytest.fixture()
def needs_review_contract(project_db):
    """导入混合 PDF（第 2 页 OCR），文档停在 needs_review。"""
    conn, project_id, project_dir = project_db
    source = _pdf_copy(project_dir)
    provider = FakeOcrProvider({2: _ocr_result("付款比例为 80%")})
    extract.import_contract(
        conn, project_id, project_dir, source,
        document_category="upward_contract",
        pdf_renderer=FakeRenderer(_mixed_pages()),
        ocr_provider=provider,
    )
    return conn, project_id, project_dir, source


def _file_id(conn, project_id):
    row = conn.execute(
        "SELECT file_id FROM contract_docs WHERE project_id=? ORDER BY id DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    return int(row["file_id"])


class TestPageListing:
    def test_pages_listed_with_review_requirements(self, needs_review_contract):
        conn, pid, _, _ = needs_review_contract
        fid = _file_id(conn, pid)
        info = page_review.list_pdf_pages(conn, pid, fid)
        assert [p["page_number"] for p in info["pages"]] == [1, 2, 3]
        assert [p["status"] for p in info["pages"]] == ["native_text", "ocr", "native_text"]
        assert info["pages"][1]["requires_review"] is True
        assert info["pages"][0]["requires_review"] is False
        assert info["all_review_pages_verified"] is False
        assert info["pages_requiring_review"] == [2]

    def test_non_pdf_document_rejected(self, project_db):
        conn, pid, _ = project_db
        with pytest.raises(ValueError, match="没有逐页 PDF 提取批次"):
            page_review.list_pdf_pages(conn, pid, 99999)


class TestSetPageReview:
    def test_native_page_cannot_be_reviewed(self, needs_review_contract):
        conn, pid, _, _ = needs_review_contract
        fid = _file_id(conn, pid)
        with pytest.raises(ValueError, match="无需人工复核"):
            page_review.set_page_review(conn, pid, fid, 1, "verified", reason="x")

    def test_verified_requires_reason(self, needs_review_contract):
        conn, pid, _, _ = needs_review_contract
        fid = _file_id(conn, pid)
        with pytest.raises(ValueError, match="对照依据"):
            page_review.set_page_review(conn, pid, fid, 2, "verified", reason="")
        result = page_review.set_page_review(
            conn, pid, fid, 2, "verified", reviewed_by="tester", reason="与原件第2页逐行核对一致"
        )
        assert result["evidence_id"]
        info = page_review.list_pdf_pages(conn, pid, fid)
        assert info["all_review_pages_verified"] is True
        assert info["pages"][1]["decision"] == "verified"

    def test_unknown_page_and_decision(self, needs_review_contract):
        conn, pid, _, _ = needs_review_contract
        fid = _file_id(conn, pid)
        with pytest.raises(ValueError, match="没有第 9 页"):
            page_review.set_page_review(conn, pid, fid, 9, "verified", reason="x")
        with pytest.raises(ValueError, match="未知的页复核决定"):
            page_review.set_page_review(conn, pid, fid, 2, "auto", reason="x")


class TestDocumentGate:
    def test_mark_blocked_until_all_pages_verified(self, needs_review_contract):
        conn, pid, _, _ = needs_review_contract
        fid = _file_id(conn, pid)
        with pytest.raises(ValueError, match="还有应复核页未核实"):
            page_review.mark_document_pages_reviewed(conn, pid, fid)
        page_review.set_page_review(
            conn, pid, fid, 2, "verified", reason="与原件核对一致"
        )
        page_review.mark_document_pages_reviewed(conn, pid, fid)
        intake = {d["file_id"]: d for d in list_documents(conn, pid)}[fid]
        assert intake["parse_status"] == "parsed"
        assert "逐页对照复核完成" in intake["detail"]

    def test_verified_doc_facts_enter_as_candidates(self, needs_review_contract):
        conn, pid, _, _ = needs_review_contract
        fid = _file_id(conn, pid)
        assert run_contract._contract_facts(conn, pid) == []  # needs_review 门控
        page_review.set_page_review(conn, pid, fid, 2, "verified", reason="核对一致")
        page_review.mark_document_pages_reviewed(conn, pid, fid)
        facts = run_contract._contract_facts(conn, pid)
        assert facts, "复核完成的文档候选条款应进入运行契约载荷"
        assert all(f["review_status"] == "candidate" for f in facts)

    def test_evidence_written_for_page_and_document(self, needs_review_contract):
        conn, pid, _, _ = needs_review_contract
        fid = _file_id(conn, pid)
        page_review.set_page_review(conn, pid, fid, 2, "verified", reason="核对一致")
        page_review.mark_document_pages_reviewed(conn, pid, fid)
        kinds = {
            r["kind"] for r in conn.execute(
                "SELECT DISTINCT kind FROM evidence WHERE project_id=?", (pid,)
            ).fetchall()
        }
        assert "pdf_page_review" in kinds
