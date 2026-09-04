"""PDF/Word 结算清单导入：fail-closed 转待人工处理，不得包装成莫名失败（用户反馈#1）。"""

from pathlib import Path

import docx as docx_lib
import pytest

from jiadun.core.db import migrations
from jiadun.core.document_intake import list_documents
from jiadun.core.engine import settlement_io


@pytest.fixture()
def project_db(tmp_path):
    db_path = tmp_path / "project.db"
    migrations.migrate(db_path, tmp_path / "backups")
    conn = migrations.connect(db_path)
    with conn:
        pid = conn.execute(
            """INSERT INTO projects(name, schema_version, workspace_path, created_at)
               VALUES (?,?,?,?)""",
            ("PDF结算导入测试", migrations.LATEST_SCHEMA_VERSION, str(tmp_path), "2026"),
        ).lastrowid
    yield conn, int(pid), tmp_path
    conn.close()


def _pdf(tmp_path: Path, name: str = "结算表.pdf") -> Path:
    p = tmp_path / name
    p.write_bytes(b"%PDF-1.7\nunit-test placeholder\n")
    return p


class TestUnsupportedSettlementTypes:
    def test_pdf_import_is_needs_review_not_failed(self, project_db):
        conn, pid, tmp = project_db
        report = settlement_io.import_settlement_file(
            conn, pid, tmp, _pdf(tmp), direction="upward",
            document_category="upward_settlement",
        )
        assert report.status == "partial"
        assert report.needs_manual_review is True
        assert report.sheets[0].state_code == "pending"
        assert "另存为 Excel/CSV" in report.sheets[0].notes[0]
        doc = list_documents(conn, pid)[0]
        assert doc["parse_status"] == "needs_review"
        assert "暂不支持自动表格解析" in doc["detail"]

    def test_guidance_evidence_recorded(self, project_db):
        conn, pid, tmp = project_db
        settlement_io.import_settlement_file(
            conn, pid, tmp, _pdf(tmp), document_category="upward_settlement"
        )
        kinds = {
            r["kind"] for r in conn.execute(
                "SELECT DISTINCT kind FROM evidence WHERE project_id=?", (pid,)
            ).fetchall()
        }
        assert "parse_failure" in kinds

    def test_docx_also_guided(self, project_db):
        conn, pid, tmp = project_db
        p = tmp / "结算.docx"
        docx_lib.Document().save(str(p))
        report = settlement_io.import_settlement_file(
            conn, pid, tmp, p, document_category="upward_settlement"
        )
        assert report.status == "partial"
        assert report.sheets[0].state_code == "pending"
