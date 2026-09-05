"""PaddleOCR 增强引擎适配器测试（伪造 paddleocr 模块，不安装真实引擎）。

验证边界：
- 未安装 / 无清单 / 无模型身份 / 未知角色 / 目录缺失 / 文件被替换 →
  一律 OcrProviderUnavailable，绝不自动安装或下载；
- 引擎参数必须指向已校验的本地模型目录（防首次初始化静默下载）；
- 2.x/3.x 两种返回形态映射为 OcrResult；畸形行跳过，不猜测文本；
- provider 不出现在默认工厂中（显式启用边界）。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from jiadun.platform.ocr import (
    OcrProviderUnavailable,
    PaddleModelSpec,
    PaddleOcrProvider,
    get_default_ocr_provider,
)


def _write_model(root: Path, relative: str, content: bytes) -> PaddleModelSpec:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    import hashlib

    return PaddleModelSpec(
        name=relative.split("/")[0],
        filename=relative,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


class FakePaddleEngine:
    """记录构造参数、返回预设识别结果的假 PaddleOCR 引擎。"""

    last_kwargs: dict | None = None
    construct_signatures: list[dict] = []
    result: object = None
    raise_on_predict: Exception | None = None

    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)
        type(self).last_kwargs = dict(kwargs)
        type(self).construct_signatures.append(dict(kwargs))
        if "text_detection_model_dir" not in kwargs and "det_model_dir" not in kwargs:
            raise TypeError("missing model dir params")

    def predict(self, image):
        if type(self).raise_on_predict is not None:
            raise type(self).raise_on_predict
        return type(self).result


@pytest.fixture()
def fake_paddle(monkeypatch: pytest.MonkeyPatch):
    module = types.ModuleType("paddleocr")
    module.PaddleOCR = FakePaddleEngine
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    FakePaddleEngine.last_kwargs = None
    FakePaddleEngine.construct_signatures = []
    FakePaddleEngine.result = None
    FakePaddleEngine.raise_on_predict = None
    monkeypatch.setattr(
        "jiadun.platform.ocr.metadata.version",
        lambda _name: "3.0.0b1",
        raising=True,
    )
    return FakePaddleEngine


@pytest.fixture()
def trusted_manifest(tmp_path: Path) -> tuple[Path, list[PaddleModelSpec]]:
    root = tmp_path / "paddle-models"
    specs = [
        _write_model(root, "det/model.pdmodel", b"det-model-bytes"),
        _write_model(root, "rec/model.pdmodel", b"rec-model-bytes"),
    ]
    return root, specs


def _provider(root: Path, specs: list[PaddleModelSpec], **kwargs) -> PaddleOcrProvider:
    return PaddleOcrProvider(
        models_dir=root,
        model_files=specs,
        model_id=kwargs.pop("model_id", "local-pp-ocrv4"),
        model_version=kwargs.pop("model_version", "custom-v1"),
        **kwargs,
    )


def test_unavailable_when_paddle_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        sys, "modules", {k: v for k, v in sys.modules.items() if k != "paddleocr"}
    )
    with pytest.raises(OcrProviderUnavailable, match="未安装 paddleocr"):
        PaddleOcrProvider(
            models_dir=tmp_path,
            model_files=[PaddleModelSpec("det", "det/m", 1, "0" * 64)],
            model_id="x",
            model_version="v",
        )


def test_refuses_empty_manifest_or_missing_identity(tmp_path: Path, fake_paddle):
    with pytest.raises(OcrProviderUnavailable, match="拒绝无清单启用"):
        _provider(tmp_path, [])
    with pytest.raises(OcrProviderUnavailable, match="model_id"):
        _provider(tmp_path, [PaddleModelSpec("det", "det/m", 1, "0" * 64)], model_id="")


def test_refuses_unknown_model_role(trusted_manifest, fake_paddle):
    root, specs = trusted_manifest
    bad = [PaddleModelSpec(specs[0].name, specs[0].filename, specs[0].size_bytes, specs[0].sha256)]
    bad[0] = PaddleModelSpec(
        "layout", specs[0].filename, specs[0].size_bytes, specs[0].sha256
    )
    with pytest.raises(OcrProviderUnavailable, match="未知角色"):
        _provider(root, bad)


def test_refuses_missing_models_dir(tmp_path: Path, fake_paddle):
    with pytest.raises(OcrProviderUnavailable, match="不自动下载"):
        _provider(tmp_path / "nope", [PaddleModelSpec("det", "det/m", 1, "0" * 64)])


def test_refuses_tampered_model_file(trusted_manifest, fake_paddle, monkeypatch):
    root, specs = trusted_manifest
    (root / "rec/model.pdmodel").write_bytes(b"tampered")
    with pytest.raises(OcrProviderUnavailable, match="校验失败"):
        _provider(root, specs)
    # 篡改后文件大小变化同样必须拒绝（大小/哈希双校验）。
    (root / "rec/model.pdmodel").write_bytes(b"x" * specs[1].size_bytes)
    with pytest.raises(OcrProviderUnavailable, match="校验失败"):
        _provider(root, specs)


def test_engine_receives_verified_local_model_dirs(trusted_manifest, fake_paddle):
    root, specs = trusted_manifest
    provider = _provider(root, specs)
    result = provider.recognize(object(), page_number=1)
    assert FakePaddleEngine.last_kwargs is not None
    # 首选 3.x 签名：模型目录必须指向已校验的本地子目录。
    assert FakePaddleEngine.construct_signatures[0]["text_detection_model_dir"] == str(
        root / "det"
    )
    assert FakePaddleEngine.construct_signatures[0]["text_recognition_model_dir"] == str(
        root / "rec"
    )
    assert FakePaddleEngine.last_kwargs["lang"] == "ch"
    description = provider.describe()
    assert description["id"] == "paddleocr_local"
    assert description["engine_version"] == "3.0.0b1"
    assert description["model_id"] == "local-pp-ocrv4"
    assert description["model_downloaded"] is False
    assert len(description["model_files"]) == 2
    assert result.provider_id == "paddleocr_local"
    assert result.model_id == "local-pp-ocrv4"


def test_falls_back_to_legacy_signature_when_3x_rejected(
    trusted_manifest, fake_paddle
):
    root, specs = trusted_manifest

    class LegacyOnlyEngine(FakePaddleEngine):
        def __init__(self, **kwargs):
            if "text_detection_model_dir" in kwargs:
                raise TypeError("3.x param not supported")
            super().__init__(**kwargs)

    import sys as _sys

    _sys.modules["paddleocr"].PaddleOCR = LegacyOnlyEngine  # type: ignore[attr-defined]
    provider = _provider(root, specs)
    provider.recognize(object(), page_number=1)
    # 3.x 签名被拒后必须回退 2.x 签名并成功；只有成功的构造被记录。
    assert len(LegacyOnlyEngine.construct_signatures) == 1
    assert LegacyOnlyEngine.last_kwargs is not None
    assert LegacyOnlyEngine.last_kwargs["det_model_dir"] == str(root / "det")
    assert LegacyOnlyEngine.last_kwargs["rec_model_dir"] == str(root / "rec")


def test_both_signatures_rejected_means_unavailable(trusted_manifest, fake_paddle):
    root, specs = trusted_manifest

    class BrokenEngine(FakePaddleEngine):
        def __init__(self, **kwargs):
            raise RuntimeError("nope")

    import sys as _sys

    _sys.modules["paddleocr"].PaddleOCR = BrokenEngine  # type: ignore[attr-defined]
    provider = _provider(root, specs)
    with pytest.raises(OcrProviderUnavailable, match="引擎初始化失败"):
        provider.recognize(object(), page_number=1)


def test_maps_paddle_2x_payload_shape(trusted_manifest, fake_paddle):
    root, specs = trusted_manifest
    # 2.x ocr() 返回 [页载荷]，页载荷是 [bbox, (text, score)] 行的列表。
    bbox = [[1, 2], [3, 4], [5, 6], [7, 8]]
    fake_paddle.result = [
        [
            [bbox, ("合同价款为人民币壹佰万元整", 0.98)],
            [bbox, ("竣工结算审核应在30天内完成", 0.91)],
        ],
    ]
    provider = _provider(root, specs)
    result = provider.recognize(object(), page_number=1)
    assert result.text == "合同价款为人民币壹佰万元整\n竣工结算审核应在30天内完成"
    assert result.confidence == pytest.approx(0.91)  # 取最低分，保守


def test_maps_paddle_3x_result_object(trusted_manifest, fake_paddle):
    root, specs = trusted_manifest
    # 3.x predict() 返回带 rec_texts/rec_scores 属性的结果对象列表。
    class Fake3xResult:
        rec_texts = ["工程质量保修期为24个月", "按月支付至已完工程价款的85%"]
        rec_scores = [0.97, 1.9]  # 越界分数按缺失处理

    fake_paddle.result = [Fake3xResult()]
    provider = _provider(root, specs)
    result = provider.recognize(object(), page_number=1)
    assert result.text == "工程质量保修期为24个月\n按月支付至已完工程价款的85%"
    assert result.confidence == pytest.approx(0.97)


def test_maps_flat_text_score_shape_and_skips_malformed_rows(
    trusted_manifest, fake_paddle
):
    root, specs = trusted_manifest
    fake_paddle.result = [
        [
            ([1, 2], "纯文本行", 0.95),          # 扁平形态
            ([1, 2], "   ", 0.9),                # 空文本跳过
            ([1, 2], 123, 0.9),                  # 非字符串跳过
            "不是一行列表",                       # 形状不对跳过
            ([1, 2], "置信度越界", 1.7),          # 越界置信度按缺失处理
            ([1, 2], ("无分数行", None)),         # 缺分数按 None 处理
        ],
    ]
    provider = _provider(root, specs)
    result = provider.recognize(object(), page_number=1)
    assert result.text == "纯文本行\n置信度越界\n无分数行"
    assert result.confidence == pytest.approx(0.95)


def test_predict_failure_raises_provider_unavailable(trusted_manifest, fake_paddle):
    root, specs = trusted_manifest
    fake_paddle.result = []
    fake_paddle.raise_on_predict = RuntimeError("engine boom")
    provider = _provider(root, specs)
    with pytest.raises(OcrProviderUnavailable, match="识别失败"):
        provider.recognize(object(), page_number=1)


def test_page_number_must_be_positive(trusted_manifest, fake_paddle):
    root, specs = trusted_manifest
    provider = _provider(root, specs)
    with pytest.raises(ValueError):
        provider.recognize(object(), page_number=0)


def test_paddle_is_not_silently_default():
    """默认工厂只回 RapidOCR；Paddle 必须显式构造，防止静默换引擎。"""
    default = get_default_ocr_provider()
    assert default is None or type(default).__name__ == "RapidOcrProvider"
