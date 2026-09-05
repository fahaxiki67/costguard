"""本地 OCR provider 适配器。

RapidOCR 是默认轻量引擎（模型随 Python 包安装，使用前逐个校验大小和
SHA-256）；PaddleOCR 是显式启用的增强引擎（模型由使用者在本地准备并提供
受信任清单，本模块同样逐个校验）。本模块不联网、不下载模型、不上传用户
文件，也不会在引擎之间静默切换——更换 OCR 引擎会改变识别结果，必须由
调用方显式选择并记录 Evidence。
"""
from __future__ import annotations

import hashlib
from importlib import metadata
from pathlib import Path
from typing import Any, NamedTuple

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


class PaddleModelSpec(NamedTuple):
    """一条受信任的本地 Paddle 模型文件描述。

    ``name`` 只允许 ``det`` / ``rec`` / ``cls``（引擎角色），``filename``
    是相对 ``models_dir`` 的路径；其所在子目录会被用作该角色的引擎模型
    目录——例如 ``det/model.pdmodel`` 表示引擎的检测模型目录是
    ``models_dir/det``。清单同时充当"这些本地文件就是引擎要用的模型"
    的绑定：目录之外或未列出的模型不会被引用。
    """

    name: str
    filename: str
    size_bytes: int
    sha256: str


_PADDLE_MODEL_ROLES = frozenset({"det", "rec", "cls"})


def _iter_paddle_rows(raw: Any):
    """递归展开 PaddleOCR 返回值，产出 (text, confidence) 行。

    PaddleOCR 2.x ``ocr()`` 返回 ``[页载荷]``（页载荷是行列表），3.x
    ``predict()`` 返回带 ``rec_texts``/``rec_scores`` 属性的结果对象；
    版本间嵌套层数不稳定，这里按「行形状」直接识别：能被
    ``_paddle_text_and_score`` 解释的当行产出，列表/元组递归下钻，
    其余形状跳过——错误形状绝不猜测成文本。
    """
    if isinstance(raw, (list, tuple)):
        for element in raw:
            if element is None:
                continue
            extracted = _paddle_text_and_score(element)
            if extracted is not None:
                yield extracted
            else:
                yield from _iter_paddle_rows(element)
    elif raw is not None:
        texts = getattr(raw, "rec_texts", None)
        if isinstance(texts, (list, tuple)):
            raw_scores = getattr(raw, "rec_scores", None)
            for index, text in enumerate(texts):
                cleaned = str(text or "").replace("\x00", "").strip()
                if not cleaned:
                    continue
                score = None
                if isinstance(raw_scores, (list, tuple)) and index < len(raw_scores):
                    try:
                        candidate = float(raw_scores[index])
                    except (TypeError, ValueError):
                        candidate = None
                    if candidate is not None and 0.0 <= candidate <= 1.0:
                        score = candidate
                yield cleaned, score


def _paddle_text_and_score(row: Any) -> tuple[str, float | None] | None:
    """从 PaddleOCR（2.x/3.x 常见返回形态）的一行结果中取文本与置信度。

    兼容两种形状：``[bbox, (text, score), ...]``（2.x）与
    ``[bbox, text, score]``（部分版本/rec 输出）。无法识别的行返回 None，
    由调用方跳过——错误形状绝不猜测成文本。
    """
    if not isinstance(row, (list, tuple)) or len(row) < 2:
        return None
    payload = row[1]
    if isinstance(payload, (list, tuple)) and payload:
        text, score = payload[0], payload[1] if len(payload) > 1 else None
    else:
        text, score = payload, row[2] if len(row) > 2 else None
    if not isinstance(text, str):
        return None
    text = text.replace("\x00", "").strip()
    if not text:
        return None
    try:
        confidence = float(score)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None and not 0.0 <= confidence <= 1.0:
        confidence = None
    return text, confidence


