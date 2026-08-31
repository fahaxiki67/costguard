# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：macOS Apple Silicon Jiadun.app（onedir + BUNDLE）。

可重复构建约束：
- 所有路径从本文件位置解析（不依赖调用时的工作目录）；
- 版本号与最低 macOS 版本由构建脚本经环境变量注入（来自 pyproject 与实测 minos）；
- 演示数据从仓库 examples/demo 原样打包进 jiadun_resources/demo；
- 不含任何本地私有目录（local_private_data 被 .gitignore 隔离且从未被引用）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    for cand in (start, *start.parents):
        if (cand / "pyproject.toml").is_file():
            return cand
    raise RuntimeError(f"pyproject.toml not found from {start}")


SPEC_DIR = Path(SPECPATH).resolve()
REPO = _find_repo_root(SPEC_DIR)
sys.path.insert(0, str(REPO / "src"))
from jiadun import branding


def _compat_env(current: str, legacy: str, default: str) -> str:
    """读取新环境变量；旧变量仅作兼容回退，冲突时失败关闭。"""
    value = os.environ.get(current)
    legacy_value = os.environ.get(legacy)
    if value and legacy_value and value != legacy_value:
        raise RuntimeError(f"{current} 与 {legacy} 不一致，拒绝继续打包")
    return value or legacy_value or default


VERSION = _compat_env("JIADUN_VERSION", "COSTGUARD_VERSION", "0.0.0")
MIN_MACOS = _compat_env("JIADUN_MIN_MACOS", "COSTGUARD_MIN_MACOS", "15.0")

DEMO_SRC = REPO / "examples" / "demo"
ICON_SRC = REPO / "src" / branding.PRODUCT_SLUG / "resources" / "icon.icns"

a = Analysis(
    [str(REPO / "src" / branding.PRODUCT_SLUG / "app.py")],
    pathex=[str(REPO / "src")],
    binaries=[],
    datas=[
        (str(DEMO_SRC), f"{branding.RESOURCE_DIR_NAME}/demo"),
        (str(ICON_SRC), branding.RESOURCE_DIR_NAME),
    ],
    hiddenimports=[],
    excludes=[
        "tkinter",
        "PyQt5",
        "IPython",
        "pytest",
        "hypothesis",
        "pytest_cov",
    ],
    noarchive=False,
)

# editable 安装会生成含本机仓库路径的 ``direct_url.json``。该文件只描述
# 开发环境安装来源，应用运行不依赖它；排除后保留包的 ``METADATA``，使
# importlib.metadata 仍可读取 Jiadun 版本，同时避免把构建机路径带入 DMG。
a.datas = [
    entry for entry in a.datas
    if Path(str(entry[0])).name != "direct_url.json"
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=branding.PRODUCT_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=branding.PRODUCT_NAME,
)

app = BUNDLE(
    coll,
    name=f"{branding.PRODUCT_NAME}.app",
    icon=str(ICON_SRC),
    # legacy compatibility: 保持既有 macOS 应用身份，避免签名/钥匙串/设置
    # 因品牌显示名迁移而分裂。Bundle identifier 变更需单独发布迁移。
    bundle_identifier=branding.BUNDLE_IDENTIFIER,
    info_plist={
        "CFBundleName": branding.PRODUCT_NAME,
        "CFBundleDisplayName": branding.PRODUCT_DISPLAY_NAME,
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": MIN_MACOS,
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.business",
        "NSPrincipalClass": "NSApplication",
        "CFBundleDevelopmentRegion": "zh-Hans",
        "CFBundleAllowMixedLocalizations": True,
    },
)
