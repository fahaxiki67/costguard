# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：Windows x64 Jiadun（onedir，GUI）。

与 macOS spec 共享同一应用入口与演示数据资源（AGENTS：不拆成两个软件）；
差异只在平台层：无 BUNDLE，主程序 Jiadun.exe，图标 .ico。
版本号与架构由构建脚本经环境变量注入；不引用任何本机私有路径。

VERSIONINFO 与 manifest 在构建时生成到 build/ 元数据目录：
- VERSIONINFO：资源管理器文件属性（产品名/版本/Publisher/架构）；
- manifest：PerMonitorV2 DPI 感知 + 长路径感知（不改变 ANSI 代码页语义）。
"""
from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


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
OCR_DATAS, OCR_BINARIES, OCR_HIDDENIMPORTS = collect_all("rapidocr_onnxruntime")

_META_DIR = REPO / "build" / "jiadun_pe_meta"
_META_DIR.mkdir(parents=True, exist_ok=True)

_VERSION_PARTS = [int(p) for p in VERSION.split(".")] + [0] * (4 - len(VERSION.split(".")))
_V1, _V2, _V3, _V4 = _VERSION_PARTS[:4]

VERSION_FILE = _META_DIR / "jiadun_version_info.txt"
VERSION_FILE.write_text(f"""# UTF-8（PyInstaller 读取生成 VS_VERSIONINFO 资源）
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({_V1}, {_V2}, {_V3}, {_V4}),
    prodvers=({_V1}, {_V2}, {_V3}, {_V4}),
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Jiadun project'),
        StringStruct('FileDescription', 'Jiadun（价盾）工程经营合规智能工作台'),
        StringStruct('FileVersion', '{VERSION}'),
        StringStruct('InternalName', 'Jiadun'),
        StringStruct('LegalCopyright', 'Apache-2.0 License'),
        StringStruct('OriginalFilename', 'Jiadun.exe'),
        StringStruct('ProductName', 'Jiadun（价盾）'),
        StringStruct('ProductVersion', '{VERSION}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200, 1033, 1200])]),
  ]
)""", encoding="utf-8")

MANIFEST_FILE = _META_DIR / "jiadun.manifest"
MANIFEST_FILE.write_text(f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity type="win32" name="Jiadun" version="{VERSION}.0" processorArchitecture="*"/>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security>
  </trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application>
      <supportedOS Id="{{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}}"/>
      <supportedOS Id="{{1f676c76-80e1-4239-95bb-83d0f6d0da78}}"/>
    </application>
  </compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>
      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2, system</dpiAwareness>
      <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
    </windowsSettings>
  </application>
  <dependency>
    <dependentAssembly>
      <assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0"
        processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*"/>
    </dependentAssembly>
  </dependency>
</assembly>""", encoding="utf-8")

a = Analysis(
    [str(REPO / "src" / "jiadun" / "app.py")],
    pathex=[str(REPO / "src")],
    binaries=OCR_BINARIES,
    datas=[
        (str(DEMO_SRC), "jiadun_resources/demo"),
        (str(ICON_SRC), "jiadun_resources"),
        *OCR_DATAS,
    ],
    hiddenimports=OCR_HIDDENIMPORTS,
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
    version=str(VERSION_FILE),
    manifest=str(MANIFEST_FILE),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Jiadun",
)
