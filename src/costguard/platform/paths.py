"""平台差异隔离层：路径。

core/ 代码禁止自行处理平台路径差异，必须经由本模块。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")


def config_dir() -> Path:
    """软件自身配置目录（与工程数据分离）。"""
    if IS_MACOS:
        base = Path.home() / "Library" / "Application Support"
    elif IS_WINDOWS:
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path.home() / ".config"
    d = base / "CostGuard"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_workspace_root() -> Path:
    """默认工程工作空间根目录（用户可在 UI 更改）。"""
    if IS_WINDOWS:
        docs = Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents"
    else:
        docs = Path.home() / "Documents"
    d = docs / "CostGuardProjects"
    return d


def settings_file() -> Path:
    """全局设置文件（记录工作空间根位置等，不含敏感信息）。"""
    d = config_dir()
    return d / "settings.json"


def reveal_in_file_manager(path: Path) -> None:
    """在系统文件管理器中显示文件/目录。"""
    if IS_MACOS:
        import subprocess

        subprocess.run(["open", "-R", str(path)], check=False)
    elif IS_WINDOWS:
        import subprocess

        if path.is_file():
            subprocess.run(["explorer", "/select,", str(path)], check=False)
        else:
            subprocess.run(["explorer", str(path)], check=False)
    else:
        import subprocess

        subprocess.run(["xdg-open", str(path if path.is_dir() else path.parent)], check=False)