class PaddleOcrProvider:
    """PaddleOCR 增强引擎适配器（显式启用，本地模型，禁止静默下载）。

    与随包安装的 RapidOCR 不同，Paddle 模型不随 Python 包分发。构造时
    必须显式提供本地模型目录和逐文件 SHA-256 清单；任何文件缺失、大小
    或哈希不符都拒绝启用。本 provider 不出现在默认 provider 工厂中，
    只能由调用方显式构造（更换 OCR 引擎必须可追溯，不允许静默切换）。
    """

    provider_id = "paddleocr_local"

    def __init__(
        self,
        *,
        models_dir: Path | str,
        model_files: list[PaddleModelSpec] | tuple[PaddleModelSpec, ...],
        model_id: str,
        model_version: str,
    ) -> None:
        if not model_files:
            raise OcrProviderUnavailable(
                "PaddleOCR 增强引擎需要显式提供本地模型清单；拒绝无清单启用"
            )
        if not model_id or not model_version:
            raise OcrProviderUnavailable(
                "PaddleOCR 增强引擎需要显式提供 model_id 与 model_version"
            )
        unknown_roles = {spec.name for spec in model_files} - _PADDLE_MODEL_ROLES
        if unknown_roles:
            raise OcrProviderUnavailable(
                f"PaddleOCR 模型清单含未知角色：{sorted(unknown_roles)}"
                f"（只允许 {sorted(_PADDLE_MODEL_ROLES)}）"
            )
        try:
            import paddleocr  # noqa: F401 - 仅探测本机安装
        except ImportError as exc:
            raise OcrProviderUnavailable(
                "未安装 paddleocr；增强 OCR 保持不可用（不自动安装）"
            ) from exc
        try:
            engine_version = metadata.version("paddleocr")
        except metadata.PackageNotFoundError as exc:
            raise OcrProviderUnavailable("PaddleOCR 引擎版本元数据缺失") from exc

        root = Path(models_dir)
        if not root.is_dir():
            raise OcrProviderUnavailable(
                f"PaddleOCR 本地模型目录不存在：{root.name}（不自动下载）"
            )
        records: list[dict[str, Any]] = []
        for spec in model_files:
            path = root / spec.filename
            actual_size, actual_sha256 = _verify_model_file(
                path,
                spec.name,
                expected_size=spec.size_bytes,
                expected_sha256=spec.sha256,
            )
            records.append({
                "name": spec.name,
                "filename": spec.filename,
                "sha256": actual_sha256,
                "size_bytes": actual_size,
            })
        bundle_digest = hashlib.sha256()
        for record in records:
            bundle_digest.update(f"{record['filename']}:{record['sha256']}\n".encode())
        self._metadata: dict[str, Any] = {
            "id": self.provider_id,
            "engine": "PaddleOCR",
            "engine_version": engine_version,
            "model_id": model_id,
            "model_version": model_version,
            "model_sha256": bundle_digest.hexdigest(),
            "model_size_bytes": sum(item["size_bytes"] for item in records),
            "model_files": records,
            "source": "user-provided local models",
            "source_url": "",
            "license": "由模型提供方决定；启用前须人工确认",
            "language": ["zh", "en"],
            "model_downloaded": False,
        }
        self._models_root = root
        self._role_dirs = {
            spec.name: str((root / spec.filename).parent) for spec in model_files
        }
        self._engine: Any = None

    def describe(self) -> dict[str, Any]:
        """返回可写入 Evidence/解析批次的稳定元数据，不暴露本机绝对路径。"""
        return {
            **self._metadata,
            "model_files": [dict(item) for item in self._metadata["model_files"]],
            "language": list(self._metadata["language"]),
        }

    def _engine_kwargs(self) -> list[dict[str, Any]]:
        """从受信清单推导引擎参数；det/rec 指向已校验的本地模型目录。

        PaddleOCR 3.x 与 2.x 参数名不同（text_detection_model_dir vs
        det_model_dir），两种签名都指向同一批本地目录；共用的轻量开关
        关闭文档预处理，减少与识别无关的行为面。
        """
        base: dict[str, Any] = {"lang": "ch"}
        if "det" in self._role_dirs and "rec" in self._role_dirs:
            return [
                {
                    **base,
                    "use_doc_orientation_classify": False,
                    "use_doc_unwarping": False,
                    "use_textline_orientation": False,
                    "text_detection_model_dir": self._role_dirs["det"],
                    "text_recognition_model_dir": self._role_dirs["rec"],
                },
                {
                    **base,
                    "det_model_dir": self._role_dirs["det"],
                    "rec_model_dir": self._role_dirs["rec"],
                },
            ]
        return [{**base}]

    def _get_engine(self) -> Any:
        if self._engine is None:
            from paddleocr import PaddleOCR

            errors: list[str] = []
            for kwargs in self._engine_kwargs():
                try:
                    self._engine = PaddleOCR(**kwargs)
                    break
                except Exception as exc:  # noqa: BLE001 - try both API signatures
                    errors.append(f"{type(exc).__name__}: {exc}")
            if self._engine is None:
                raise OcrProviderUnavailable(
                    f"PaddleOCR 引擎初始化失败（本地模型目录未被接受）："
                    f"{'; '.join(errors)[:200]}"
                )
        return self._engine

    def recognize(self, image: Any, *, page_number: int) -> OcrResult:
        if page_number < 1:
            raise ValueError("OCR page number must be positive")
        engine = self._get_engine()
        try:
            raw = engine.predict(image) if hasattr(engine, "predict") else engine.ocr(image)
        except Exception as exc:  # noqa: BLE001 - caller records page-level failure
            raise OcrProviderUnavailable(
                f"OCR 第 {page_number} 页识别失败：{type(exc).__name__}"
            ) from exc

        texts: list[str] = []
        scores: list[float] = []
        for text, confidence in _iter_paddle_rows(raw):
            texts.append(text)
            if confidence is not None:
                scores.append(confidence)
        return OcrResult(
            text="\n".join(texts),
            confidence=min(scores) if scores else None,
            provider_id=self.provider_id,
            model_id=str(self._metadata["model_id"]),
            model_version=str(self._metadata["model_version"]),
            metadata=self.describe(),
        )


__all__ = [
    "OcrProviderUnavailable",
    "PaddleModelSpec",
    "PaddleOcrProvider",
    "RapidOcrProvider",
    "get_default_ocr_provider",
]
