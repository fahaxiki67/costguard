"""检查源码版本、发布说明和当前预览定位是否一致。

该检查只读仓库文件，不生成或修改任何发布物。它用于把“代码版本、文档版本、
当前是否仍为预览候选”变成可重复的发布前门槛，避免文档误把候选构建写成正式版。
生产发布默认要求至少一个脱敏真实黄金案例；本地开发检查若确实需要在真实案例
补齐前继续运行，必须显式传入 ``allow_no_real=True`` 或命令行
``--allow-no-real``，该模式会保留有条件状态且不会把结果标记为生产可发布。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from jiadun.version import read_project_version  # noqa: E402

BASE_DOC_FILES = (
    Path("README.md"),
    Path("README_zh-CN.md"),
    Path("ARCHITECTURE.md"),
    Path("ROADMAP.md"),
    Path("CHANGELOG.md"),
)


def _read_version(root: Path) -> str | None:
    return read_project_version(root)


def _check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    detail: str,
    *,
    status: str | None = None,
) -> None:
    check: dict[str, Any] = {"name": name, "passed": bool(passed), "detail": detail}
    if status is not None:
        check["status"] = status
    checks.append(check)


def check_release_consistency(
    root: Path = REPO_ROOT,
    *,
    expected_version: str | None = None,
    run_golden: bool = True,
    allow_no_real: bool = False,
) -> dict[str, Any]:
    """返回可序列化的检查结果；不通过项列在 ``issues`` 中。"""
    root = Path(root)
    checks: list[dict[str, Any]] = []
    issues: list[str] = []
    real_case_count: int | None = None
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

    # 黄金回归是发布前的确定性门槛。合成演示和脱敏真实登记表都必须
    # 执行；“尚无真实案例”默认阻断生产发布，不能被合成案例冒充。开发
    # 检查只有显式 allow_no_real 才能保留为 conditional。
    if run_golden:
        registry = root / "tests" / "golden" / "cases.json"
        anonymized_registry = root / "tests" / "anonymized_golden_cases" / "cases.json"
        if not registry.is_file() or not anonymized_registry.is_file():
            missing = [str(path.relative_to(root)) for path in (registry, anonymized_registry) if not path.is_file()]
            _check(
                checks,
                "golden_regression",
                False,
                f"缺少 {', '.join(missing)}；生产发布门禁失败",
                status="failed",
            )
            issues.append("缺少合成或脱敏真实黄金登记表，无法执行完整黄金回归")
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

                golden = golden_regression.run_golden_regression_suite(
                    registries=(registry, anonymized_registry)
                )
            except Exception as exc:  # noqa: BLE001 - 发布门槛需保留失败原因
                _check(
                    checks,
                    "golden_regression",
                    False,
                    f"执行失败: {type(exc).__name__}: {exc}",
                    status="failed",
                )
                issues.append(f"黄金回归执行失败: {type(exc).__name__}: {exc}")
            else:
                real_case_count = int(golden.get("real_case_count") or 0)
                regression_passed = golden.get("status") == "passed"
                expected_total: object | None = golden.get("case_count")
                if expected_total is None and (
                    "available_case_count" in golden or "not_available_case_count" in golden
                ):
                    try:
                        expected_total = (
                            golden.get("available_case_count", 0)
                            + golden.get("not_available_case_count", 0)
                        )
                    except TypeError:
                        expected_total = "invalid"
                comparison_counts, canonical_errors = (
                    golden_regression.normalize_comparison_status_counts(
                        golden.get("comparison_status_counts"),
                        expected_total=expected_total,
                    )
                )
                pending_count = comparison_counts.get("PENDING", 0)
                incomparable_count = comparison_counts.get("INCOMPARABLE", 0)
                fail_count = comparison_counts.get("FAIL", 0)
                known_statuses = {"PASS", "FAIL", "PENDING", "INCOMPARABLE"}
                unknown_count = sum(
                    count
                    for status, count in comparison_counts.items()
                    if status not in known_statuses
                )
                canonical_detail = ", ".join(
                    f"{status}={count}"
                    for status, count in sorted(comparison_counts.items())
                )
                canonical_error_detail = (
                    f"canonical_errors={'；'.join(canonical_errors)}, "
                    if canonical_errors
                    else ""
                )
                unavailable_count = int(golden.get("not_available_case_count") or 0)
                pending_only_unavailable = (
                    pending_count > 0
                    and pending_count == unavailable_count
                    and real_case_count == 0
                )
                if (
                    not regression_passed
                    or canonical_errors
                    or fail_count
                    or incomparable_count
                    or unknown_count
                ):
                    gate_status = "failed"
                    passed = False
                    issues.append(
                        "黄金回归 canonical comparison_status 存在不可放行项："
                        f"{canonical_detail}"
                        + (f"；校验错误：{'；'.join(canonical_errors)}" if canonical_errors else "")
                    )
                elif real_case_count == 0 and not allow_no_real:
                    gate_status = "blocked"
                    passed = False
                    issues.append(
                        "生产发布被阻断：真实黄金案例回归 real_case_count=0；"
                        "补齐脱敏真实案例，或仅在开发检查中显式使用 allow_no_real"
                    )
                elif real_case_count == 0 and allow_no_real and (
                    pending_count == 0 or pending_only_unavailable
                ):
                    gate_status = "conditional"
                    passed = True
                elif pending_count:
                    gate_status = "blocked"
                    passed = False
                    issues.append(
                        "黄金回归仍有 PENDING 案例，不能形成生产结论："
                        f"{canonical_detail}"
                    )
                else:
                    gate_status = "passed"
                    passed = True
                detail = (
                    f"status={golden.get('status')}, available={golden.get('available_case_count')}, "
                    f"real_case_count={real_case_count}, mismatches={golden.get('mismatch_case_count')}, "
                    f"comparison_status_counts={canonical_detail}, "
                    f"{canonical_error_detail}"
                    f"gate_status={gate_status}, allow_no_real={allow_no_real}"
                )
                _check(checks, "golden_regression", passed, detail, status=gate_status)
                if not regression_passed:
                    issues.append("黄金回归存在差异或失败案例")
        else:
            # 临时文档一致性测试或外部镜像根目录不应误用当前 checkout
            # 的输入；保留明确的未执行状态，由真实发布根目录负责运行。
            _check(
                checks,
                "golden_regression",
                False,
                "非当前仓库根目录，未执行黄金回归；生产发布门禁失败",
                status="failed",
            )
            issues.append("非当前仓库根目录，未执行黄金回归")

    return {
        "ok": not issues,
        "allow_no_real": bool(allow_no_real),
        "real_case_count": real_case_count,
        "production_release_ready": bool(
            not issues and run_golden and real_case_count is not None and real_case_count > 0
        ),
        "version": version,
        "expected_version": expected,
        "checks": checks,
        "issues": issues,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查 Jiadun 发布版本与文档一致性")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="仓库根目录")
    parser.add_argument("--version", dest="expected_version", help="期望版本；默认读取 pyproject.toml")
    parser.add_argument(
        "--allow-no-real",
        action="store_true",
        help="仅开发检查时允许 real_case_count=0；不会使生产发布门禁通过",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    return parser


def main(argv: list[str] | None = None) -> int:
    # CI/英文 Windows 控制台默认 cp1252，输出中文会 UnicodeEncodeError；统一 UTF-8。
    for _stream in (sys.stdout, sys.stderr):
        try:
            if _stream is not None and getattr(_stream, "encoding", "").lower() not in ("utf-8", "utf8"):
                _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
    args = build_parser().parse_args(argv)
    result = check_release_consistency(
        args.root,
        expected_version=args.expected_version,
        allow_no_real=args.allow_no_real,
    )
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
