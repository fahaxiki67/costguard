"""页级 PDF 提取管线。

本模块只定义跨平台的页面结果、渲染器和 OCR provider 边界。具体 OCR 引擎
必须由 ``jiadun.platform`` 适配，核心层不直接依赖 RapidOCR、PaddleOCR 或
任何操作系统 API。

页面完整性是 fail-closed 的前提：只有所有页面都按 1..N 到达，且每页状态
为 ``native_text`` 或 ``ocr`` 时，文档才允许进入后续合同文本解析。页面状态
和模型摘要可以序列化到现有 ``parse_batches.stats_json``，不把 OCR 原文重复
写入解析统计；原文引用由后续 Evidence 保存。
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

PAGE_STATUSES = frozenset({
    "native_text", "ocr", "pending_ocr", "ocr_failed", "needs_review",
})
PARSEABLE_PAGE_STATUSES = frozenset({"native_text", "ocr"})
OCR_CONFIDENCE_THRESHOLD = 0.80
PDF_PIPELINE_VERSION = "pdf-hybrid-v1"

# 这是持久化 OCR 批次可复用的信任边界，不是 OCR 引擎 API。平台适配层
# 使用同一份清单校验随包模型；核心层据此拒绝数据库中“自洽但未受信任”的
# 模型快照，避免旧批次被替换模型伪装成可复用结果。
TRUSTED_RAPIDOCR_MODEL_FILES = (
    (
        "det",
        "models/ch_PP-OCRv4_det_infer.onnx",
        4_745_517,
        "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9",
    ),
    (
        "rec",
        "models/ch_PP-OCRv4_rec_infer.onnx",
        10_857_958,
        "48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b",
    ),
    (
        "cls",
        "models/ch_ppocr_mobile_v2.0_cls_infer.onnx",
        585_532,
        "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
    ),
)
TRUSTED_RAPIDOCR_MODEL_SIZE_BYTES = 16_189_007
TRUSTED_RAPIDOCR_MODEL_SHA256 = (
    "c4e5d0ece5870fee10bdc8325827aa3851390356fae2de6eb388b9656076b629"
)


def _compact_error(value: object, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


@dataclass(frozen=True, slots=True)
class OcrResult:
    """一个 OCR provider 返回的单页文本和最小质量元数据。"""

    text: str
    confidence: float | None = None
    provider_id: str = ""
    model_id: str = ""
    model_version: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("OCR text must be str")
        if self.confidence is not None and (
            not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError("OCR confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class PageExtractionResult:
    """一个 PDF 页的完整提取状态；失败页也必须保留。"""

    page_number: int
    status: str
    text: str = ""
    extraction_method: str = "none"
    confidence: float | None = None
    provider_id: str = ""
    model_id: str = ""
    model_version: str = ""
    error: str = ""
    image_count: int = 0

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("PDF page number must be positive")
        if self.status not in PAGE_STATUSES:
            raise ValueError(f"invalid PDF page status: {self.status!r}")
        if not isinstance(self.text, str):
            raise TypeError("page text must be str")
        if self.status in PARSEABLE_PAGE_STATUSES and not self.text.strip():
            raise ValueError(f"{self.status} page must contain text")
        if self.confidence is not None and (
            not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError("page confidence must be between 0 and 1")
        if isinstance(self.image_count, bool) or self.image_count < 0:
            raise ValueError("PDF image count cannot be negative")

    def as_stats(self) -> dict[str, Any]:
        text = self.text.strip()
        return {
            "page_no": int(self.page_number),
            "status": self.status,
            "extraction_method": self.extraction_method,
            "image_count": int(self.image_count),
            "text_sha256": (
                hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
            ),
            "text_char_count": len(text),
            "line_count": len(text.splitlines()) if text else 0,
            "confidence": self.confidence,
            "provider_id": self.provider_id or None,
            "model_id": self.model_id or None,
            "model_version": self.model_version or None,
            "error": self.error or None,
        }


@dataclass(frozen=True, slots=True)
class PdfExtractionReport:
    """PDF 页面提取快照，不携带整份 OCR 原文。"""

    page_count: int
    pages: tuple[PageExtractionResult, ...]
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)
    parser_version: str = PDF_PIPELINE_VERSION
    complete: bool = True
    error: str = ""

    def __post_init__(self) -> None:
        if self.page_count < 0:
            raise ValueError("PDF page count cannot be negative")
        page_numbers = [page.page_number for page in self.pages]
        if len(page_numbers) != len(set(page_numbers)):
            raise ValueError("PDF page results contain duplicate page numbers")

    @property
    def coverage_complete(self) -> bool:
        return self.complete and self.page_count > 0 and [
            page.page_number for page in self.pages
        ] == list(range(1, self.page_count + 1))

    @property
    def parse_ready(self) -> bool:
        return self.coverage_complete and all(
            page.status in PARSEABLE_PAGE_STATUSES for page in self.pages
        )

    @property
    def status_counts(self) -> dict[str, int]:
        return {
            status: sum(1 for page in self.pages if page.status == status)
            for status in sorted(PAGE_STATUSES)
        }

    def with_error(self, error: object, *, complete: bool | None = None) -> PdfExtractionReport:
        return replace(
            self,
            error=_compact_error(error),
            complete=self.complete if complete is None else complete,
        )

    def as_stats(self, *, source_sha256: str | None = None) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "parser_version": self.parser_version,
            "page_count": int(self.page_count),
            "coverage_complete": self.coverage_complete,
            "parse_ready": self.parse_ready,
            "page_status_counts": self.status_counts,
            "pages": [page.as_stats() for page in self.pages],
            "ocr_provider": _json_safe(dict(self.provider_metadata)),
            "error": self.error or None,
        }
        if source_sha256:
            stats["source_sha256"] = source_sha256
        return stats


@dataclass(frozen=True, slots=True)
class RenderedPdfPage:
    """渲染器输出的单页；图片按需渲染，处理后立即释放。"""

    page_number: int
    native_text: str = ""
    render_image: Callable[[], Any] | None = None
    image_count: int = 0

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("rendered PDF page number must be positive")
        if not isinstance(self.native_text, str):
            raise TypeError("native PDF text must be str")
        if isinstance(self.image_count, bool) or self.image_count < 0:
            raise ValueError("PDF image count cannot be negative")


class PdfRenderSession(Protocol):
    page_count: int

    def iter_pages(self) -> Iterator[RenderedPdfPage]: ...

    def __enter__(self) -> Any: ...

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> None: ...


class PdfRenderer(Protocol):
    """PDF 渲染器中立接口；实现可以在 core 或 platform 适配。"""

    def open(self, path: Path) -> PdfRenderSession: ...


class OcrProvider(Protocol):
    """OCR 引擎中立接口；不得暴露给核心业务引擎具体厂商 API。"""

    def describe(self) -> Mapping[str, Any]: ...

    def recognize(self, image: Any, *, page_number: int) -> OcrResult: ...


class PdfPipelineError(RuntimeError):
    """PDF 结构/渲染完整性错误；永远不能降级为成功。"""

    def __init__(self, message: str, report: PdfExtractionReport | None = None):
        super().__init__(message)
        self.report = report


class PdfExtractionPending(NotImplementedError):  # noqa: N818 - stable domain status name
    """页面需要 OCR、重试或人工确认；兼容既有 ImportWorker 通道。"""

    def __init__(self, message: str, report: PdfExtractionReport):
        super().__init__(message)
        self.report = report


class PdfPlumberRenderer:
    """使用 pdfplumber 读取文本并按需用 pypdfium2 渲染扫描页。"""

    def __init__(self, *, resolution: int = 200, antialias: bool = True):
        if resolution < 72:
            raise ValueError("PDF render resolution must be at least 72 DPI")
        self.resolution = int(resolution)
        self.antialias = bool(antialias)

    def open(self, path: Path) -> PdfRenderSession:
        return _PdfPlumberRenderSession(
            Path(path), resolution=self.resolution, antialias=self.antialias
        )


class _PdfPlumberRenderSession:
    def __init__(self, path: Path, *, resolution: int, antialias: bool):
        self.path = path
        self.resolution = resolution
        self.antialias = antialias
        self.pdf: Any = None
        self.page_count = 0

    def __enter__(self) -> _PdfPlumberRenderSession:
        import pdfplumber

        self.pdf = pdfplumber.open(str(self.path))
        self.page_count = len(self.pdf.pages)
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> None:
        if self.pdf is not None:
            self.pdf.close()
            self.pdf = None

    def iter_pages(self) -> Iterator[RenderedPdfPage]:
        if self.pdf is None:
            raise RuntimeError("PDF render session is not open")
        for page_number, page in enumerate(self.pdf.pages, start=1):
            native_text = (page.extract_text() or "").strip()
            image_count = len(getattr(page, "images", ()) or ())

            def render_image(page=page):
                page_image = page.to_image(
                    resolution=self.resolution, antialias=self.antialias
                )
                image = getattr(page_image, "original", None)
                if image is None:
                    raise RuntimeError("PDF renderer did not return a page image")
                return image

            yield RenderedPdfPage(
                page_number=page_number,
                native_text=native_text,
                render_image=None if native_text else render_image,
                image_count=image_count,
            )


def _provider_metadata(provider: OcrProvider | None) -> dict[str, Any]:
    if provider is None:
        return {
            "id": "disabled",
            "source": "not_configured",
            "model_id": None,
            "model_version": None,
            "model_sha256": None,
            "license": None,
            "language": [],
            "model_size_bytes": None,
        }
    describe = getattr(provider, "describe", None)
    if not callable(describe):
        return {"id": type(provider).__name__, "metadata_error": "provider has no describe()"}
    try:
        value = describe()
    except Exception as exc:  # noqa: BLE001 - metadata failure must remain visible
        return {"id": type(provider).__name__, "metadata_error": _compact_error(exc)}
    if not isinstance(value, Mapping):
        return {"id": type(provider).__name__, "metadata_error": "describe() did not return an object"}
    return dict(_json_safe(value))


def _coerce_ocr_result(value: Any) -> OcrResult:
    if isinstance(value, OcrResult):
        return value
    if isinstance(value, str):
        return OcrResult(value)
    if isinstance(value, Mapping):
        return OcrResult(
            text=str(value.get("text") or ""),
            confidence=value.get("confidence"),
            provider_id=str(value.get("provider_id") or ""),
            model_id=str(value.get("model_id") or ""),
            model_version=str(value.get("model_version") or ""),
            metadata=value,
        )
    raise TypeError(f"unsupported OCR result type: {type(value).__name__}")


def _metadata_text(metadata: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _extract_page(
    rendered: RenderedPdfPage,
    *,
    provider: OcrProvider | None,
    provider_metadata: Mapping[str, Any],
    confidence_threshold: float,
) -> PageExtractionResult:
    page_number = rendered.page_number
    native_text = rendered.native_text.strip()
    if native_text:
        if rendered.image_count:
            return PageExtractionResult(
                page_number,
                "needs_review",
                native_text,
                extraction_method="native_text_with_images",
                error=(
                    "页面同时包含文本层和图片，拒绝仅依赖文本层；"
                    "需人工确认或补充 OCR，防止扫描内容被漏掉"
                ),
                image_count=rendered.image_count,
            )
        return PageExtractionResult(
            page_number,
            "native_text",
            native_text,
            extraction_method="native_text",
            image_count=rendered.image_count,
        )

    if provider is None:
        return PageExtractionResult(
            page_number,
            "pending_ocr",
            extraction_method="none",
            error="本页没有文本层，当前未配置离线 OCR provider",
            image_count=rendered.image_count,
        )
    if rendered.render_image is None:
        return PageExtractionResult(
            page_number,
            "ocr_failed",
            extraction_method="ocr",
            error="本页无法取得渲染图像",
            image_count=rendered.image_count,
        )

    try:
        image = rendered.render_image()
        if image is None:
            raise RuntimeError("渲染器返回空图像")
        raw_result = provider.recognize(image, page_number=page_number)
        result = _coerce_ocr_result(raw_result)
    except Exception as exc:  # noqa: BLE001 - each page becomes an explicit status
        return PageExtractionResult(
            page_number,
            "ocr_failed",
            extraction_method="ocr",
            error=f"OCR 页面处理失败：{_compact_error(exc)}",
            image_count=rendered.image_count,
        )

    text = result.text.strip()
    metadata = dict(provider_metadata)
    metadata.update(dict(result.metadata))
    provider_id = result.provider_id or _metadata_text(
        metadata, "id", "provider_id"
    )
    model_id = result.model_id or _metadata_text(metadata, "model_id")
    model_version = result.model_version or _metadata_text(
        metadata, "model_version", "version"
    )
    if not text:
        return PageExtractionResult(
            page_number,
            "needs_review",
            extraction_method="ocr",
            confidence=result.confidence,
            provider_id=provider_id,
            model_id=model_id,
            model_version=model_version,
            error="OCR 未返回文本；页面可能为空白或文字不可识别",
            image_count=rendered.image_count,
        )
    if (
        result.confidence is None
        or float(result.confidence) < confidence_threshold
        or not provider_id
        or not model_id
        or not model_version
    ):
        return PageExtractionResult(
            page_number,
            "needs_review",
            text,
            extraction_method="ocr",
            confidence=result.confidence,
            provider_id=provider_id,
            model_id=model_id,
            model_version=model_version,
            error=(
                "OCR 文本需要人工复核"
                if result.confidence is not None and float(result.confidence) < confidence_threshold
                else "OCR 结果缺少可信度或模型身份元数据"
            ),
            image_count=rendered.image_count,
        )
    return PageExtractionResult(
        page_number,
        "ocr",
        text,
        extraction_method="ocr",
        confidence=result.confidence,
        provider_id=provider_id,
        model_id=model_id,
        model_version=model_version,
        image_count=rendered.image_count,
    )


def _incomplete_report(
    page_count: int,
    pages: list[PageExtractionResult],
    provider_metadata: Mapping[str, Any],
    error: object,
) -> PdfExtractionReport:
    return PdfExtractionReport(
        page_count=max(0, int(page_count)),
        pages=tuple(pages),
        provider_metadata=provider_metadata,
        complete=False,
        error=_compact_error(error),
    )


def extract_pdf_document(
    path: Path,
    *,
    renderer: PdfRenderer | None = None,
    ocr_provider: OcrProvider | None = None,
    confidence_threshold: float = OCR_CONFIDENCE_THRESHOLD,
) -> PdfExtractionReport:
    """逐页提取 PDF，发现任一未解释页面时 fail-closed。"""
    if not 0.0 <= float(confidence_threshold) <= 1.0:
        raise ValueError("OCR confidence threshold must be between 0 and 1")
    renderer = renderer or PdfPlumberRenderer()
    provider_metadata = _provider_metadata(ocr_provider)
    pages: list[PageExtractionResult] = []
    expected_page_count = 0
    try:
        with renderer.open(Path(path)) as document:
            expected_page_count = int(document.page_count)
            if expected_page_count <= 0:
                report = _incomplete_report(
                    expected_page_count, pages, provider_metadata, "PDF 没有可处理的页面"
                )
                raise PdfPipelineError("PDF 没有可处理的页面", report)
            page_iter = iter(document.iter_pages())
            for expected_page_number in range(1, expected_page_count + 1):
                try:
                    rendered = next(page_iter)
                except StopIteration as exc:
                    report = _incomplete_report(
                        expected_page_count, pages, provider_metadata,
                        f"PDF 页面缺失：期望第 {expected_page_number} 页",
                    )
                    raise PdfPipelineError(report.error, report) from exc
                if rendered.page_number != expected_page_number:
                    report = _incomplete_report(
                        expected_page_count, pages, provider_metadata,
                        "PDF 页面顺序/编号不一致，拒绝继续解析",
                    )
                    raise PdfPipelineError(report.error, report)
                pages.append(
                    _extract_page(
                        rendered,
                        provider=ocr_provider,
                        provider_metadata=provider_metadata,
                        confidence_threshold=float(confidence_threshold),
                    )
                )
            try:
                extra_page = next(page_iter)
            except StopIteration:
                pass
            else:
                report = _incomplete_report(
                    expected_page_count, pages, provider_metadata,
                    f"PDF 出现超出声明页数的第 {extra_page.page_number} 页",
                )
                raise PdfPipelineError(report.error, report)
    except PdfPipelineError:
        raise
    except Exception as exc:  # noqa: BLE001 - renderer failures are never success
        report = _incomplete_report(expected_page_count, pages, provider_metadata, exc)
        raise PdfPipelineError(f"PDF 页面读取失败：{report.error}", report) from exc

    report = PdfExtractionReport(
        page_count=expected_page_count,
        pages=tuple(pages),
        provider_metadata=provider_metadata,
    )
    unresolved = [page for page in pages if page.status not in PARSEABLE_PAGE_STATUSES]
    if unresolved:
        page_text = "、".join(
            f"{page.page_number}:{page.status}" for page in unresolved[:12]
        )
        report = report.with_error(f"PDF 页面尚未完成提取（扫描件需要 OCR）：{page_text}")
        raise PdfExtractionPending(report.error, report)
    return report


def paragraphs_from_report(report: PdfExtractionReport) -> list[dict[str, Any]]:
    """将完整页结果转成兼容旧合同解析器的段落列表。"""
    if not report.parse_ready:
        raise PdfExtractionPending(
            report.error or "PDF 页面提取不完整，不能进入合同文本解析",
            report,
        )
    paragraphs: list[dict[str, Any]] = []
    for page in report.pages:
        for line_number, line in enumerate(page.text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            paragraphs.append({
                "index": f"p{page.page_number}:{line_number}",
                "text": line,
                "page_number": page.page_number,
                "page_status": page.status,
                "extraction_method": page.extraction_method,
                "ocr_confidence": page.confidence,
                "ocr_provider": page.provider_id,
                "ocr_model": page.model_id,
                "ocr_model_version": page.model_version,
            })
    return paragraphs


__all__ = [
    "OCR_CONFIDENCE_THRESHOLD",
    "PAGE_STATUSES",
    "PARSEABLE_PAGE_STATUSES",
    "PDF_PIPELINE_VERSION",
    "OcrProvider",
    "OcrResult",
    "PageExtractionResult",
    "PdfExtractionPending",
    "PdfExtractionReport",
    "PdfPipelineError",
    "PdfPlumberRenderer",
    "PdfRenderer",
    "RenderedPdfPage",
    "extract_pdf_document",
    "paragraphs_from_report",
]
