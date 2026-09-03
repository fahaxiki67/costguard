"""页级混合 PDF/OCR 管线测试。

测试渲染器和 OCR provider 都是本地假实现，不需要下载 PDF 或 OCR 模型；
它们只验证页面覆盖、失败边界、资料台账和 Evidence/Run Contract 门控。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from jiadun.core.contracts import extract, run_contract
from jiadun.core.db import migrations
from jiadun.core.document_intake import list_documents
from jiadun.core.evidence import evidence as evidence_api
from jiadun.core.models.source_file import import_file, sha256_of
from jiadun.core.parsing.pdf_pipeline import (
    TRUSTED_RAPIDOCR_MODEL_FILES,
    TRUSTED_RAPIDOCR_MODEL_SHA256,
    TRUSTED_RAPIDOCR_MODEL_SIZE_BYTES,
    OcrResult,
    PdfExtractionPending,
    PdfPipelineError,
    RenderedPdfPage,
    extract_pdf_document,
    paragraphs_from_report,
)


class FakeRenderSession:
    def __init__(self, pages: list[RenderedPdfPage], page_count: int | None = None):
        self.pages = pages
        self.page_count = len(pages) if page_count is None else page_count

    def __enter__(self) -> FakeRenderSession:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def iter_pages(self):
        yield from self.pages


class FakeRenderer:
    def __init__(self, pages: list[RenderedPdfPage], page_count: int | None = None):
        self.pages = pages
        self.page_count = page_count

    def open(self, path: Path) -> FakeRenderSession:
        return FakeRenderSession(self.pages, self.page_count)


class FakeOcrProvider:
    def __init__(self, results: dict[int, OcrResult]):
        self.results = results
        self.calls: list[int] = []

    def describe(self) -> dict:
        model_files = [
            {
                "name": name,
                "filename": filename,
                "sha256": sha256,
                "size_bytes": size_bytes,
            }
            for name, filename, size_bytes, sha256 in TRUSTED_RAPIDOCR_MODEL_FILES
        ]
        return {
            "id": "rapidocr_onnxruntime",
            "engine": "RapidOCR",
            "engine_version": "1.4.4",
            "model_id": "ch_PP-OCRv4_det-rec_cls",
            "model_version": "PP-OCRv4",
            "model_sha256": TRUSTED_RAPIDOCR_MODEL_SHA256,
            "model_files": model_files,
            "source": "unit-test",
            "license": "test",
            "language": ["zh", "en"],
            "model_size_bytes": TRUSTED_RAPIDOCR_MODEL_SIZE_BYTES,
            "model_downloaded": False,
        }

    def recognize(self, image, *, page_number: int) -> OcrResult:
        self.calls.append(page_number)
        return self.results[page_number]


class ExplodingOcrProvider(FakeOcrProvider):
    def recognize(self, image, *, page_number: int) -> OcrResult:
        raise AssertionError("a reusable PDF batch must not invoke OCR again")


def _page(number: int, text: str = "", *, image: bool = False) -> RenderedPdfPage:
    return RenderedPdfPage(
        page_number=number,
        native_text=text,
        render_image=(lambda: object()) if image else None,
        image_count=1 if image else 0,
    )


def _mixed_pages() -> list[RenderedPdfPage]:
    return [
        _page(1, "合同价款为 10000 元"),
        _page(2, image=True),
        _page(3, "竣工结算审核应在 30 天内完成"),
    ]


@pytest.fixture()
def project_db(tmp_path: Path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        project_id = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("页级管线测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
    yield conn, int(project_id), tmp_path
    conn.close()


def _pdf_copy(tmp_path: Path) -> Path:
    path = tmp_path / "混合合同 副本.pdf"
    path.write_bytes(b"%PDF-1.7\nunit-test-placeholder\n")
    return path


def _ocr_result(text: str, confidence: float = 0.99) -> OcrResult:
    return OcrResult(
        text=text,
        confidence=confidence,
        provider_id="rapidocr_onnxruntime",
        model_id="ch_PP-OCRv4_det-rec_cls",
        model_version="PP-OCRv4",
    )


def test_mixed_pdf_keeps_every_page_and_records_page_metadata(tmp_path: Path):
    source = _pdf_copy(tmp_path)
    provider = FakeOcrProvider({2: _ocr_result("付款比例为 80%")})

    report = extract_pdf_document(
        source,
        renderer=FakeRenderer(_mixed_pages()),
        ocr_provider=provider,
    )

    assert report.page_count == 3
    assert report.coverage_complete is True
    assert report.parse_ready is True
    assert [page.status for page in report.pages] == ["native_text", "ocr", "native_text"]
    assert provider.calls == [2]
    paragraphs = paragraphs_from_report(report)
    assert [paragraph["page_number"] for paragraph in paragraphs] == [1, 2, 3]
    assert any(paragraph["text"] == "付款比例为 80%" for paragraph in paragraphs)
    assert all(paragraph["page_status"] in {"native_text", "ocr"} for paragraph in paragraphs)


def test_mixed_pdf_without_provider_is_pending_but_never_silent(tmp_path: Path):
    source = _pdf_copy(tmp_path)

    with pytest.raises(PdfExtractionPending) as raised:
        extract_pdf_document(source, renderer=FakeRenderer(_mixed_pages()))

    report = raised.value.report
    assert report.coverage_complete is True
    assert report.parse_ready is False
    assert [page.status for page in report.pages] == ["native_text", "pending_ocr", "native_text"]
    assert "2:pending_ocr" in str(raised.value)


@pytest.mark.parametrize(
    ("pages", "page_count"),
    [
        ([_page(1, "第一页")], 2),
        ([_page(2, "第二页"), _page(1, "第一页")], 2),
        ([_page(1, "第一页"), _page(2, "第二页"), _page(3, "多出的一页")], 2),
    ],
)
def test_page_coverage_errors_fail_closed(
    tmp_path: Path, pages: list[RenderedPdfPage], page_count: int
):
    source = _pdf_copy(tmp_path)

    with pytest.raises(PdfPipelineError) as raised:
        extract_pdf_document(
            source,
            renderer=FakeRenderer(pages, page_count=page_count),
        )

    assert raised.value.report is not None
    assert raised.value.report.coverage_complete is False
    assert raised.value.report.parse_ready is False


def test_low_confidence_ocr_is_review_only(tmp_path: Path):
    source = _pdf_copy(tmp_path)
    provider = FakeOcrProvider({2: _ocr_result("付款比例为 80%", confidence=0.35)})

    with pytest.raises(PdfExtractionPending) as raised:
        extract_pdf_document(
            source,
            renderer=FakeRenderer(_mixed_pages()),
            ocr_provider=provider,
        )

    page = raised.value.report.pages[1]
    assert page.status == "needs_review"
    assert page.text == "付款比例为 80%"
    assert raised.value.report.parse_ready is False


def test_native_text_with_images_is_review_only(tmp_path: Path):
    source = _pdf_copy(tmp_path)
    provider = FakeOcrProvider({1: _ocr_result("不应调用")})

    with pytest.raises(PdfExtractionPending) as raised:
        extract_pdf_document(
            source,
            renderer=FakeRenderer([_page(1, "页眉文本", image=True)]),
            ocr_provider=provider,
        )

    page = raised.value.report.pages[0]
    assert page.status == "needs_review"
    assert page.extraction_method == "native_text_with_images"
    assert page.image_count == 1
    assert provider.calls == []


def test_contract_import_persists_pdf_batch_evidence_and_gates_ocr_facts(project_db):
    conn, project_id, project_dir = project_db
    source = _pdf_copy(project_dir)
    before = sha256_of(source)
    provider = FakeOcrProvider({2: _ocr_result("付款比例为 80%")})

    doc_id = extract.import_contract(
        conn,
        project_id,
        project_dir,
        source,
        document_category="upward_contract",
        pdf_renderer=FakeRenderer(_mixed_pages()),
        ocr_provider=provider,
    )

    assert sha256_of(source) == before
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM contract_docs WHERE id=?", (doc_id,)
    ).fetchone()["n"] == 1
    intake = list_documents(conn, project_id)[0]
    assert intake["parse_status"] == "needs_review"
    assert intake["parser"] == "pdf_hybrid"
    batch = conn.execute(
        """SELECT status, stats_json FROM parse_batches
           WHERE file_id=(SELECT file_id FROM contract_docs WHERE id=?)
           ORDER BY id DESC LIMIT 1""",
        (doc_id,),
    ).fetchone()
    assert batch["status"] == "needs_review"
    stats = json.loads(batch["stats_json"])
    assert stats["coverage_complete"] is True
    assert stats["parse_ready"] is True
    assert [page["status"] for page in stats["pages"]] == ["native_text", "ocr", "native_text"]

    evidence_rows = conn.execute(
        """SELECT e.sources_json FROM evidence e
           JOIN contract_facts cf ON cf.evidence_id=e.id
           WHERE cf.doc_id=?""",
        (doc_id,),
    ).fetchall()
    sources = [json.loads(row["sources_json"])[0] for row in evidence_rows]
    ocr_sources = [source for source in sources if source.get("page_no") == 2]
    assert ocr_sources
    assert ocr_sources[0]["file_id"]
    assert ocr_sources[0]["source_sha256"] == before
    assert ocr_sources[0]["page_status"] == "ocr"
    assert ocr_sources[0]["extraction_method"] == "ocr"
    assert ocr_sources[0]["ocr_provider"] == "rapidocr_onnxruntime"
    assert ocr_sources[0]["ocr_model"] == "ch_PP-OCRv4_det-rec_cls"

    # OCR 候选可以留在合同事实/Evidence 供人工复核，但 needs_review 文档
    # 不得进入当前 Run Contract 的计算输入。
    assert run_contract._contract_facts(conn, project_id) == []
    assert extract.contract_risks(conn, project_id) == []


def test_pending_mixed_pdf_persists_all_pages_and_preserves_source(project_db):
    conn, project_id, project_dir = project_db
    source = _pdf_copy(project_dir)
    before = sha256_of(source)

    with pytest.raises(NotImplementedError):
        extract.import_contract(
            conn,
            project_id,
            project_dir,
            source,
            pdf_renderer=FakeRenderer(_mixed_pages()),
        )

    assert sha256_of(source) == before
    intake = list_documents(conn, project_id)[0]
    assert intake["parse_status"] == "pending_ocr"
    assert intake["parser"] == "pdf_hybrid"
    assert conn.execute("SELECT COUNT(*) AS n FROM contract_docs").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM contract_facts").fetchone()["n"] == 0
    batch = conn.execute(
        "SELECT status, stats_json FROM parse_batches ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert batch["status"] == "pending_ocr"
    assert [page["page_no"] for page in json.loads(batch["stats_json"])["pages"]] == [1, 2, 3]


def test_current_complete_pdf_batch_is_reused_without_duplicate_facts(project_db):
    conn, project_id, project_dir = project_db
    source = _pdf_copy(project_dir)
    provider = FakeOcrProvider({2: _ocr_result("付款比例为 80%")})
    first_id = extract.import_contract(
        conn,
        project_id,
        project_dir,
        source,
        pdf_renderer=FakeRenderer(_mixed_pages()),
        ocr_provider=provider,
    )

    second_id = extract.import_contract(
        conn,
        project_id,
        project_dir,
        source,
        pdf_renderer=FakeRenderer(_mixed_pages()),
        ocr_provider=ExplodingOcrProvider({2: _ocr_result("不应调用")}),
    )

    assert second_id == first_id
    assert conn.execute("SELECT COUNT(*) AS n FROM contract_docs").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM contract_facts").fetchone()["n"] > 0


def test_tampered_pdf_batch_snapshot_is_not_reused(project_db):
    conn, project_id, project_dir = project_db
    source = _pdf_copy(project_dir)
    extract.import_contract(
        conn,
        project_id,
        project_dir,
        source,
        pdf_renderer=FakeRenderer(_mixed_pages()),
        ocr_provider=FakeOcrProvider({2: _ocr_result("付款比例为 80%")}),
    )
    file_id = conn.execute(
        "SELECT id FROM source_files WHERE project_id=?", (project_id,)
    ).fetchone()["id"]
    row = conn.execute(
        "SELECT id, stats_json FROM parse_batches WHERE file_id=? ORDER BY id DESC LIMIT 1",
        (file_id,),
    ).fetchone()
    stats = json.loads(row["stats_json"])
    stats["page_status_counts"]["ocr"] = 0
    with conn:
        conn.execute(
            "UPDATE parse_batches SET stats_json=? WHERE id=?",
            (json.dumps(stats, ensure_ascii=False), row["id"]),
        )

    assert extract._reusable_pdf_batch(conn, file_id, sha256_of(source)) is None


def test_tampered_ocr_model_metadata_is_not_reused(project_db):
    conn, project_id, project_dir = project_db
    source = _pdf_copy(project_dir)
    extract.import_contract(
        conn,
        project_id,
        project_dir,
        source,
        pdf_renderer=FakeRenderer(_mixed_pages()),
        ocr_provider=FakeOcrProvider({2: _ocr_result("付款比例为 80%")}),
    )
    row = conn.execute(
        "SELECT id, stats_json FROM parse_batches ORDER BY id DESC LIMIT 1"
    ).fetchone()
    stats = json.loads(row["stats_json"])
    stats["ocr_provider"]["model_sha256"] = "0" * 64

    assert not extract._valid_pdf_batch_stats(
        stats,
        source_sha256=sha256_of(source),
        status="needs_review",
    )


def test_self_consistent_noncanonical_ocr_manifest_is_not_reused(project_db):
    conn, project_id, project_dir = project_db
    source = _pdf_copy(project_dir)
    extract.import_contract(
        conn,
        project_id,
        project_dir,
        source,
        pdf_renderer=FakeRenderer(_mixed_pages()),
        ocr_provider=FakeOcrProvider({2: _ocr_result("付款比例为 80%")}),
    )
    row = conn.execute(
        "SELECT stats_json FROM parse_batches ORDER BY id DESC LIMIT 1"
    ).fetchone()
    stats = json.loads(row["stats_json"])
    stats["ocr_provider"]["model_files"][0]["filename"] = "models/replaced.onnx"
    digest = hashlib.sha256()
    for model in stats["ocr_provider"]["model_files"]:
        digest.update(f"{model['filename']}:{model['sha256']}\n".encode())
    stats["ocr_provider"]["model_sha256"] = digest.hexdigest()

    assert not extract._valid_pdf_batch_stats(
        stats,
        source_sha256=sha256_of(source),
        status="needs_review",
    )


def test_old_pdf_projection_is_retired_before_retry(project_db):
    conn, project_id, project_dir = project_db
    source = _pdf_copy(project_dir)
    original_hash = sha256_of(source)
    source_file = import_file(conn, project_id, project_dir, source)
    with conn:
        evidence_id = evidence_api.add_evidence(
            conn,
            project_id,
            "contract_fact",
            "旧版部分解析候选",
            sources=[{"file_id": source_file.file_id, "source_sha256": original_hash}],
            commit=False,
        )
        doc_id = conn.execute(
            """INSERT INTO contract_docs(project_id, file_id, doc_type, title, parsed_at)
               VALUES (?,?,?,?,?)""",
            (project_id, source_file.file_id, "legacy", "旧版合同", "2026"),
        ).lastrowid
        conn.execute(
            """INSERT INTO contract_facts(
                   doc_id, fact_key, fact_value, quote_text, location, confidence, evidence_id
               ) VALUES (?,?,?,?,?,?,?)""",
            (doc_id, "payment_clause", "80%", "旧版片段", "p1", 0.9, evidence_id),
        )
        from jiadun.core.document_intake import record_document

        record_document(
            conn, project_id, source_file.file_id, category="upward_contract",
            parse_status="parsed", parser="legacy", commit=False,
        )
    risks = extract.contract_risks(conn, project_id)
    assert risks
    extract.persist_risks(conn, project_id, risks)
    old_risk = conn.execute(
        """SELECT id, evidence_id FROM anomalies
           WHERE project_id=? AND rule_id='contract_risk' AND subject_id=?
           ORDER BY id LIMIT 1""",
        (project_id, doc_id),
    ).fetchone()
    assert old_risk is not None

    with pytest.raises(NotImplementedError):
        extract.import_contract(
            conn,
            project_id,
            project_dir,
            source,
            pdf_renderer=FakeRenderer(_mixed_pages()),
        )

    assert sha256_of(source) == original_hash
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM contract_docs WHERE file_id=?", (source_file.file_id,)
    ).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM contract_facts WHERE doc_id=?", (doc_id,)
    ).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT scope FROM evidence WHERE id=?", (evidence_id,)
    ).fetchone()["scope"] == "historical"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM evidence_events WHERE evidence_id=?", (evidence_id,)
    ).fetchone()["n"] == 1
    retired_risk = conn.execute(
        "SELECT status, lifecycle_status FROM anomalies WHERE id=?", (old_risk["id"],)
    ).fetchone()
    assert retired_risk["status"] == "stale"
    assert retired_risk["lifecycle_status"] == "historical"
    assert conn.execute(
        "SELECT scope FROM evidence WHERE id=?", (old_risk["evidence_id"],)
    ).fetchone()["scope"] == "historical"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM finding_status_events WHERE anomaly_id=?",
        (old_risk["id"],),
    ).fetchone()["n"] == 1


def test_legacy_contract_without_intake_is_excluded_fail_closed(project_db):
    conn, project_id, project_dir = project_db
    source = _pdf_copy(project_dir)
    source_file = import_file(conn, project_id, project_dir, source)
    with conn:
        doc_id = conn.execute(
            """INSERT INTO contract_docs(project_id, file_id, doc_type, title, parsed_at)
               VALUES (?,?,?,?,?)""",
            (project_id, source_file.file_id, "legacy", "无 intake 合同", "2026"),
        ).lastrowid
        conn.execute(
            """INSERT INTO contract_facts(
                   doc_id, fact_key, fact_value, quote_text, location, confidence
               ) VALUES (?,?,?,?,?,?)""",
            (doc_id, "payment_clause", "80%", "旧版片段", "p1", 0.9),
        )

    assert run_contract._contract_facts(conn, project_id) == []
    assert extract.contract_risks(conn, project_id) == []


def test_failed_pdf_retry_invalidates_an_existing_run_contract(project_db):
    conn, project_id, project_dir = project_db
    source = _pdf_copy(project_dir)
    original_hash = sha256_of(source)
    source_file = import_file(conn, project_id, project_dir, source)
    with conn:
        evidence_id = evidence_api.add_evidence(
            conn,
            project_id,
            "contract_fact",
            "旧版当前合同事实",
            sources=[{"file_id": source_file.file_id, "source_sha256": original_hash}],
            commit=False,
        )
        doc_id = conn.execute(
            """INSERT INTO contract_docs(project_id, file_id, doc_type, title, parsed_at)
               VALUES (?,?,?,?,?)""",
            (project_id, source_file.file_id, "legacy", "旧版合同", "2026"),
        ).lastrowid
        conn.execute(
            """INSERT INTO contract_facts(
                   doc_id, fact_key, fact_value, quote_text, location, confidence, evidence_id
               ) VALUES (?,?,?,?,?,?,?)""",
            (doc_id, "payment_clause", "80%", "旧版片段", "p1", 0.9, evidence_id),
        )
        from jiadun.core.document_intake import record_document

        record_document(
            conn, project_id, source_file.file_id, category="upward_contract",
            parse_status="parsed", parser="contract_text", commit=False,
        )
    old_contract = run_contract.ensure_run_contract(conn, project_id)
    assert old_contract.components["contract_facts"]

    with pytest.raises(NotImplementedError):
        extract.import_contract(
            conn,
            project_id,
            project_dir,
            source,
            pdf_renderer=FakeRenderer(_mixed_pages()),
        )

    current = run_contract.get_current_contract(conn, project_id)
    assert current is not None
    assert current.run_id != old_contract.run_id
    assert current.components["contract_facts"] == []
    assert conn.execute(
        "SELECT invalidated_at FROM run_contracts WHERE run_id=?", (old_contract.run_id,)
    ).fetchone()["invalidated_at"] is not None
    assert sha256_of(source) == original_hash


def test_source_hash_helper_is_sha256_not_business_content(tmp_path: Path):
    source = _pdf_copy(tmp_path)
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    assert sha256_of(source) == expected


def test_ocr_model_manifest_mismatch_fails_closed(tmp_path: Path):
    from jiadun.platform import ocr

    model = tmp_path / "model.onnx"
    model.write_bytes(b"test-model")

    with pytest.raises(ocr.OcrProviderUnavailable, match="模型文件校验失败"):
        ocr._verify_model_file(
            model,
            "det",
            expected_size=1,
            expected_sha256="0" * 64,
        )
