"""平台差异隔离层：路径。

core/ 代码禁止自行处理平台路径差异，必须经由本模块。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from jiadun import branding

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")


def _config_base() -> Path:
    """返回平台配置根目录，不创建任何目录。"""
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support"
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    return Path.home() / ".config"


def config_dir() -> Path:
    """价盾当前配置目录（与工程数据分离）。

    新安装始终写入 ``Jiadun``。旧 ``CostGuard`` 目录只通过
    :func:`legacy_config_dir` 暴露给读取兼容层，避免品牌迁移时改写旧设置。
    """
    d = _config_base() / branding.CONFIG_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def legacy_config_dir() -> Path:
    """返回旧版配置目录；只读发现，不创建、不迁移、不删除。"""
    return _config_base() / branding.LEGACY_CONFIG_DIR_NAME


def default_workspace_root() -> Path:
    """价盾默认工程工作空间根目录（用户可在 UI 更改）。"""
    if IS_WINDOWS:
        docs = Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents"
    else:
        docs = Path.home() / "Documents"
    return docs / branding.WORKSPACE_DIR_NAME


def legacy_workspace_root() -> Path:
    """返回旧版工程工作空间；只读扫描，不创建、不移动、不覆盖。"""
    if IS_WINDOWS:
        docs = Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents"
    else:
        docs = Path.home() / "Documents"
    return docs / branding.LEGACY_WORKSPACE_DIR_NAME


def settings_file() -> Path:
    """价盾当前全局设置文件（记录工作空间根位置等，不含敏感信息）。"""
    d = config_dir()
    return d / "settings.json"


def legacy_settings_file() -> Path:
    """返回旧版设置文件；仅供缺少新设置时只读回退。"""
    return legacy_config_dir() / "settings.json"


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
