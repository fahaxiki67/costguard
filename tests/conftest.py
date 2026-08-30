"""全局测试隔离设施。

项目创建/打开现在会持久登记工作空间（写 settings.json）。为避免任何测试
污染真实用户配置（也避免真实配置影响测试断言），所有测试自动使用临时
settings 文件；需要自定路径的测试可再次覆写（如 ws fixture）。
"""
from __future__ import annotations

import pytest

from costguard.core.models import project as project_model


@pytest.fixture(autouse=True)
def _isolate_global_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(project_model, "_SETTINGS_FILE", tmp_path / "settings.json")
