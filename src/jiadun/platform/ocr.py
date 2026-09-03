"""本地 OCR provider 适配器。

RapidOCR 只是平台适配层的一个可替换实现。模型随 Python 包安装并在使用前
逐个校验存在、大小和 SHA-256；本模块不联网、不下载模型、不上传用户文件。
"""
from __future__ import annotations

import hashlib
from importlib import metadata
from pathlib import Path
from typing import Any

from jiadun.core.parsing.pdf_pipeline import (
    TRUSTED_RAPIDOCR_MODEL_FILES,
    OcrResult,
)

_MODEL_FILES = TRUSTED_RAPIDOCR_MODEL_FILES


class OcrProviderUnavailable(RuntimeError):  # noqa: N818 - provider boundary name
    """本地 OCR 引擎或其随包模型不可用。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_model_file(
    path: Path, model_name: str, *, expected_size: int, expected_sha256: str
) -> tuple[int, str]:
    """拒绝缺失、替换或版本漂移的随包模型；禁止静默下载或继续。"""
    try:
        actual_size = path.stat().st_size
        actual_sha256 = _sha256(path)
    except OSError as exc:
        raise OcrProviderUnavailable(
            f"OCR 模型文件无法读取：{model_name}（安装不完整或权限异常）"
        ) from exc
    if actual_size != expected_size or actual_sha256 != expected_sha256:
        raise OcrProviderUnavailable(
            f"OCR 模型文件校验失败：{model_name}（版本或文件内容不受信任）"
        )
    return actual_size, actual_sha256


class RapidOcrProvider:
    """RapidOCR + ONNX Runtime 的无网络、跨平台适配器。"""

    provider_id = "rapidocr_onnxruntime"

    def __init__(self) -> None:
        try:
            import rapidocr_onnxruntime
        except ImportError as exc:
            raise OcrProviderUnavailable(
                "未安装 rapidocr-onnxruntime，扫描 PDF 保持 OCR 待处理"
            ) from exc

        try:
            package_version = metadata.version("rapidocr_onnxruntime")
        except metadata.PackageNotFoundError as exc:
            raise OcrProviderUnavailable("OCR 引擎版本元数据缺失") from exc
        if package_version != "1.4.4":
            raise OcrProviderUnavailable(
                f"OCR 引擎版本未纳入校验清单：{package_version}"
            )

        package_root = Path(rapidocr_onnxruntime.__file__).resolve().parent
        model_records: list[dict[str, Any]] = []
        for model_name, relative, expected_size, expected_sha256 in _MODEL_FILES:
            model_path = package_root / relative
            if not model_path.is_file():
                raise OcrProviderUnavailable(
                    f"OCR 模型文件缺失：{model_name}（未下载或安装不完整）"
                )
            actual_size, actual_sha256 = _verify_model_file(
                model_path,
                model_name,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
            model_records.append({
                "name": model_name,
                "filename": relative,
                "sha256": actual_sha256,
                "size_bytes": actual_size,
            })

        package_metadata = metadata.metadata("rapidocr_onnxruntime")
        bundle_digest = hashlib.sha256()
        for model in model_records:
            bundle_digest.update(
                f"{model['filename']}:{model['sha256']}\n".encode()
            )
        self._metadata = {
            "id": self.provider_id,
            "engine": "RapidOCR",
            "engine_version": package_version,
            "model_id": "ch_PP-OCRv4_det-rec_cls",
            "model_version": "PP-OCRv4",
            "model_sha256": bundle_digest.hexdigest(),
            "model_size_bytes": sum(item["size_bytes"] for item in model_records),
            "model_files": model_records,
            "source": "bundled package rapidocr-onnxruntime",
            "source_url": package_metadata.get("Home-page") or "",
            "license": package_metadata.get("License") or "Apache-2.0",
            "language": ["zh", "en"],
            "model_downloaded": False,
        }
        self._engine: Any = None

    def describe(self) -> dict[str, Any]:
        """返回可写入 Evidence/解析批次的稳定元数据，不暴露本机绝对路径。"""
        return {
            **self._metadata,
            "model_files": [dict(item) for item in self._metadata["model_files"]],
            "language": list(self._metadata["language"]),
        }

    def _get_engine(self) -> Any:
        if self._engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR

                # 默认配置只使用随包模型；不传入任何远程模型地址或下载回调。
                self._engine = RapidOCR()
            except Exception as exc:  # noqa: BLE001 - surface as explicit provider failure
                raise OcrProviderUnavailable(f"OCR 引擎初始化失败：{type(exc).__name__}") from exc
        return self._engine

    def recognize(self, image: Any, *, page_number: int) -> OcrResult:
        if page_number < 1:
            raise ValueError("OCR page number must be positive")
        engine = self._get_engine()
        try:
            rows, _timings = engine(image)
        except Exception as exc:  # noqa: BLE001 - caller records page-level failure
            raise OcrProviderUnavailable(
                f"OCR 第 {page_number} 页识别失败：{type(exc).__name__}"
            ) from exc

        texts: list[str] = []
        scores: list[float] = []
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            text = str(row[1] or "").replace("\x00", "").strip()
            if not text:
                continue
            texts.append(text)
            try:
                score = float(row[2])
            except (TypeError, ValueError):
                continue
            if 0.0 <= score <= 1.0:
                scores.append(score)

        return OcrResult(
            text="\n".join(texts),
            confidence=min(scores) if scores else None,
            provider_id=self.provider_id,
            model_id=str(self._metadata["model_id"]),
            model_version=str(self._metadata["model_version"]),
            metadata=self.describe(),
        )


def get_default_ocr_provider() -> RapidOcrProvider | None:
    """返回本机已安装的默认 provider；不可用时保守返回 None。"""
    try:
        return RapidOcrProvider()
    except (ImportError, OcrProviderUnavailable, OSError, RuntimeError):
        return None


__all__ = [
    "OcrProviderUnavailable",
    "RapidOcrProvider",
    "get_default_ocr_provider",
]
