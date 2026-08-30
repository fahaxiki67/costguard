"""扫描 PDF 必须显式失败（issue #6 现象一）。

无文本层的扫描件曾经静默返回 ok=True + 0 段落 0 事实，用户无法区分
"合同没有该条款"与"根本没读到内容"。现 parse_pdf 在零文本时抛
NotImplementedError（验收执行器 expected_limit 通道与 GUI 失败弹窗
均可正确呈现）。
"""
from __future__ import annotations

import pytest

from costguard.core.contracts.docx_parser import parse_contract

# 最小合法单页空白 PDF（无内容流、无文本层）
MINIMAL_BLANK_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R/Size 4>>\n"
    b"%%EOF\n"
)


def test_scanned_pdf_raises_instead_of_silent_ok(tmp_path):
    p = tmp_path / "scanned.pdf"
    p.write_bytes(MINIMAL_BLANK_PDF)
    with pytest.raises(NotImplementedError, match="扫描件"):
        parse_contract(p, "pdf")
