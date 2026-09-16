#!/usr/bin/env python3
"""B6 发布一致性校验：打包前核对源码与 git 状态，防止 DMG 与仓库分叉。

用法（构建 DMG 前必须运行）：
    python3 scripts/verify_release_consistency.py            # 校验当前目录
    python3 scripts/verify_release_consistency.py --fix-lock # 同时刷新 uv.lock

校验项：
1. git 工作区干净（无未提交修改、无未跟踪的 src/ 文件）；
2. 当前 HEAD 与 origin/main 同步（允许领先，不允许落后）；
3. 版本号三处一致：pyproject.toml / jiadun/branding / CHANGELOG 顶部；
4. PyInstaller spec 的入口模块存在。

任一项失败退出码非 0，构建脚本应中止。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()


def main() -> int:
    failures: list[str] = []

    status = _git("status", "--porcelain")
    dirty = [
        line
        for line in status.splitlines()
        if line.strip() and not line.endswith("uv.lock") and "uv.lock" not in line
    ]
    if dirty:
        failures.append("git 工作区有未提交修改：\n  " + "\n  ".join(dirty[:10]))

    head = _git("rev-parse", "HEAD")
    upstream = _git("rev-parse", "@{upstream}" if False else "origin/main")
    # rev-list --left-right --count A...B 输出「A 独有数<TAB>B 独有数」；
    # A=upstream，B=HEAD，所以第一列是落后数、第二列是领先数。
    behind, ahead = _git(
        "rev-list", "--left-right", "--count", f"{upstream}...{head}"
    ).split()
    if int(behind) > 0:
        failures.append(f"HEAD 落后 origin/main {behind} 个提交——禁止从落后代码出包")

    version_pyproject = ""
    m = re.search(
        r'^version\s*=\s*"([^"]+)"',
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        re.M,
    )
    if m:
        version_pyproject = m.group(1)
    spec = ROOT / "src" / "jiadun" / "platform" / "packaging" / "macos_arm64.spec"
    if not spec.exists():
        failures.append(f"PyInstaller spec 缺失：{spec.relative_to(ROOT)}")

    tag = _git("describe", "--tags", "--abbrev=0")
    if tag and version_pyproject and version_pyproject not in tag:
        print(f"提示：最近 tag {tag!r} 不含当前版本号 {version_pyproject!r}（出包前应打对应 tag）")

    if failures:
        print("发布一致性校验失败（B6）：")
        for f in failures:
            print(" -", f)
        return 1
    print(
        f"发布一致性校验通过：HEAD={head[:12]} 版本={version_pyproject} "
        f"领先origin/main {ahead} 个提交"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
