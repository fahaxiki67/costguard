"""全局测试隔离设施。

项目创建/打开现在会持久登记工作空间（写 settings.json）。为避免任何测试
污染真实用户配置（也避免真实配置影响测试断言），所有测试自动使用临时
settings 文件；需要自定路径的测试可再次覆写（如 ws fixture）。
"""
from __future__ import annotations

import pytest

from jiadun.core.models import project as project_model


@pytest.fixture(autouse=True)
def _isolate_global_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(project_model, "_SETTINGS_FILE", tmp_path / "settings.json")
    # legacy: 测试默认不应读取本机真实 CostGuard 设置；兼容回退由专门测试
    # 显式注入临时 legacy 文件验证。
    monkeypatch.setattr(
        project_model.platform_paths,
        "legacy_settings_file",
        lambda: tmp_path / "legacy-settings.json",
    )
    # legacy: 隔离本机既有 CostGuardProjects，避免只读发现污染路径断言。
    monkeypatch.setattr(
        project_model.platform_paths,
        "legacy_workspace_root",
        lambda: tmp_path / "CostGuardProjects",
    )
