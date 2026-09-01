"""生成 Jiadun（价盾）发布前清单（P0 发布质量要求）。

清单把已经执行的结果、尚未执行的门禁和明确的限制放在同一份机器可读
记录中。任何未执行、不可用或仅具备条件的项目都不会被写成 ``passed``；
输出只写入仓库的 ``dist/`` 或用户指定的独立目录，不读取或写入
``local_private_data/``。

常用方式：

    uv run python scripts/release_checklist.py --run-tests
    uv run python scripts/release_checklist.py --run-tests --run-performance

性能基准耗时较长，未显式指定 ``--run-performance`` 时清单会保留
1万/5万/20万行门禁为 ``not_run``，不能据此宣称生产通过。没有脱敏真实黄金案例
时，生产发布门禁默认 ``failed``；本地开发检查必须显式使用 ``--allow-no-real``，
并仍保留 ``conditional`` 与 ``production_release_ready=false``。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PART = "local_private_data"
PERFORMANCE_SIZES = (10_000, 50_000, 200_000)

# 直接以 ``python scripts/release_checklist.py`` 运行时，Python 默认只把
# scripts/ 放进 sys.path；显式加入仓库根，保证脚本和 ``uv run``/pytest
# 入口使用同一套包解析规则。
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _version(root: Path) -> str | None:
    import re

    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"(?ms)^\[project\].*?^version\s*=\s*[\"']([^\"']+)[\"']", text)
    return match.group(1) if match else None


def _git_commit(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def _private_path(path: Path) -> bool:
    return PRIVATE_PART in path.resolve().parts


def _item(
    item_id: str,
    name: str,
    status: str,
    *,
    evidence: str | None = None,
    detail: str | None = None,
    command: Sequence[str] | None = None,
    gate_status: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": item_id,
        "name": name,
        "status": status,
    }
    if evidence is not None:
        item["evidence"] = evidence
    if detail is not None:
        item["detail"] = detail
    if command is not None:
        item["command"] = list(command)
    if gate_status is not None:
        item["gate_status"] = gate_status
    return item


def _run_command(root: Path, command: Sequence[str]) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        completed = subprocess.run(
            list(command),
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return {"status": "failed", "returncode": None, "output": f"{type(exc).__name__}: {exc}"}
    output = (completed.stdout + "\n" + completed.stderr).strip()
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        # 清单只保留可读尾部，完整日志仍由 CI/调用者留存。
        "output": output[-4000:],
    }


def _performance_item(
    root: Path,
    *,
    output_dir: Path,
    run_performance: bool,
    performance_report: Path | None,
    keep_workspace: bool,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/performance_benchmark.py",
        "--sizes",
        *(str(size) for size in PERFORMANCE_SIZES),
    ]
    if performance_report is not None:
        report_path = Path(performance_report).resolve()
        if _private_path(report_path):
            raise ValueError("性能报告不能来自 local_private_data")
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _item(
                "performance_1w_5w_20w",
                "1万/5万/20万行性能与取消机制",
                "failed",
                evidence=str(report_path),
                detail=f"无法读取性能报告: {type(exc).__name__}: {exc}",
                command=command,
            )
        if not isinstance(report, dict):
            return _item(
                "performance_1w_5w_20w",
                "1万/5万/20万行性能与取消机制",
                "failed",
                evidence=str(report_path),
                detail="性能报告不是 JSON 对象。",
                command=command,
            )
        sizes = report.get("config", {}).get("sizes", [])
        size_set = {int(size) for size in sizes if str(size).isdigit()}
        result_statuses = [result.get("status") for result in report.get("results", [])]
        if not set(PERFORMANCE_SIZES).issubset(size_set):
            status = "failed"
            detail = f"报告规模不完整：expected={list(PERFORMANCE_SIZES)}, actual={sizes}"
        elif report.get("status") == "completed" and all(
            status == "completed" for status in result_statuses
        ):
            status = "passed"
            detail = f"已读取现场报告；sizes={sizes}，results={len(result_statuses)}"
        elif report.get("status") == "cancelled":
            status = "conditional"
            detail = f"现场报告为 cancelled；sizes={sizes}，results={result_statuses}"
        else:
            status = "failed"
            detail = f"现场报告未完成：status={report.get('status')}, results={result_statuses}"
        return _item(
            "performance_1w_5w_20w",
            "1万/5万/20万行性能与取消机制",
            status,
            evidence=str(report_path),
            detail=detail,
            command=command,
        )
    if not run_performance:
        return _item(
            "performance_1w_5w_20w",
            "1万/5万/20万行性能与取消机制",
            "not_run",
            evidence="scripts/performance_benchmark.py",
            detail="未显式指定 --run-performance；不得把未执行的性能门禁当作通过。",
            command=command,
        )
    from scripts import performance_benchmark

    bench_dir = output_dir / "performance"
    bench_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = performance_benchmark.run_benchmark(
            PERFORMANCE_SIZES,
            output=bench_dir,
            keep_workspace=keep_workspace,
        )
    except Exception as exc:  # noqa: BLE001 - 清单必须记录门禁失败原因
        return _item(
            "performance_1w_5w_20w",
            "1万/5万/20万行性能与取消机制",
            "failed",
            evidence=str(bench_dir),
            detail=f"执行失败: {type(exc).__name__}: {exc}",
            command=command,
        )
    benchmark_status = report.get("status")
    # 取消是 P0-06 要求的受控终态：现场保留、可恢复，但不能把未完成的
    # 规模测试误报为失败或通过。只有明确的运行异常才是 failed。
    status = (
        "passed"
        if benchmark_status == "completed"
        else "conditional"
        if benchmark_status == "cancelled"
        else "failed"
    )
    return _item(
        "performance_1w_5w_20w",
        "1万/5万/20万行性能与取消机制",
        status,
        evidence=str(report.get("output_paths", {}).get("json", bench_dir)),
        detail=(
            f"status={report.get('status')}, sizes={report.get('config', {}).get('sizes')}, "
            f"results={len(report.get('results', []))}"
        ),
        command=command,
    )


def _office_item(root: Path) -> dict[str, Any]:
    steps = root / "docs" / "WPS_ACCEPTANCE_STEPS.md"
    if not steps.is_file():
        return _item(
            "office_four_environment",
            "WPS/Excel 四环境真机验收",
            "not_available",
            detail="缺少 WPS/Excel 验收步骤或记录。",
        )
    return _item(
        "office_four_environment",
        "WPS/Excel 四环境真机验收",
        "conditional",
        evidence=str(steps.relative_to(root)),
        detail=(
            "仓库有合成样例的人工 WPS 记录；WPS macOS、WPS Windows、"
            "Microsoft Excel macOS、Microsoft Excel Windows 的当前版本真机证据仍需逐项登记。"
        ),
    )


def _signature_item(root: Path, version: str | None) -> dict[str, Any]:
    sums = root / "dist" / "SHA256SUMS.txt"
    if not sums.is_file():
        return _item(
            "package_signature",
            "安装包签名状态",
            "not_available",
            detail="当前没有可核验的 dist/SHA256SUMS.txt。",
        )
    text = sums.read_text(encoding="utf-8", errors="replace")
    version_token = f"{version}-" if version else ""
    has_current = version_token in text or (version is not None and f"{version}." in text)
    return _item(
        "package_signature",
        "安装包签名状态",
        "conditional" if has_current else "not_available",
        evidence=str(sums.relative_to(root)),
        detail=(
            "当前版本的校验和文件已登记；签名、公证和证书链仍须以对应构建现场证据为准。"
            if has_current
            else "校验和文件不包含当前版本，不能当作当前安装包签名证据。"
        ),
    )


def build_checklist(
    root: Path = REPO_ROOT,
    *,
    version: str | None = None,
    run_tests: bool = False,
    run_performance: bool = False,
    performance_report: Path | None = None,
    keep_workspace: bool = False,
    run_golden: bool = True,
    allow_no_real: bool = False,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """构造发布清单；所有门禁都显式保留状态和证据边界。"""
    root = Path(root).resolve()
    current_version = version or _version(root)
    output_root = (Path(output_dir) if output_dir is not None else root / "dist").resolve()
    if _private_path(output_root):
        raise ValueError("发布清单输出不能写入 local_private_data")
    output_root.mkdir(parents=True, exist_ok=True)

    items: list[dict[str, Any]] = []
    real_case_count: int | None = None
    tests_command = [sys.executable, "-m", "pytest", "-q"]
    lint_command = [sys.executable, "-m", "ruff", "check", "src", "scripts", "tests"]
    migration_command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/unit/test_db_migrations.py",
        "tests/unit/test_run_contract.py",
    ]
    recovery_command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/unit/test_db_migrations.py",
    ]
    abnormal_command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/unit/test_performance_benchmark.py",
    ]
    if run_tests:
        tests = _run_command(root, tests_command)
        items.append(
            _item(
                "automated_tests",
                "自动化测试结果",
                tests["status"],
                evidence="pytest -q",
                detail=f"returncode={tests['returncode']}\n{tests['output']}",
                command=tests_command,
            )
        )
        lint = _run_command(root, lint_command)
        items.append(
            _item(
                "lint",
                "Ruff 代码质量检查",
                lint["status"],
                evidence="ruff check src scripts tests",
                detail=f"returncode={lint['returncode']}\n{lint['output']}",
                command=lint_command,
            )
        )
        migration = _run_command(root, migration_command)
        items.append(
            _item(
                "migration_tests",
                "迁移测试",
                migration["status"],
                evidence="tests/unit/test_db_migrations.py + tests/unit/test_run_contract.py",
                detail=f"returncode={migration['returncode']}\n{migration['output']}",
                command=migration_command,
            )
        )
        recovery = _run_command(root, recovery_command)
        items.append(
            _item(
                "backup_recovery_tests",
                "备份恢复测试",
                recovery["status"],
                evidence="tests/unit/test_db_migrations.py",
                detail=f"returncode={recovery['returncode']}\n{recovery['output']}",
                command=recovery_command,
            )
        )
        abnormal = _run_command(root, abnormal_command)
        items.append(
            _item(
                "abnormal_exit_tests",
                "异常退出/取消恢复测试",
                abnormal["status"],
                evidence="tests/unit/test_performance_benchmark.py",
                detail=f"returncode={abnormal['returncode']}\n{abnormal['output']}",
                command=abnormal_command,
            )
        )
    else:
        for item_id, name, command, evidence in (
            ("automated_tests", "自动化测试结果", tests_command, "pytest -q"),
            ("lint", "Ruff 代码质量检查", lint_command, "ruff check src scripts tests"),
            (
                "migration_tests",
                "迁移测试",
                migration_command,
                "tests/unit/test_db_migrations.py + tests/unit/test_run_contract.py",
            ),
            ("backup_recovery_tests", "备份恢复测试", recovery_command, "tests/unit/test_db_migrations.py"),
            (
                "abnormal_exit_tests",
                "异常退出/取消恢复测试",
                abnormal_command,
                "tests/unit/test_performance_benchmark.py",
            ),
        ):
            items.append(
                _item(
                    item_id,
                    name,
                    "not_run",
                    evidence=evidence,
                    detail="未指定 --run-tests；保留命令作为可恢复执行入口。",
                    command=command,
                )
            )

    if run_golden:
        registry = root / "tests" / "golden" / "cases.json"
        anonymized_registry = root / "tests" / "anonymized_golden_cases" / "cases.json"
        if root == REPO_ROOT and registry.is_file() and anonymized_registry.is_file():
            from scripts import golden_regression

            golden = golden_regression.run_golden_regression_suite(
                registries=(registry, anonymized_registry)
            )
            real_case_count = int(golden.get("real_case_count") or 0)
            if golden.get("status") != "passed":
                golden_status = "failed"
                golden_gate_status = "failed"
            elif real_case_count > 0:
                golden_status = "passed"
                golden_gate_status = "passed"
            elif allow_no_real:
                golden_status = "conditional"
                golden_gate_status = "development_override"
            else:
                golden_status = "failed"
                golden_gate_status = "blocked"
            golden_note = (
                "生产发布门禁已阻断：请补齐脱敏真实案例。"
                if golden_gate_status == "blocked"
                else "合成与脱敏登记表均已执行；真实脱敏案例数量仍需补齐。"
            )
            items.append(
                _item(
                    "golden_regression",
                    "真实黄金案例回归",
                    golden_status,
                    evidence="tests/golden/cases.json; tests/anonymized_golden_cases/cases.json",
                    detail=(
                        f"status={golden.get('status')}, available={golden.get('available_case_count')}, "
                        f"real_case_count={real_case_count}, mismatches={golden.get('mismatch_case_count')}；"
                        f"gate_status={golden_gate_status}, allow_no_real={allow_no_real}；{golden_note}"
                    ),
                    gate_status=golden_gate_status,
                )
            )
        else:
            items.append(
                _item(
                    "golden_regression",
                    "真实黄金案例回归",
                    "failed",
                    detail="当前根目录缺少可执行的黄金回归库；生产发布门禁失败。",
                    gate_status="failed",
                )
            )
    else:
        items.append(
            _item(
                "golden_regression",
                "真实黄金案例回归",
                "failed",
                evidence="tests/golden/cases.json",
                detail="未运行黄金回归；生产发布门禁失败。请移除 --skip-golden 后重试。",
                gate_status="blocked",
            )
        )

    items.append(_performance_item(
        root,
        output_dir=output_root,
        run_performance=run_performance,
        performance_report=performance_report,
        keep_workspace=keep_workspace,
    ))
    items.append(_office_item(root))
    items.append(_signature_item(root, current_version))

    from scripts import release_consistency_check

    consistency = release_consistency_check.check_release_consistency(
        root,
        expected_version=current_version,
        run_golden=False,
    )
    items.append(
        _item(
            "release_consistency",
            "版本、README、CHANGELOG 和发布说明一致性",
            "passed" if consistency["ok"] else "failed",
            evidence="scripts/release_consistency_check.py",
            detail="；".join(consistency["issues"]) or "版本与当前文档状态一致。",
        )
    )

    gap_doc = root / "docs" / "PHASE0_GAP_MATRIX_20260901.md"
    known_limitations = [
        "真实脱敏黄金案例尚未登记，合成案例不能替代真实项目回归。",
        "WPS macOS、WPS Windows、Microsoft Excel macOS、Microsoft Excel Windows 当前版本真机证据待补。",
        "活动 schema 只支持前向迁移和备份恢复，不提供活动 downgrade API。",
        "签名、公证、异常退出恢复和大规模性能需以本次发布现场记录为准。",
    ]
    unresolved = [
        "P0-04：真实黄金案例数量仍为 0。",
        "P0-05：四环境 WPS/Excel 真机验收未形成当前版本闭环。",
        "P0-06：1万/5万/20万行完整性能报告尚未在本清单运行。",
        "P1：差异雷达、字段模板、别名知识库、规则中心、Finding 闭环和项目首页已有核心实现；真实项目、UI 真机和跨库授权边界仍需逐项验收。",
        "P2-01/P2-03：历史单价库、项目版本链及 Excel 专用工作表/工作台入口已有受控核心路径；Word 字段级差异、跨项目资产复用和脱敏真实资产仍待补齐。",
    ]
    items.append(
        _item(
            "known_limitations",
            "已知限制",
            "passed" if gap_doc.is_file() else "not_available",
            evidence=str(gap_doc.relative_to(root)) if gap_doc.is_file() else None,
            detail="\n".join(known_limitations),
        )
    )
    items.append(
        _item(
            "unresolved_p0_p1",
            "未解决 P0/P1 Issue",
            "conditional",
            evidence=str(gap_doc.relative_to(root)) if gap_doc.is_file() else None,
            detail="\n".join(unresolved),
        )
    )

    statuses = {str(item["status"]) for item in items}
    overall = "failed" if "failed" in statuses else (
        "conditional" if statuses & {"conditional", "not_run", "not_available"} else "passed"
    )
    return {
        "schema_version": 1,
        "checklist_version": 1,
        "product": "Jiadun（价盾）",
        "version": current_version,
        "allow_no_real": bool(allow_no_real),
        "real_case_count": real_case_count,
        "generated_at": _now(),
        "source_commit": _git_commit(root),
        "environment": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "overall_status": overall,
        "production_release_ready": bool(
            overall == "passed" and real_case_count is not None and real_case_count > 0
        ),
        "items": items,
        "summary": {
            "passed": sum(item["status"] == "passed" for item in items),
            "failed": sum(item["status"] == "failed" for item in items),
            "conditional": sum(item["status"] == "conditional" for item in items),
            "not_run": sum(item["status"] == "not_run" for item in items),
            "not_available": sum(item["status"] == "not_available" for item in items),
        },
    }


def _output_paths(output: Path, version: str | None) -> tuple[Path, Path]:
    output = Path(output)
    if output.suffix.lower() == ".json":
        return output, output.with_suffix(".md")
    version_token = version or "unknown"
    return output / f"release-checklist-v{version_token}.json", output / f"release-checklist-v{version_token}.md"


def write_checklist(report: dict[str, Any], output: Path) -> tuple[Path, Path]:
    json_path, markdown_path = _output_paths(output, report.get("version"))
    for path in (json_path, markdown_path):
        if _private_path(path):
            raise ValueError("发布清单不能写入 local_private_data")
        path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        f"# Jiadun（价盾）v{report.get('version') or 'unknown'} 发布清单",
        "",
        f"- 生成时间：{report.get('generated_at')}",
        f"- 来源提交：{report.get('source_commit') or '未记录'}",
        f"- 总体状态：**{report.get('overall_status')}**",
        "",
        "| 门禁 | 状态 | 证据/说明 |",
        "| --- | --- | --- |",
    ]
    status_labels = {
        "passed": "通过",
        "failed": "失败",
        "conditional": "有条件",
        "not_run": "未运行",
        "not_available": "不可用",
    }
    for item in report.get("items", []):
        detail = str(item.get("detail") or item.get("evidence") or "").replace("\n", "；")
        lines.append(f"| {item['name']} | {status_labels.get(item['status'], item['status'])} | {detail} |")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 Jiadun 发布前 checklist")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--version", default=None)
    parser.add_argument("--output", type=Path, default=None, help="JSON 文件或输出目录；默认 dist/")
    parser.add_argument("--run-tests", action="store_true", help="运行 pytest、Ruff、迁移和恢复测试")
    parser.add_argument("--run-performance", action="store_true", help="运行 1万/5万/20万行性能基准")
    parser.add_argument("--performance-report", type=Path, help="读取已生成的性能 JSON，不重复运行")
    parser.add_argument("--keep-workspace", action="store_true", help="性能基准成功后保留现场")
    parser.add_argument("--skip-golden", action="store_true", help="不运行黄金回归（不建议用于发布）")
    parser.add_argument(
        "--allow-no-real",
        action="store_true",
        help="仅开发检查时允许 real_case_count=0；不会使生产发布门禁通过",
    )
    parser.add_argument("--json", action="store_true", help="同时把 JSON 报告打印到标准输出")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_checklist(
        args.root,
        version=args.version,
        run_tests=args.run_tests,
        run_performance=args.run_performance,
        performance_report=args.performance_report,
        keep_workspace=args.keep_workspace,
        run_golden=not args.skip_golden,
        allow_no_real=args.allow_no_real,
        output_dir=(args.output if args.output and args.output.suffix.lower() != ".json" else None),
    )
    output = args.output or args.root / "dist"
    json_path, markdown_path = write_checklist(report, output)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"JSON：{json_path}")
        print(f"Markdown：{markdown_path}")
        print(f"总体状态：{report['overall_status']}")
    return 0 if report["overall_status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
