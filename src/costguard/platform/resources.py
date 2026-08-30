"""打包资源定位（ADR-007 平台层）。

PyInstaller 打包后资源位于 sys._MEIPASS 下的 costguard_resources/；
源码/开发模式直接解析仓库内 examples/demo 与 src/costguard/resources。
core/ 与 ui/ 不得自行探测打包路径，一律经由本模块。
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物中。"""
    return getattr(sys, "frozen", False)


def _bundle_base() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))


def bundled_demo_dir() -> Path:
    """安装包内置演示数据目录（与 examples/demo 同构：xlsx/docx + manifest.json）。"""
    if is_frozen():
        cand = _bundle_base() / "costguard_resources" / "demo"
        if cand.is_dir():
            return cand
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "examples" / "demo"
        if cand.is_dir():
            return cand
    raise FileNotFoundError(
        "未找到演示数据目录：打包产物缺少 costguard_resources/demo，"
        "或源码运行时未在仓库内（请检查打包 spec 与仓库结构）")


def app_icon_path() -> Path | None:
    """应用图标（.icns/.png）；不存在时返回 None（UI 使用系统默认图标）。"""
    candidates: list[Path] = []
    if is_frozen():
        candidates.append(_bundle_base() / "costguard_resources" / "icon.icns")
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates.append(parent / "src" / "costguard" / "resources" / "icon.icns")
        candidates.append(parent / "costguard" / "resources" / "icon.icns")
    for cand in candidates:
        if cand.is_file():
            return cand
    return None
