# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：macOS Apple Silicon CostGuard.app（onedir + BUNDLE）。

可重复构建约束：
- 所有路径从本文件位置解析（不依赖调用时的工作目录）；
- 版本号与最低 macOS 版本由构建脚本经环境变量注入（来自 pyproject 与实测 minos）；
- 演示数据从仓库 examples/demo 原样打包进 costguard_resources/demo；
- 不含任何本地私有目录（local_private_data 被 .gitignore 隔离且从未被引用）。
"""
from __future__ import annotations

import os
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    for cand in (start, *start.parents):
        if (cand / "pyproject.toml").is_file():
            return cand
    raise RuntimeError(f"pyproject.toml not found from {start}")


SPEC_DIR = Path(SPECPATH).resolve()
REPO = _find_repo_root(SPEC_DIR)
VERSION = os.environ.get("COSTGUARD_VERSION", "0.0.0")
MIN_MACOS = os.environ.get("COSTGUARD_MIN_MACOS", "15.0")

DEMO_SRC = REPO / "examples" / "demo"
ICON_SRC = REPO / "src" / "costguard" / "resources" / "icon.icns"

a = Analysis(
    [str(REPO / "src" / "costguard" / "app.py")],
    pathex=[str(REPO / "src")],
    binaries=[],
    datas=[
        (str(DEMO_SRC), "costguard_resources/demo"),
        (str(ICON_SRC), "costguard_resources"),
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

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CostGuard",
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
    name="CostGuard",
)

app = BUNDLE(
    coll,
    name="CostGuard.app",
    icon=str(ICON_SRC),
    bundle_identifier="io.github.fahaxiki67.costguard",
    info_plist={
        "CFBundleName": "CostGuard",
        "CFBundleDisplayName": "CostGuard",
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
