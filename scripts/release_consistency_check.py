"""检查源码版本、发布说明和当前预览定位是否一致。

该检查只读仓库文件，不生成或修改任何发布物。它用于把“代码版本、文档版本、
当前是否仍为预览候选”变成可重复的发布前门槛，避免文档误把候选构建写成正式版。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_DOC_FILES = (
    Path("README.md"),
    Path("README_zh-CN.md"),
    Path("ARCHITECTURE.md"),
    Path("ROADMAP.md"),
    Path("CHANGELOG.md"),
)


def _read_version(root: Path) -> str | None:
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"(?ms)^\[project\].*?^version\s*=\s*[\"']([^\"']+)[\"']", text)
    return match.group(1) if match else None


def _check(checks: list[dict[str, Any]], name: str, passed: bool, detail: str) -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})


def check_release_consistency(
    root: Path = REPO_ROOT,
    *,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """返回可序列化的检查结果；不通过项列在 ``issues`` 中。"""
    root = Path(root)
    checks: list[dict[str, Any]] = []
    issues: list[str] = []
    version = _read_version(root)
    expected = expected_version or version

    _check(checks, "pyproject_version_present", version is not None, f"version={version!r}")
    if version is None:
        issues.append("pyproject.toml 未找到 [project].version")
    if expected is not None:
        same = version == expected
        _check(checks, "expected_version", same, f"expected={expected!r}, actual={version!r}")
        if not same:
            issues.append(f"源码版本 {version!r} 与期望版本 {expected!r} 不一致")

    release_note_relative = (
        Path(f"docs/RELEASE_NOTES_v{expected}.md") if expected is not None else None
    )
    doc_files = BASE_DOC_FILES + ((release_note_relative,) if release_note_relative else ())
    loaded: dict[Path, str] = {}
    for relative in doc_files:
        path = root / relative
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            _check(checks, f"file:{relative}", False, str(exc))
            issues.append(f"缺少或无法读取 {relative}")
            continue
        loaded[relative] = text
        version_ok = expected is None or f"v{expected}" in text or f"{expected}" in text
        _check(checks, f"version_in:{relative}", version_ok, f"version={expected!r}")
        if not version_ok:
            issues.append(f"{relative} 未出现当前版本 {expected}")

    markers = {
        Path("README.md"): ("preview", "production"),
        Path("README_zh-CN.md"): ("预览候选", "正式生产版"),
        Path("ROADMAP.md"): (f"v{expected} 生产门槛", "- [ ]"),
        Path("CHANGELOG.md"): (f"[{expected}]", "production"),
    }
    if release_note_relative:
        markers[release_note_relative] = ("预览候选", "生产")
    for relative, required in markers.items():
        text = loaded.get(relative, "")
        missing = [marker for marker in required if marker not in text]
        passed = not missing
        _check(checks, f"status_markers:{relative}", passed, f"missing={missing}")
        if missing:
            issues.append(f"{relative} 缺少状态标识: {', '.join(missing)}")

    # 品牌迁移后的当前文档必须能明确识别为 Jiadun/价盾。历史发布物名称
    # 仍允许在兼容说明中出现 CostGuard，因此这里只检查新品牌是否存在，
    # 不做不安全的全局旧名禁用。
    missing_brand = [
        str(relative)
        for relative, text in loaded.items()
        if not any(token in text for token in ("Jiadun", "价盾", "jiadun"))
    ]
    _check(checks, "current_brand_present", not missing_brand, f"missing={missing_brand}")
    if missing_brand:
        issues.append(f"当前文档缺少 Jiadun/价盾 品牌标识: {', '.join(missing_brand)}")

    release_note = loaded.get(release_note_relative, "") if release_note_relative else ""
    gate_terms = (
        ("WPS", ("WPS",)),
        ("macOS Excel", ("macOS Excel",)),
        ("Windows Excel", ("Windows Excel",)),
        ("1万", ("1万",)),
        ("5万", ("5万",)),
        ("20万", ("20万",)),
        ("公证", ("公证", "notarization")),
    )
    missing_gates = [label for label, alternatives in gate_terms
                     if not any(term in release_note for term in alternatives)]
    _check(checks, "preview_release_gates_listed", not missing_gates, f"missing={missing_gates}")
    if missing_gates:
        issues.append(f"发布说明未列出门槛: {', '.join(missing_gates)}")

    return {
        "ok": not issues,
        "version": version,
        "expected_version": expected,
        "checks": checks,
        "issues": issues,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查 Jiadun 发布版本与文档一致性")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="仓库根目录")
    parser.add_argument("--version", dest="expected_version", help="期望版本；默认读取 pyproject.toml")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = check_release_consistency(args.root, expected_version=args.expected_version)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"版本: {result['version'] or '无法读取'}")
        for check in result["checks"]:
            print(f"{'PASS' if check['passed'] else 'FAIL'}  {check['name']}: {check['detail']}")
        print("结果: " + ("PASS" if result["ok"] else "FAIL"))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
