"""Jiadun（价盾）的运行时版本读取入口。

源码工作树以仓库根目录的 ``pyproject.toml`` 为唯一版本真源；安装后的
wheel/应用包没有该文件时，才回退到当前发行包的 metadata。所有需要把
版本写入 Run Contract、验收报告或导出成果的 Python 入口都应调用
``app_version``，避免各自维护正则和回退顺序。
"""
from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

from jiadun import branding


def _find_project_root(start: Path | None = None) -> Path | None:
    """从模块位置或给定路径向上寻找包含 pyproject.toml 的仓库根。"""
    candidate = Path(start or __file__).resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for parent in (candidate, *candidate.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def read_project_version(root: Path | None = None) -> str | None:
    """读取指定项目的 ``[project].version``；文件缺失或格式错误返回 None。"""
    project_root = Path(root).resolve() if root is not None else _find_project_root()
    if project_root is None:
        return None
    try:
        with (project_root / "pyproject.toml").open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    value = project.get("version")
    return str(value).strip() if value not in (None, "") else None


def _metadata_version() -> str | None:
    """读取安装 metadata；旧 costguard 仅作为只读兼容回退。"""
    for distribution in (branding.PRODUCT_SLUG, branding.LEGACY_PRODUCT_SLUG):
        try:
            return importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def app_version(*, root: Path | None = None) -> str:
    """返回当前应用版本，未知时返回明确的 ``unknown`` 而不是伪造版本。"""
    return read_project_version(root) or _metadata_version() or "unknown"


__all__ = ["app_version", "read_project_version"]
