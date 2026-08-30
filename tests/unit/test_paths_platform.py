"""Windows 路径分支回归测试（issue #1）。

pathlib.Path 没有 expandvars（那是 os.path.expandvars），曾经写成
Path(Path.expandvars(...)) 导致 Windows 上测试收集与 GUI 启动即抛
AttributeError，而 macOS 分支掩盖了问题。现改为直接读环境变量并带
缺省回退；这里在任意平台通过 monkeypatch 强制走 Windows 分支锁定行为。
"""
from __future__ import annotations

import costguard.platform.paths as paths


def test_config_dir_windows_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "IS_MACOS", False)
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = paths.config_dir()
    assert d == tmp_path / "CostGuard"
    assert d.is_dir()


def test_config_dir_windows_fallback_when_appdata_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "IS_MACOS", False)
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    d = paths.config_dir()
    assert d == tmp_path / "AppData" / "Roaming" / "CostGuard"


def test_default_workspace_root_windows_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    d = paths.default_workspace_root()
    assert d == tmp_path / "Documents" / "CostGuardProjects"


def test_default_workspace_root_windows_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))
    d = paths.default_workspace_root()
    assert d == tmp_path / "Documents" / "CostGuardProjects"
