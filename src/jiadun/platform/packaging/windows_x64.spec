# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：Windows x64 Jiadun（onedir，GUI）。

与 macOS spec 共享同一应用入口与演示数据资源（AGENTS：不拆成两个软件）；
差异只在平台层：无 BUNDLE，主程序 Jiadun.exe，图标 .ico。
版本号与架构由构建脚本经环境变量注入；不引用任何本机私有路径。
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
VERSION = os.environ.get("JIADUN_VERSION", "0.0.0")

DEMO_SRC = REPO / "examples" / "demo"
ICON_SRC = REPO / "src" / "jiadun" / "resources" / "icon.ico"

a = Analysis(
    [str(REPO / "src" / "jiadun" / "app.py")],
    pathex=[str(REPO / "src")],
    binaries=[],
    datas=[
        (str(DEMO_SRC), "jiadun_resources/demo"),
        (str(ICON_SRC), "jiadun_resources"),
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
    name="Jiadun",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON_SRC),
    version=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Jiadun",
)
