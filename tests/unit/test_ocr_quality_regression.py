"""OCR 质量回归用例集：页级状态流转矩阵的门控行为防回归。

背景（2026-09 三轮实测沉淀）：页级状态流转是 fail-closed 的核心边界——
低质量页必须落在 ``needs_review`` 进入人工确认，不允许静默降级为可用文本，
也不允许因为整份 PDF 前几页有文本就把扫描页漏掉。

本用例集把「输入页形态 → 页状态」的全矩阵固化为命名用例：

- ``native_text``   纯文本层页（无图片）→ 可解析
- ``ocr``           无文本层 + OCR 成功且置信度 ≥ 阈值且模型身份齐全 → 可解析
- ``pending_ocr``   无文本层且无可用 provider → 显式待处理
- ``ocr_failed``    渲染图像不可得 / provider 异常 → 显式失败
- ``needs_review``  文本层+图片并存 / OCR 空文本 / 低置信度 / 模型身份缺失
                    → 人工确认，绝不静默当作已解析

另有覆盖完整性（缺页/多页/乱序 fail-closed）、数据结构不变量
（置信度越界拒绝、可解析页空文本拒绝）和统计口径（status_counts）用例。

最后一个用例是 RapidOCR 实机质量回归：本机安装了受信任的
rapidocr-onnxruntime 1.4.4 随包模型时才运行（真实渲染 + 真实识别），
CI 或未安装环境下跳过并注明原因，不用假结果冒充实机通过。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from jiadun.core.parsing.pdf_pipeline import (
    OCR_CONFIDENCE_THRESHOLD,
    PAGE_STATUSES,
    PARSEABLE_PAGE_STATUSES,
    OcrResult,
    PageExtractionResult,
    PdfExtractionPending,
    PdfPipelineError,
    RenderedPdfPage,
    extract_pdf_document,
    paragraphs_from_report,
)

# ---------------------------------------------------------------------------
# 测试基建：本地假渲染器 / 假 provider（沿用 test_pdf_page_pipeline 的口径）
# ---------------------------------------------------------------------------


class FakeRenderSession:
    def __init__(self, pages: list[RenderedPdfPage], page_count: int):
        self.pages = pages
        self.page_count = page_count

    def __enter__(self) -> FakeRenderSession:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def iter_pages(self):
        yield from self.pages


class FakeRenderer:
    def __init__(self, pages: list[RenderedPdfPage], page_count: int | None = None):
        self.pages = pages
        self.page_count = len(pages) if page_count is None else page_count

    def open(self, path: Path) -> FakeRenderSession:
        return FakeRenderSession(self.pages, self.page_count)


class ScriptedOcrProvider:
    """按页号返回预设 OcrResult 或抛预设异常的假 provider。

    ``anonymous_model=True`` 时 describe() 也不声明模型身份——用于验证
    页级与 provider 级模型身份全部缺失时必须进 needs_review。
    """

    provider_id = "fake_quality_provider"

    def __init__(
        self,
        results: dict[int, OcrResult | Exception],
        *,
        anonymous_model: bool = False,
    ):
        self.results = results
        self.anonymous_model = anonymous_model
        self.calls: list[int] = []

    def describe(self) -> dict:
        description = {
            "id": self.provider_id,
            "engine": "FakeOCR",
            "engine_version": "0.0.test",
            "model_downloaded": False,
        }
        if not self.anonymous_model:
            description.update({"model_id": "fake_model", "model_version": "v0"})
        return description

    def recognize(self, image, *, page_number: int) -> OcrResult:
        self.calls.append(page_number)
        outcome = self.results[page_number]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _ocr(
    text: str,
    *,
    confidence: float | None = 0.95,
    provider_id: str = "fake_quality_provider",
    model_id: str = "fake_model",
    model_version: str = "v0",
) -> OcrResult:
    return OcrResult(
        text=text,
        confidence=confidence,
        provider_id=provider_id,
        model_id=model_id,
        model_version=model_version,
    )


def _page(number: int, text: str = "", *, image: bool = False) -> RenderedPdfPage:
    return RenderedPdfPage(
        page_number=number,
        native_text=text,
        render_image=(lambda: object()) if image else None,
        image_count=1 if image else 0,
    )


def _extract(pages: list[RenderedPdfPage], **kwargs):
    """跑真实 extract_pdf_document；返回 (report, pending_exc)。"""
    report_holder: dict[str, object] = {}
    try:
        report = extract_pdf_document(
            Path("fake.pdf"), renderer=FakeRenderer(pages), **kwargs
        )
    except PdfExtractionPending as pending:
        report_holder["pending"] = True
        return pending.report, pending
    report_holder["pending"] = False
    return report, None


# ---------------------------------------------------------------------------
# 用例集 A：输入页形态 → 页状态 全矩阵（质量门控行为）
# ---------------------------------------------------------------------------

QUALITY_CASES = [
    # (用例名, 页, provider 结局, 期望状态, 期望可解析, 期望 error 关键词)
    (
        "纯文本层页→native_text",
        _page(1, "合同价款为人民币 10000 元"),
        None,
        "native_text",
        True,
        None,
    ),
    (
        "文本层与扫描图并存→needs_review（拒绝仅依赖文本层）",
        _page(1, "第 1 页有文本层", image=True),
        None,
        "needs_review",
        False,
        "文本层和图片",
    ),
    (
        "无文本层且无 provider→pending_ocr（显式待处理，不静默）",
        _page(1, image=True),
        "no-provider",
        "pending_ocr",
        False,
        "未配置离线 OCR",
    ),
    (
        "渲染图像不可得→ocr_failed",
        RenderedPdfPage(page_number=1, native_text="", render_image=None, image_count=1),
        "ok",
        "ocr_failed",
        False,
        "无法取得渲染图像",
    ),
    (
        "provider 异常→ocr_failed（页级显式失败）",
        _page(1, image=True),
        RuntimeError("engine crashed"),
        "ocr_failed",
        False,
        "OCR 页面处理失败",
    ),
    (
        "OCR 空文本→needs_review（可能是空白页或不可识别）",
        _page(1, image=True),
        "empty-text",
        "needs_review",
        False,
        "OCR 未返回文本",
    ),
    (
        "OCR 置信度低于阈值→needs_review（低质量页进人工确认）",
        _page(1, image=True),
        "low-confidence",
        "needs_review",
        False,
        "OCR 文本需要人工复核",
    ),
    (
        "OCR 缺少置信度→needs_review（质量元数据缺失不当可用）",
        _page(1, image=True),
        "no-confidence",
        "needs_review",
        False,
        "缺少可信度",
    ),
    (
        "OCR 缺少模型身份→needs_review（无 model_id 不可追溯）",
        _page(1, image=True),
        "no-model-id",
        "needs_review",
        False,
        "模型身份元数据",
    ),
    (
        "OCR 达标→ocr（文本、置信度、模型身份齐全）",
        _page(1, image=True),
        "ok",
        "ocr",
        True,
        None,
    ),
]


def _build_case(
    page: RenderedPdfPage, outcome
) -> tuple[ScriptedOcrProvider | None, bool]:
    """返回 (provider, anonymous_model)；provider 为 None 表示无 provider。"""
    if outcome is None:
        return None, False
    if outcome == "no-provider":
        return None, False
    mapping: dict[int, OcrResult | Exception] = {}
    anonymous = False
    if outcome == "ok":
        mapping[1] = _ocr("扫描页识别文本 第一行")
    elif outcome == "empty-text":
        mapping[1] = _ocr("   ")
    elif outcome == "low-confidence":
        mapping[1] = _ocr("模糊扫描文本", confidence=OCR_CONFIDENCE_THRESHOLD - 0.01)
    elif outcome == "no-confidence":
        mapping[1] = _ocr("无置信度文本", confidence=None)
    elif outcome == "no-model-id":
        # 页级与 provider 级模型身份全部缺失，才构成"不可追溯"用例；
        # 仅页级缺失会被 describe() 元数据合法回填（provider 已声明身份）。
        mapping[1] = _ocr("缺模型身份文本", model_id="")
        anonymous = True
    elif isinstance(outcome, Exception):
        mapping[1] = outcome
    else:  # pragma: no cover - 防御用例表自身写错
        raise AssertionError(f"未知用例结局：{outcome!r}")
    return ScriptedOcrProvider(mapping, anonymous_model=anonymous), anonymous


@pytest.mark.parametrize(
    "name,page,outcome,expected_status,parseable,error_keyword",
    QUALITY_CASES,
    ids=[case[0] for case in QUALITY_CASES],
)
def test_page_quality_gating_matrix(
    name, page, outcome, expected_status, parseable, error_keyword
):
    provider, _anonymous = _build_case(page, outcome)
    report, pending = _extract([page], ocr_provider=provider)
    result = report.pages[0]
    assert result.status == expected_status, (
        f"{name}：期望 {expected_status}，实际 {result.status}（error={result.error!r}）"
    )
    assert (result.status in PARSEABLE_PAGE_STATUSES) is parseable
    assert report.parse_ready is parseable
    if error_keyword:
        assert error_keyword in result.error
    if parseable and pending is None:
        # 可解析页必须能进入段落转换；不可解析页必须以 Pending 拒绝。
        paragraphs = paragraphs_from_report(report)
        assert paragraphs
    else:
        assert pending is not None
        with pytest.raises(PdfExtractionPending):
            paragraphs_from_report(report)


def test_ocr_boundary_confidence_exactly_at_threshold_is_accepted():
    """置信度恰好等于阈值视为达标（阈值是下界），且阈值可按参数收紧。"""
    page = _page(1, image=True)
    provider = ScriptedOcrProvider(
        {1: _ocr("边界置信度文本", confidence=OCR_CONFIDENCE_THRESHOLD)}
    )
    report, pending = _extract([page], ocr_provider=provider)
    assert report.pages[0].status == "ocr"
    assert report.parse_ready
    # 收紧阈值后同页必须降为 needs_review——证明阈值真实参与门控。
    strict_provider = ScriptedOcrProvider(
        {1: _ocr("边界置信度文本", confidence=OCR_CONFIDENCE_THRESHOLD)}
    )
    report_strict, _ = _extract(
        [page], ocr_provider=strict_provider,
        confidence_threshold=OCR_CONFIDENCE_THRESHOLD + 0.05,
    )
    assert report_strict.pages[0].status == "needs_review"


def test_ocr_page_records_model_identity_and_provider_metadata():
    """ocr 页必须记录 provider/model/version（Evidence 可追溯性）。"""
    provider = ScriptedOcrProvider({1: _ocr("识别文本")})
    report, _ = _extract([_page(1, image=True)], ocr_provider=provider)
    page = report.pages[0]
    assert page.provider_id == "fake_quality_provider"
    assert page.model_id == "fake_model"
    assert page.model_version == "v0"
    assert page.confidence == pytest.approx(0.95)
    stats = page.as_stats()
    assert stats["provider_id"] == "fake_quality_provider"
    assert stats["model_id"] == "fake_model"
    assert stats["text_sha256"]


# ---------------------------------------------------------------------------
# 用例集 B：覆盖完整性 fail-closed（缺页 / 多页 / 乱序 / 零页）
# ---------------------------------------------------------------------------


def test_missing_trailing_page_fails_closed_with_incomplete_report():
    pages = [_page(1, "有文本")]
    renderer = FakeRenderer(pages, page_count=2)  # 声明 2 页只给 1 页
    with pytest.raises(PdfPipelineError) as excinfo:
        extract_pdf_document(Path("fake.pdf"), renderer=renderer)
    assert "页面缺失" in str(excinfo.value)
    assert excinfo.value.report is not None
    assert not excinfo.value.report.coverage_complete


def test_extra_page_beyond_declared_count_fails_closed():
    pages = [_page(1, "有文本"), _page(2, "多出来的页")]
    renderer = FakeRenderer(pages, page_count=1)
    with pytest.raises(PdfPipelineError) as excinfo:
        extract_pdf_document(Path("fake.pdf"), renderer=renderer)
    assert "超出声明页数" in str(excinfo.value)


def test_out_of_order_page_fails_closed():
    pages = [_page(2, "顺序错乱"), _page(1, "第一页")]
    renderer = FakeRenderer(pages, page_count=2)
    with pytest.raises(PdfPipelineError) as excinfo:
        extract_pdf_document(Path("fake.pdf"), renderer=renderer)
    assert "顺序/编号不一致" in str(excinfo.value)


def test_zero_page_pdf_fails_closed():
    renderer = FakeRenderer([], page_count=0)
    with pytest.raises(PdfPipelineError) as excinfo:
        extract_pdf_document(Path("fake.pdf"), renderer=renderer)
    assert "没有可处理的页面" in str(excinfo.value)


def test_mixed_document_needs_review_blocks_whole_parse_not_partial():
    """混合 PDF：可解析页存在也必须整份 Pending，禁止部分解析进入合同抽取。"""
    provider = ScriptedOcrProvider({2: _ocr("第 2 页扫描文本", confidence=0.5)})
    report, pending = _extract(
        [_page(1, "第 1 页原生文本"), _page(2, image=True), _page(3, "第 3 页原生文本")],
        ocr_provider=provider,
    )
    assert pending is not None
    assert [page.status for page in report.pages] == [
        "native_text", "needs_review", "native_text",
    ]
    assert not report.parse_ready
    assert report.status_counts == {
        "native_text": 2, "ocr": 0, "pending_ocr": 0,
        "ocr_failed": 0, "needs_review": 1,
    }
    # 未解决页清单必须逐页可定位（error 文本含页号与状态）。
    assert "2:needs_review" in report.error


# ---------------------------------------------------------------------------
# 用例集 C：数据结构不变量（在构造层就拒绝非法质量数据）
# ---------------------------------------------------------------------------


def test_ocr_result_rejects_out_of_range_confidence():
    with pytest.raises(ValueError):
        OcrResult(text="x", confidence=1.5)
    with pytest.raises(ValueError):
        OcrResult(text="x", confidence=-0.01)


def test_page_result_rejects_parseable_status_with_empty_text():
    with pytest.raises(ValueError):
        PageExtractionResult(1, "ocr", "   ")
    with pytest.raises(ValueError):
        PageExtractionResult(1, "native_text", "")


def test_page_result_rejects_unknown_status():
    with pytest.raises(ValueError):
        PageExtractionResult(1, "silently_guessed", "文本")


def test_status_vocabulary_is_exactly_the_five_charter_states():
    assert PAGE_STATUSES == frozenset(
        {"native_text", "ocr", "pending_ocr", "ocr_failed", "needs_review"}
    )
    assert PARSEABLE_PAGE_STATUSES == frozenset({"native_text", "ocr"})


# ---------------------------------------------------------------------------
# 用例集 D：RapidOCR 实机质量回归（本机有受信任模型才运行）
# ---------------------------------------------------------------------------

REAL_OCR_ENV_FLAG = "JIADUN_TEST_REAL_OCR"


def _real_provider():
    pytest.importorskip("rapidocr_onnxruntime", reason="本机未安装 RapidOCR")
    from jiadun.platform.ocr import OcrProviderUnavailable, RapidOcrProvider

    try:
        return RapidOcrProvider()
    except OcrProviderUnavailable as exc:  # 模型缺失/版本漂移/校验失败
        pytest.skip(f"RapidOCR 随包模型不可用：{exc}")


@pytest.mark.skipif(
    os.environ.get(REAL_OCR_ENV_FLAG) != "1",
    reason=f"实机 OCR 用例默认跳过；设 {REAL_OCR_ENV_FLAG}=1 且本机安装受信任模型后运行",
)
def test_real_rapidocr_quality_regression_on_rendered_chinese_page(tmp_path: Path):
    """真实渲染 + 真实识别：中文文本页必须产出 ocr 状态、达标置信度与模型身份。

    覆盖 2026-09 实测链路：Pillow 合成中文页（无 PDF 文本层）→
    RapidOcrProvider.recognize → OcrResult 质量元数据 → 管线门控。
    中文渲染字体缺失时按环境跳过，不伪装成质量通过。
    """
    provider = _real_provider()

    from PIL import Image, ImageDraw, ImageFont

    text_lines = [
        "合同价款为人民币壹佰贰拾万元整",
        "竣工结算审核应在30天内完成",
        "质量保证金按结算总额的3%预留",
    ]
    width, height = 1240, 640
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = None
    for candidate in (
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(candidate).exists():
            try:
                font = ImageFont.truetype(candidate, 36)
                break
            except OSError:
                continue
    if font is None:
        pytest.skip("本机无可用中文字体，无法构造真实中文渲染页")
    for offset, line in enumerate(text_lines):
        draw.text((80, 80 + offset * 120), line, fill="black", font=font)
    png_path = tmp_path / "中文扫描页.png"
    image.save(png_path)

    from PIL import Image as PilImage

    with PilImage.open(png_path) as handle:
        result = provider.recognize(handle.copy(), page_number=1)

    assert result.text, "实机 OCR 未返回文本"
    joined = result.text.replace(" ", "")
    assert "人民币" in joined or "合同价款" in joined, (
        f"实机 OCR 未识别出关键中文内容：{result.text!r}"
    )
    assert result.confidence is not None and result.confidence >= OCR_CONFIDENCE_THRESHOLD, (
        f"实机 OCR 置信度未达标：{result.confidence}"
    )
    metadata = provider.describe()
    assert metadata["model_id"] == "ch_PP-OCRv4_det-rec_cls"
    assert metadata["engine_version"] == "1.4.4"
    assert len(metadata["model_files"]) == 3
    assert all(entry["sha256"] for entry in metadata["model_files"])
    assert metadata["model_downloaded"] is False

    # 实机识别结果接入管线后必须落在 ocr（可解析）状态，且携带模型身份。
    rendered = RenderedPdfPage(
        page_number=1,
        native_text="",
        render_image=lambda: PilImage.open(png_path).copy(),
        image_count=1,
    )
    report, _ = _extract([rendered], ocr_provider=provider)
    assert report.pages[0].status == "ocr"
    assert report.pages[0].model_id == "ch_PP-OCRv4_det-rec_cls"
