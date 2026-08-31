"""Windows 路径分支回归测试（issue #1）。

pathlib.Path 没有 expandvars（那是 os.path.expandvars），曾经写成
Path(Path.expandvars(...)) 导致 Windows 上测试收集与 GUI 启动即抛
AttributeError，而 macOS 分支掩盖了问题。现改为直接读环境变量并带
缺省回退；这里在任意平台通过 monkeypatch 强制走 Windows 分支锁定行为。
"""
from __future__ import annotations

import json

import jiadun.platform.paths as paths
from jiadun.core.models import project as project_model


def test_config_dir_windows_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "IS_MACOS", False)
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = paths.config_dir()
    assert d == tmp_path / "Jiadun"
    assert d.is_dir()


def test_config_dir_windows_fallback_when_appdata_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "IS_MACOS", False)
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    d = paths.config_dir()
    assert d == tmp_path / "AppData" / "Roaming" / "Jiadun"


def test_default_workspace_root_windows_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    d = paths.default_workspace_root()
    assert d == tmp_path / "Documents" / "JiadunProjects"


def test_default_workspace_root_windows_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    d = paths.default_workspace_root()
    assert d == tmp_path / "Documents" / "JiadunProjects"


def test_legacy_workspace_is_discovered_read_only_when_present(monkeypatch, tmp_path):
    """旧 CostGuardProjects 只加入扫描，不替代新默认目录。"""
    new_root = tmp_path / "Documents" / "JiadunProjects"
    legacy_root = tmp_path / "Documents" / "CostGuardProjects"
    legacy_root.mkdir(parents=True)
    monkeypatch.setattr(project_model, "_SETTINGS_FILE", tmp_path / "new-settings.json")
    monkeypatch.setattr(paths, "legacy_settings_file", lambda: tmp_path / "legacy-settings.json")
    monkeypatch.setattr(paths, "default_workspace_root", lambda: new_root)
    monkeypatch.setattr(paths, "legacy_workspace_root", lambda: legacy_root)

    roots = project_model.workspace_roots()

    assert roots[0] == new_root
    assert legacy_root in roots
    assert not new_root.exists(), "读取旧空间不应自动创建新工作空间"


def test_legacy_settings_are_read_only_fallback(monkeypatch, tmp_path):
    """缺少新 settings 时可以读取旧设置，但保存仍由新路径负责。"""
    new_settings = tmp_path / "Jiadun" / "settings.json"
    legacy_settings = tmp_path / "CostGuard" / "settings.json"
    legacy_settings.parent.mkdir()
    legacy_settings.write_text(
        json.dumps({"workspace_root": str(tmp_path / "old-workspace")}), encoding="utf-8"
    )
    monkeypatch.setattr(project_model, "_SETTINGS_FILE", new_settings)
    monkeypatch.setattr(paths, "legacy_settings_file", lambda: legacy_settings)
    monkeypatch.setattr(paths, "default_workspace_root", lambda: tmp_path / "JiadunProjects")

    assert project_model.load_settings()["workspace_root"].endswith("old-workspace")
    assert project_model.workspace_root() == tmp_path / "JiadunProjects"
    assert not new_settings.exists(), "读取 legacy 设置不应复制或覆盖新设置"


def test_corrupt_current_settings_fall_back_to_legacy(monkeypatch, tmp_path):
    """当前 settings 损坏时，仍应只读回退到可用的 legacy 设置。"""
    new_settings = tmp_path / "Jiadun" / "settings.json"
    legacy_settings = tmp_path / "CostGuard" / "settings.json"
    new_settings.parent.mkdir()
    legacy_settings.parent.mkdir()
    new_settings.write_text("{not-json", encoding="utf-8")
    legacy_settings.write_text(
        json.dumps({"workspace_root": str(tmp_path / "old-workspace")}), encoding="utf-8"
    )
    monkeypatch.setattr(project_model, "_SETTINGS_FILE", new_settings)
    monkeypatch.setattr(paths, "legacy_settings_file", lambda: legacy_settings)

    loaded = project_model.load_settings()

    assert loaded["workspace_root"].endswith("old-workspace")
    assert not new_settings.exists(), "损坏的新设置应归档，而不应被覆盖"
    assert list(new_settings.parent.glob("settings.json.corrupt-*")), "应保留损坏设置留档"
    assert legacy_settings.is_file(), "legacy 设置必须保持只读原样"
