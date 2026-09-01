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
    run_golden: bool = True,
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

    # 黄金回归是发布前的确定性门槛。合成演示案例用于验证执行器和当前
    # 代码基线；是否已登记脱敏真实案例另列为条件，不把“无真实案例”
    # 偷换成回归通过，也不自动更新 cases.json。
    if run_golden:
        registry = root / "tests" / "golden" / "cases.json"
        if not registry.is_file():
            _check(checks, "golden_regression", False, f"缺少 {registry}")
            issues.append("缺少 tests/golden/cases.json，无法执行黄金回归")
        elif root.resolve() == REPO_ROOT.resolve():
            try:
                # 直接执行 ``python scripts/release_consistency_check.py`` 时，
                # Python 的 sys.path[0] 是 scripts/，仓库根目录不会自动进入
                # import 搜索路径；测试环境从仓库根目录导入时则不暴露这个问题。
                # 这里显式补入当前受检根目录，保证发布门槛在命令行和测试中
                # 使用同一条黄金回归路径。
                root_text = str(root.resolve())
                if root_text not in sys.path:
                    sys.path.insert(0, root_text)
                from scripts import golden_regression

                golden = golden_regression.run_golden_regression(registry)
            except Exception as exc:  # noqa: BLE001 - 发布门槛需保留失败原因
                _check(checks, "golden_regression", False, f"执行失败: {type(exc).__name__}: {exc}")
                issues.append(f"黄金回归执行失败: {type(exc).__name__}: {exc}")
            else:
                passed = golden.get("status") == "passed"
                detail = (
                    f"status={golden.get('status')}, available={golden.get('available_case_count')}, "
                    f"real={golden.get('real_case_count')}, mismatches={golden.get('mismatch_case_count')}"
                )
                _check(checks, "golden_regression", passed, detail)
                if not passed:
                    issues.append("黄金回归存在差异或失败案例")
        else:
            # 临时文档一致性测试或外部镜像根目录不应误用当前 checkout
            # 的输入；保留明确的未执行状态，由真实发布根目录负责运行。
            _check(checks, "golden_regression", False, "非当前仓库根目录，未执行黄金回归")
            issues.append("非当前仓库根目录，未执行黄金回归")

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
