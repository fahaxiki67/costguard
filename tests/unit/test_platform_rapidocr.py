"""本地 OCR 的遥测边界；防止原生上传线程在退出时崩溃。"""

import builtins

import onnxruntime
import pytest

from jiadun.platform.ocr import RapidOcrProvider, get_default_ocr_provider


@pytest.mark.parametrize("factory", [RapidOcrProvider, get_default_ocr_provider])
def test_disables_telemetry_before_loading_ocr(factory, monkeypatch):
    events = []
    original_disable = onnxruntime.disable_telemetry_events
    original_import = builtins.__import__

    def disable():
        original_disable()
        events.append("disabled")

    def observe_import(name, *args, **kwargs):
        if name == "rapidocr_onnxruntime":
            events.append("load_ocr")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(onnxruntime, "disable_telemetry_events", disable)
    monkeypatch.setattr(builtins, "__import__", observe_import)

    provider = factory()
    assert provider is not None
    assert events[:2] == ["disabled", "load_ocr"]
    assert provider.describe()["model_downloaded"] is False


def test_default_provider_fails_closed_if_telemetry_cannot_be_disabled(monkeypatch):
    def unavailable():
        raise RuntimeError("telemetry control unavailable")

    monkeypatch.setattr(onnxruntime, "disable_telemetry_events", unavailable)
    assert get_default_ocr_provider() is None
