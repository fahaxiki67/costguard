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
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PART = "local_private_data"
PERFORMANCE_SIZES = (10_000, 50_000, 200_000)
PERFORMANCE_STAGE_NAMES = (
    "合成数据生成",
    "Excel 合成导入",
    "清单分页打开",
    "清单搜索",
    "异常检测",
    "匹配计算",
    "对上/对下双向校核",
    "Excel 审核底稿导出",
)
PERFORMANCE_STAGE_STATUSES = frozenset(
    {"pending", "running", "completed", "failed", "skipped", "cancelled"}
)
PERFORMANCE_RESULT_STATUSES = frozenset({"completed", "failed", "cancelled"})

# 直接以 ``python scripts/release_checklist.py`` 运行时，Python 默认只把
# scripts/ 放进 sys.path；显式加入仓库根，保证脚本和 ``uv run``/pytest
# 入口使用同一套包解析规则。
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from jiadun.core.acceptance import canonical_bundle_hash  # noqa: E402
from jiadun.version import read_project_version  # noqa: E402


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _version(root: Path) -> str | None:
    return read_project_version(root)


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _performance_artifact_root(report_path: Path) -> Path | None:
    """从标准 runs/<run_id>/performance_benchmark.json 位置解析现场根目录。"""
    report_path = Path(report_path).resolve()
    run_dir = report_path.parent
    if run_dir.parent.name != "runs" or not run_dir.name:
        return None
    return run_dir.parent.parent / "work" / run_dir.name


def _validate_termination_payload(
    value: Any,
    *,
    expected_status: str,
    scope: str,
    expected_size: int | None = None,
) -> list[str]:
    """验证失败/取消终态的规模、阶段、原因和时间证据。"""

    if not isinstance(value, dict):
        return [f"{scope}缺少 termination 终止证据对象"]
    errors: list[str] = []
    if value.get("status") != expected_status:
        errors.append(
            f"{scope}termination.status 必须为 {expected_status!r}，实际={value.get('status')!r}"
        )
    for field in ("at", "stage", "reason"):
        raw = value.get(field)
        if not isinstance(raw, str) or not raw.strip():
            errors.append(f"{scope}termination.{field} 必须是非空字符串")
    raw_at = value.get("at")
    if isinstance(raw_at, str) and raw_at.strip():
        try:
            datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"{scope}termination.at 必须是 ISO-8601 时间")
    size = value.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        errors.append(f"{scope}termination.size 必须是正整数")
    elif expected_size is not None and size != expected_size:
        errors.append(
            f"{scope}termination.size 与规模不一致：expected={expected_size}, actual={size}"
        )
    return errors


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdefABCDEF" for char in value)
    )


def _validate_stage_metrics(stage: dict[str, Any], scope: str, errors: list[str]) -> None:
    """阶段证据必须包含可复核的墙钟耗时，不能以空字段冒充已执行。"""

    elapsed = stage.get("elapsed_seconds")
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, int | float)
        or not math.isfinite(float(elapsed))
        or elapsed < 0
    ):
        errors.append(f"{scope}.elapsed_seconds 必须是非负有限数字")


def _validate_direction_counts(
    item: dict[str, Any], size: int, scope: str, errors: list[str]
) -> None:
    total = item.get("total_detail_rows")
    if isinstance(total, bool) or not isinstance(total, int) or total != size:
        errors.append(f"{scope}.total_detail_rows 必须等于规模 {size}")
    rows = item.get("rows_per_direction")
    if not isinstance(rows, dict):
        errors.append(f"{scope}.rows_per_direction 必须是对象")
        return
    if set(rows) != {"upward", "downward"}:
        errors.append(f"{scope}.rows_per_direction 必须包含 upward/downward")
        return
    values: list[int] = []
    for direction in ("upward", "downward"):
        value = rows.get(direction)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            errors.append(f"{scope}.rows_per_direction.{direction} 必须是非负整数")
        else:
            values.append(value)
    if len(values) == 2 and sum(values) != size:
        errors.append(
            f"{scope}.rows_per_direction 合计与规模不一致：rows={sum(values)}, size={size}"
        )


def _validate_acceptance_bundle(
    report: dict[str, Any],
    *,
    expected_input_hashes: set[tuple[int, str]] | None = None,
    expected_output_hashes: set[tuple[int, str]] | None = None,
    scope: str = "报告 ",
) -> list[str]:
    """校验运行包完整性，避免报告自带的空壳字段被当作现场证据。"""

    bundle = report.get("acceptance_bundle")
    if not isinstance(bundle, dict):
        return [f"{scope}缺少 acceptance_bundle 运行包证据"]
    errors: list[str] = []
    for key in (
        "bundle_version",
        "run_id",
        "runtime",
        "configuration",
        "inputs",
        "outputs",
        "stages",
        "truth",
        "golden_vector",
        "integrity",
    ):
        if key not in bundle:
            errors.append(f"{scope}acceptance_bundle 缺少 {key}")
    if bundle.get("bundle_version") != 1:
        errors.append(f"{scope}acceptance_bundle.bundle_version 必须为 1")
    if not isinstance(bundle.get("run_id"), str) or not bundle.get("run_id", "").strip():
        errors.append(f"{scope}acceptance_bundle.run_id 必须是非空字符串")
    runtime = bundle.get("runtime")
    if not isinstance(runtime, dict):
        errors.append(f"{scope}acceptance_bundle.runtime 必须是对象")
    else:
        if runtime.get("product_id") != "jiadun":
            errors.append(f"{scope}acceptance_bundle.runtime.product_id 必须为 jiadun")
        if not isinstance(runtime.get("schema_version"), int):
            errors.append(f"{scope}acceptance_bundle.runtime.schema_version 无效")
    configuration = bundle.get("configuration")
    if not isinstance(configuration, dict):
        errors.append(f"{scope}acceptance_bundle.configuration 必须是对象")
    elif configuration.get("benchmark_config") != report.get("config"):
        errors.append(f"{scope}acceptance_bundle 未绑定同一份 benchmark config")
    for key in ("inputs", "outputs", "stages"):
        if not isinstance(bundle.get(key), list):
            errors.append(f"{scope}acceptance_bundle.{key} 必须是数组")
    integrity = bundle.get("integrity")
    if not isinstance(integrity, dict):
        errors.append(f"{scope}acceptance_bundle.integrity 必须是对象")
    else:
        digest = integrity.get("bundle_sha256")
        if not _valid_sha256(digest):
            errors.append(f"{scope}acceptance_bundle.integrity.bundle_sha256 无效")
        else:
            try:
                expected_digest = canonical_bundle_hash(bundle)
            except Exception as exc:  # noqa: BLE001 - 外部 JSON 必须 fail-closed
                errors.append(
                    f"{scope}acceptance_bundle 无法重算哈希：{type(exc).__name__}: {exc}"
                )
            else:
                if digest.lower() != expected_digest.lower():
                    errors.append(f"{scope}acceptance_bundle 哈希与内容不一致")
    golden_vector = bundle.get("golden_vector")
    if not isinstance(golden_vector, dict) or golden_vector.get("matches_expected") is not True:
        errors.append(f"{scope}acceptance_bundle.golden_vector 未通过确定性自检")

    def metadata_hashes(key: str) -> set[tuple[int, str]]:
        values: set[tuple[int, str]] = set()
        entries = bundle.get(key)
        if not isinstance(entries, list):
            return values
        for index, entry in enumerate(entries):
            entry_scope = f"{scope}acceptance_bundle.{key}[{index}]"
            if not isinstance(entry, dict):
                errors.append(f"{entry_scope} 必须是对象")
                continue
            size_bytes = entry.get("size_bytes")
            digest = entry.get("sha256")
            if (
                isinstance(size_bytes, bool)
                or not isinstance(size_bytes, int)
                or size_bytes <= 0
            ):
                errors.append(f"{entry_scope}.size_bytes 必须是正整数")
            if not _valid_sha256(digest):
                errors.append(f"{entry_scope}.sha256 无效")
            if isinstance(size_bytes, int) and size_bytes > 0 and _valid_sha256(digest):
                values.add((size_bytes, digest.lower()))
        return values

    actual_inputs = metadata_hashes("inputs")
    actual_outputs = metadata_hashes("outputs")
    for expected, label, actual in (
        (expected_input_hashes or set(), "输入", actual_inputs),
        (expected_output_hashes or set(), "导出", actual_outputs),
    ):
        for size_bytes, digest in expected:
            if (size_bytes, digest.lower()) not in actual:
                errors.append(
                    f"{scope}acceptance_bundle 缺少对应{label}文件哈希："
                    f"bytes={size_bytes}, sha256={digest}"
                )
    return errors


def _validate_noncompleted_performance_report(
    report: dict[str, Any],
    results: list[Any],
    config: dict[str, Any],
) -> list[str]:
    """验证取消/失败现场，避免空结果或缺步骤被当成可恢复证据。"""

    status = report.get("status")
    if status not in {"cancelled", "failed"}:
        return []
    errors = _validate_termination_payload(
        report.get("termination"),
        expected_status=status,
        scope="报告 ",
    )
    workspace = report.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        errors.append("非完成报告必须保留 workspace 现场路径")
    elif not Path(workspace).expanduser().is_dir():
        errors.append(f"非完成报告 workspace 现场不存在：{workspace!r}")
    errors.extend(_validate_acceptance_bundle(report))
    declared_sizes = config.get("sizes")
    allowed_sizes = {
        size
        for size in declared_sizes
        if isinstance(size, int) and not isinstance(size, bool)
    } if isinstance(declared_sizes, list) else set()
    termination = report.get("termination")
    if isinstance(termination, dict):
        term_size = termination.get("size")
        if isinstance(term_size, int) and not isinstance(term_size, bool):
            if term_size not in allowed_sizes:
                errors.append(f"报告 termination.size 不在 config.sizes 中：{term_size}")
            if not any(
                isinstance(item, dict)
                and item.get("size") == term_size
                and item.get("status") == status
                for item in results
            ):
                errors.append("报告 termination.size 未对应同一终态的规模结果")
    if not results:
        errors.append(f"{status} 报告必须保留至少一条规模结果")
        return errors

    for index, item in enumerate(results):
        scope = f"results[{index}] "
        if not isinstance(item, dict):
            errors.append(f"{scope}必须是对象")
            continue
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            errors.append(f"{scope}.size 必须是正整数")
            item_size: int | None = None
        else:
            item_size = size
            if size not in allowed_sizes:
                errors.append(f"{scope}.size 不在 config.sizes 中：{size}")
        if item_size is not None:
            _validate_direction_counts(item, item_size, scope, errors)
        if item.get("workspace_retained") is not True:
            errors.append(f"{scope}.workspace_retained 必须为 true")
        item_status = item.get("status")
        if item_status not in PERFORMANCE_RESULT_STATUSES:
            errors.append(
                f"{scope}.status 必须是 completed/failed/cancelled，实际={item_status!r}"
            )
        stages = item.get("stages")
        if not isinstance(stages, list) or not stages:
            errors.append(f"{scope}必须保留至少一个阶段步骤")
        else:
            names: list[str] = []
            for stage_index, stage in enumerate(stages):
                stage_scope = f"{scope}stages[{stage_index}]"
                if not isinstance(stage, dict):
                    errors.append(f"{stage_scope} 必须是对象")
                    continue
                name = stage.get("name")
                if not isinstance(name, str) or not name.strip():
                    errors.append(f"{stage_scope}.name 必须是非空字符串")
                else:
                    names.append(name)
                if stage.get("status") not in PERFORMANCE_STAGE_STATUSES:
                    errors.append(
                        f"{stage_scope}.status 无效：{stage.get('status')!r}"
                    )
                if not isinstance(stage.get("details"), dict):
                    errors.append(f"{stage_scope}.details 必须是对象")
                _validate_stage_metrics(stage, stage_scope, errors)
            duplicate_names = sorted({name for name in names if names.count(name) > 1})
            if duplicate_names:
                errors.append(f"{scope}阶段名称不得重复：{'、'.join(duplicate_names)}")
        if item_status in {"failed", "cancelled"}:
            errors.extend(
                _validate_termination_payload(
                    item.get("termination"),
                    expected_status=item_status,
                    scope=scope,
                    expected_size=item_size,
                )
            )
            termination = item.get("termination")
            if isinstance(termination, dict) and isinstance(stages, list):
                stage_names = {
                    stage.get("name")
                    for stage in stages
                    if isinstance(stage, dict) and isinstance(stage.get("name"), str)
                }
                term_stage = termination.get("stage")
                if isinstance(term_stage, str) and term_stage not in stage_names:
                    errors.append(f"{scope}termination.stage 未对应已记录阶段：{term_stage!r}")
    top_termination = report.get("termination")
    if isinstance(top_termination, dict):
        term_size = top_termination.get("size")
        term_stage = top_termination.get("stage")
        matching_items = [
            item
            for item in results
            if isinstance(item, dict)
            and item.get("size") == term_size
            and item.get("status") == status
        ]
        if matching_items and isinstance(term_stage, str):
            names = {
                stage.get("name")
                for stage in matching_items[0].get("stages", [])
                if isinstance(stage, dict) and isinstance(stage.get("name"), str)
            }
            if term_stage not in names:
                errors.append(f"报告 termination.stage 未对应终止规模的阶段：{term_stage!r}")
    return errors


def _validate_performance_report(
    report: dict[str, Any],
    *,
    artifact_root: Path | None = None,
) -> list[str]:
    """验证性能证据的最小完整结构，防止空集合 ``all([])`` 假通过。

    ``cancelled`` 是可恢复的现场状态，允许只有已经完成的前置规模记录；
    但即使在取消或失败状态，也必须保留终止规模、阶段、原因、时间、阶段数组
    和工作现场入口。只有顶层明确 ``completed`` 时，才要求
    1万/5万/20万三条唯一规模记录、全部业务阶段和 Excel 导出证据齐全。
    """
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["性能报告必须是 JSON 对象"]
    if report.get("schema_version") != 1:
        errors.append("schema_version 必须为 1")
    config = report.get("config")
    if not isinstance(config, dict):
        return ["config 必须是对象"]
    declared_sizes = config.get("sizes")
    if not isinstance(declared_sizes, list) or not declared_sizes:
        errors.append("config.sizes 必须是非空数组")
    else:
        normalized_sizes: list[int] = []
        for raw_size in declared_sizes:
            if isinstance(raw_size, bool) or not isinstance(raw_size, int):
                errors.append(f"config.sizes 含无效规模：{raw_size!r}")
                continue
            normalized_sizes.append(raw_size)
        if len(normalized_sizes) != len(set(normalized_sizes)):
            errors.append("config.sizes 不得重复")
        if set(normalized_sizes) != set(PERFORMANCE_SIZES):
            errors.append(
                f"config.sizes 必须严格等于 {list(PERFORMANCE_SIZES)}，实际={declared_sizes}"
            )
    if not isinstance(config.get("skip_export"), bool):
        errors.append("config.skip_export 必须为布尔值")

    results = report.get("results")
    if not isinstance(results, list):
        errors.append("results 必须是数组")
        return errors
    if report.get("status") != "completed":
        # 取消/失败现场由上层分类为 conditional/failed，但仍必须有规模、
        # 阶段和终止原因证据，不能把空壳 JSON 当成可恢复现场。
        errors.extend(
            _validate_noncompleted_performance_report(report, results, config)
        )
        return errors

    if len(results) != len(PERFORMANCE_SIZES):
        errors.append(
            f"completed 报告必须包含 {len(PERFORMANCE_SIZES)} 条规模结果，实际={len(results)}"
        )
    result_sizes: list[int] = []
    expected_input_hashes: set[tuple[int, str]] = set()
    expected_output_hashes: set[tuple[int, str]] = set()
    if report.get("benchmark") != "Jiadun synthetic performance benchmark":
        errors.append("completed 报告 benchmark 标识不正确")
    if not isinstance(report.get("benchmark_version"), str) or not report.get(
        "benchmark_version", ""
    ).strip():
        errors.append("completed 报告缺少 benchmark_version")
    current_version = _version(REPO_ROOT)
    if (
        current_version is not None
        and isinstance(report.get("benchmark_version"), str)
        and report.get("benchmark_version") != current_version
    ):
        errors.append(
            "completed 报告 benchmark_version 与当前项目版本不一致："
            f"expected={current_version}, actual={report.get('benchmark_version')}"
        )
    if not isinstance(report.get("environment"), dict):
        errors.append("completed 报告缺少 environment 运行环境证据")
    generated_at = report.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        errors.append("completed 报告缺少 generated_at")
    else:
        try:
            datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        except ValueError:
            errors.append("completed 报告 generated_at 必须是 ISO-8601 时间")
    workspace = report.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        errors.append("completed 报告必须保留 workspace 现场路径")
    elif not Path(workspace).expanduser().is_dir():
        errors.append(f"completed 报告 workspace 现场不存在：{workspace!r}")
    output_paths = report.get("output_paths")
    if not isinstance(output_paths, dict) or not all(
        isinstance(output_paths.get(key), str) and output_paths.get(key, "").strip()
        for key in ("json", "markdown")
    ):
        errors.append("completed 报告缺少 JSON/Markdown 输出路径证据")
    bundle = report.get("acceptance_bundle")
    if isinstance(bundle, dict):
        repository = bundle.get("repository")
        current_commit = _git_commit(REPO_ROOT)
        if not isinstance(repository, dict):
            errors.append("completed 报告 acceptance_bundle 缺少 repository 源码身份")
        elif current_commit is not None and repository.get("git_head") != current_commit:
            errors.append(
                "completed 报告 acceptance_bundle.git_head 与当前源码提交不一致："
                f"expected={current_commit}, actual={repository.get('git_head')}"
            )
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            errors.append(f"results[{index}] 必须是对象")
            continue
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int):
            errors.append(f"results[{index}].size 无效：{size!r}")
            continue
        result_sizes.append(size)
        if item.get("status") != "completed":
            errors.append(f"规模 {size} 状态不是 completed：{item.get('status')!r}")
        _validate_direction_counts(item, size, f"规模 {size}", errors)
        if item.get("workspace_retained") is not True:
            errors.append(f"规模 {size} workspace_retained 必须为 true")
        input_info = item.get("input")
        if not isinstance(input_info, dict):
            errors.append(f"规模 {size} 缺少 input 合成文件证据")
        else:
            if input_info.get("total_detail_rows") != size:
                errors.append(f"规模 {size} input.total_detail_rows 与规模不一致")
            if input_info.get("rows_per_direction") != item.get("rows_per_direction"):
                errors.append(f"规模 {size} input.rows_per_direction 与规模结果不一致")
            if not isinstance(input_info.get("files"), list) or not input_info.get("files"):
                errors.append(f"规模 {size} input.files 不能为空")
        stages = item.get("stages")
        if not isinstance(stages, list):
            errors.append(f"规模 {size} 缺少 stages 数组")
            continue
        stage_names: list[str] = []
        for stage_index, stage in enumerate(stages):
            if not isinstance(stage, dict):
                errors.append(f"规模 {size} stages[{stage_index}] 必须是对象")
                continue
            name = stage.get("name")
            if not isinstance(name, str) or not name.strip():
                errors.append(f"规模 {size} stages[{stage_index}].name 必须是非空字符串")
            else:
                stage_names.append(name)
            status = stage.get("status")
            if status not in PERFORMANCE_STAGE_STATUSES:
                errors.append(
                    f"规模 {size} stages[{stage_index}].status 无效：{status!r}"
                )
            if not isinstance(stage.get("details"), dict):
                errors.append(f"规模 {size} stages[{stage_index}].details 必须是对象")
            _validate_stage_metrics(stage, f"规模 {size} stages[{stage_index}]", errors)
            if stage.get("status") == "completed":
                elapsed = stage.get("elapsed_seconds")
                if isinstance(elapsed, int | float) and not isinstance(elapsed, bool) and elapsed <= 0:
                    errors.append(
                        f"规模 {size} stages[{stage_index}].elapsed_seconds 必须大于 0"
                    )
        duplicate_stage_names = sorted(
            {name for name in stage_names if stage_names.count(name) > 1}
        )
        if duplicate_stage_names:
            errors.append(
                f"规模 {size} 阶段名称不得重复：{'、'.join(duplicate_stage_names)}"
            )
        stage_by_name = {
            stage.get("name"): stage
            for stage in stages
            if isinstance(stage, dict) and isinstance(stage.get("name"), str)
        }
        missing_stages = [name for name in PERFORMANCE_STAGE_NAMES if name not in stage_by_name]
        if missing_stages:
            errors.append(f"规模 {size} 缺少阶段：{'、'.join(missing_stages)}")
        for stage_name in PERFORMANCE_STAGE_NAMES:
            stage = stage_by_name.get(stage_name)
            if stage is not None and stage.get("status") != "completed":
                errors.append(
                    f"规模 {size} 阶段 {stage_name} 状态不是 completed：{stage.get('status')!r}"
                )
        input_entries_by_direction: dict[str, dict[str, Any]] = {}
        if isinstance(input_info, dict) and isinstance(input_info.get("files"), list):
            for input_index, input_entry in enumerate(input_info["files"]):
                input_scope = f"规模 {size} 输入文件[{input_index}]"
                if not isinstance(input_entry, dict):
                    errors.append(f"{input_scope} 必须是对象")
                    continue
                direction = input_entry.get("direction")
                if direction not in {"upward", "downward"}:
                    errors.append(f"{input_scope}.direction 无效：{direction!r}")
                elif direction in input_entries_by_direction:
                    errors.append(f"规模 {size} 输入文件 direction 重复：{direction}")
                else:
                    input_entries_by_direction[direction] = input_entry
                file_name = input_entry.get("file")
                if (
                    not isinstance(file_name, str)
                    or not file_name.strip()
                    or Path(file_name).name != file_name
                ):
                    errors.append(f"{input_scope}.file 必须是安全的文件名")
                rows = input_entry.get("rows")
                if isinstance(rows, bool) or not isinstance(rows, int) or rows <= 0:
                    errors.append(f"{input_scope}.rows 必须是正整数")
                file_bytes = input_entry.get("bytes")
                if (
                    isinstance(file_bytes, bool)
                    or not isinstance(file_bytes, int)
                    or file_bytes <= 0
                ):
                    errors.append(f"{input_scope}.bytes 必须是正整数")
                if not _valid_sha256(input_entry.get("sha256")):
                    errors.append(f"{input_scope}.sha256 无效")
            if set(input_entries_by_direction) != {"upward", "downward"}:
                errors.append(f"规模 {size} 输入文件必须恰好覆盖 upward/downward")
        generation_stage = stage_by_name.get("合成数据生成")
        if generation_stage is not None:
            generation = generation_stage.get("details")
            if not isinstance(generation, dict):
                generation = {}
            if generation.get("total_detail_rows") != size:
                errors.append(f"规模 {size} 合成数据生成未证明 total_detail_rows")
            if generation.get("rows_per_direction") != item.get("rows_per_direction"):
                errors.append(f"规模 {size} 合成数据生成方向行数与规模结果不一致")
            files = generation.get("files")
            if not isinstance(files, list) or not files:
                errors.append(f"规模 {size} 合成数据生成缺少文件清单")
            else:
                generated_rows = 0
                generation_directions: set[str] = set()
                for file_index, file_info in enumerate(files):
                    file_scope = f"规模 {size} 合成文件[{file_index}]"
                    if not isinstance(file_info, dict):
                        errors.append(f"{file_scope} 必须是对象")
                        continue
                    rows = file_info.get("rows")
                    if isinstance(rows, bool) or not isinstance(rows, int) or rows <= 0:
                        errors.append(f"{file_scope}.rows 必须是正整数")
                    else:
                        generated_rows += rows
                    direction = file_info.get("direction")
                    if direction in generation_directions:
                        errors.append(f"规模 {size} 合成文件 direction 重复：{direction}")
                    elif direction not in {"upward", "downward"}:
                        errors.append(f"规模 {size} 合成文件 direction 无效：{direction!r}")
                    else:
                        generation_directions.add(direction)
                    file_bytes = file_info.get("bytes")
                    digest = file_info.get("sha256")
                    if (
                        isinstance(file_bytes, bool)
                        or not isinstance(file_bytes, int)
                        or file_bytes <= 0
                    ):
                        errors.append(f"{file_scope}.bytes 必须是正整数")
                    if not _valid_sha256(digest):
                        errors.append(f"{file_scope}.sha256 无效")
                    file_name = file_info.get("file")
                    if (
                        not isinstance(file_name, str)
                        or not file_name.strip()
                        or Path(file_name).name != file_name
                    ):
                        errors.append(f"{file_scope}.file 必须是安全的文件名")
                    input_entry = input_entries_by_direction.get(direction)
                    if input_entry is None:
                        errors.append(f"{file_scope} 未找到对应 input 文件")
                    else:
                        for field in ("file", "rows", "bytes", "sha256"):
                            if input_entry.get(field) != file_info.get(field):
                                errors.append(
                                    f"{file_scope}.{field} 与 input 证据不一致"
                                )
                    if isinstance(file_bytes, int) and not isinstance(file_bytes, bool) and file_bytes > 0 and _valid_sha256(digest):
                        expected_input_hashes.add((file_bytes, digest.lower()))
                    if isinstance(file_name, str) and Path(file_name).name == file_name:
                        if artifact_root is None:
                            errors.append(f"{file_scope} 无法确定输入文件现场")
                        else:
                            input_path = (
                                Path(artifact_root)
                                / f"size-{size}"
                                / "inputs"
                                / file_name
                            ).resolve()
                            allowed_inputs_root = (
                                Path(artifact_root) / f"size-{size}" / "inputs"
                            ).resolve()
                            try:
                                input_path.relative_to(allowed_inputs_root)
                            except ValueError:
                                errors.append(f"{file_scope} 输入路径越过本规模现场")
                            else:
                                if not input_path.is_file():
                                    errors.append(f"{file_scope} 输入文件不存在：{input_path}")
                                else:
                                    try:
                                        actual_bytes = input_path.stat().st_size
                                        actual_sha = _sha256_file(input_path)
                                    except OSError as exc:
                                        errors.append(
                                            f"{file_scope} 输入文件无法复核：{type(exc).__name__}: {exc}"
                                        )
                                    else:
                                        if actual_bytes != file_bytes:
                                            errors.append(
                                                f"{file_scope} 输入文件大小与报告不一致："
                                                f"report={file_bytes}, actual={actual_bytes}"
                                            )
                                        if actual_sha.lower() != str(digest).lower():
                                            errors.append(f"{file_scope} 输入文件 SHA-256 与报告不一致")
                if generation_directions != {"upward", "downward"}:
                    errors.append(f"规模 {size} 合成文件必须恰好覆盖 upward/downward")
                if generated_rows != size:
                    errors.append(
                        f"规模 {size} 合成文件明细行合计不一致：rows={generated_rows}"
                    )
        import_stage = stage_by_name.get("Excel 合成导入")
        if import_stage is not None:
            imported = import_stage.get("details")
            if not isinstance(imported, dict) or imported.get("imported_detail_rows") != size:
                errors.append(f"规模 {size} Excel 合成导入未证明 imported_detail_rows")
            reports = imported.get("reports") if isinstance(imported, dict) else None
            if not isinstance(reports, list) or not reports:
                errors.append(f"规模 {size} Excel 合成导入缺少方向报告")
            elif any(not isinstance(report_item, dict) or report_item.get("status") != "ok" for report_item in reports):
                errors.append(f"规模 {size} Excel 合成导入存在未完成方向")
            elif {
                report_item.get("direction") for report_item in reports
            } != {"upward", "downward"}:
                errors.append(f"规模 {size} Excel 合成导入方向证据不完整")
        page_stage = stage_by_name.get("清单分页打开")
        if page_stage is not None:
            page = page_stage.get("details")
            if not isinstance(page, dict) or page.get("total_rows") != size:
                errors.append(f"规模 {size} 清单分页未证明 total_rows")
            elif (
                isinstance(page.get("page_size"), bool)
                or not isinstance(page.get("page_size"), int)
                or page.get("page_size") <= 0
                or isinstance(page.get("returned_rows"), bool)
                or not isinstance(page.get("returned_rows"), int)
                or page.get("returned_rows") != min(page["page_size"], size)
            ):
                errors.append(f"规模 {size} 清单分页 page_size/returned_rows 无效")
        search_stage = stage_by_name.get("清单搜索")
        if search_stage is not None:
            search = search_stage.get("details")
            if not isinstance(search, dict) or not isinstance(search.get("search"), str):
                errors.append(f"规模 {size} 清单搜索缺少检索证据")
            elif (
                isinstance(search.get("total_rows"), bool)
                or not isinstance(search.get("total_rows"), int)
                or search.get("total_rows") < 0
                or isinstance(search.get("returned_rows"), bool)
                or not isinstance(search.get("returned_rows"), int)
                or search.get("returned_rows") < 0
                or search.get("total_rows") != search.get("returned_rows")
            ):
                errors.append(f"规模 {size} 清单搜索总数与返回数不一致")
        anomaly_stage = stage_by_name.get("异常检测")
        if anomaly_stage is not None:
            anomaly = anomaly_stage.get("details")
            if not isinstance(anomaly, dict) or (
                isinstance(anomaly.get("finding_count"), bool)
                or not isinstance(anomaly.get("finding_count"), int)
                or anomaly.get("finding_count") < 0
                or not isinstance(anomaly.get("by_severity"), dict)
            ):
                errors.append(f"规模 {size} 异常检测缺少 finding_count/by_severity 证据")
        matching_stage = stage_by_name.get("匹配计算")
        if matching_stage is not None:
            matching = matching_stage.get("details")
            if not isinstance(matching, dict) or (
                isinstance(matching.get("group_count"), bool)
                or not isinstance(matching.get("group_count"), int)
                or matching.get("group_count") < 0
            ):
                errors.append(f"规模 {size} 匹配计算缺少 group_count 证据")
        crosscheck_stage = stage_by_name.get("对上/对下双向校核")
        if crosscheck_stage is not None:
            crosscheck = crosscheck_stage.get("details")
            checks = crosscheck.get("checks") if isinstance(crosscheck, dict) else None
            if not isinstance(checks, list) or not checks:
                errors.append(f"规模 {size} 双向校核缺少 checks 证据")
            else:
                check_directions: set[str] = set()
                required_check_fields = {
                    "period_no",
                    "direction",
                    "verification_level",
                    "status",
                    "detail_rows",
                    "path_a_total",
                    "path_b_total",
                    "control_status",
                    "control_diff",
                    "range_unproven_sheets",
                }
                for check_index, check in enumerate(checks):
                    check_scope = f"规模 {size} 校核 checks[{check_index}]"
                    if not isinstance(check, dict):
                        errors.append(f"{check_scope} 必须是对象")
                        continue
                    missing_fields = sorted(required_check_fields - set(check))
                    if missing_fields:
                        errors.append(
                            f"{check_scope} 缺少字段：{'、'.join(missing_fields)}"
                        )
                    direction = check.get("direction")
                    if direction not in {"upward", "downward"}:
                        errors.append(f"{check_scope}.direction 无效：{direction!r}")
                    else:
                        check_directions.add(direction)
                    period_no = check.get("period_no")
                    if isinstance(period_no, bool) or not isinstance(period_no, int) or period_no <= 0:
                        errors.append(f"{check_scope}.period_no 必须是正整数")
                    detail_rows = check.get("detail_rows")
                    if isinstance(detail_rows, bool) or not isinstance(detail_rows, int) or detail_rows < 0:
                        errors.append(f"{check_scope}.detail_rows 必须是非负整数")
                    unproven = check.get("range_unproven_sheets")
                    if isinstance(unproven, bool) or not isinstance(unproven, int) or unproven < 0:
                        errors.append(f"{check_scope}.range_unproven_sheets 必须是非负整数")
                if check_directions != {"upward", "downward"}:
                    errors.append(f"规模 {size} 双向校核必须恰好覆盖 upward/downward")
        export_stage = stage_by_name.get("Excel 审核底稿导出")
        if export_stage is not None:
            details = export_stage.get("details")
            if not isinstance(details, dict) or not details.get("file"):
                errors.append(f"规模 {size} 缺少 Excel 导出文件证据")
            elif not str(details.get("file", "")).lower().endswith(".xlsx"):
                errors.append(f"规模 {size} 导出文件扩展名不是 .xlsx")
            elif (
                isinstance(details.get("bytes"), bool)
                or not isinstance(details.get("bytes"), int)
                or details.get("bytes", 0) <= 0
            ):
                errors.append(f"规模 {size} Excel 导出文件大小无效")
            sha = details.get("sha256") if isinstance(details, dict) else None
            if not _valid_sha256(sha):
                errors.append(f"规模 {size} Excel 导出 SHA-256 无效")
            artifact_path_value = details.get("path") if isinstance(details, dict) else None
            if not isinstance(artifact_path_value, str) or not artifact_path_value.strip():
                errors.append(f"规模 {size} 缺少 Excel 导出路径证据")
            elif artifact_root is None:
                errors.append(f"规模 {size} 无法确定 Excel 导出所属的性能现场")
            else:
                artifact_path = Path(artifact_path_value).expanduser().resolve()
                allowed_root = Path(artifact_root).expanduser().resolve()
                try:
                    relative_artifact = artifact_path.relative_to(allowed_root)
                except ValueError:
                    errors.append(
                        f"规模 {size} Excel 导出路径越过本次性能现场：{artifact_path_value!r}"
                    )
                else:
                    if f"size-{size}" not in relative_artifact.parts:
                        errors.append(
                            f"规模 {size} Excel 导出路径未绑定本规模现场：{artifact_path_value!r}"
                        )
                    if details.get("file") != artifact_path.name:
                        errors.append(f"规模 {size} 导出文件名与路径不一致")
                    if not artifact_path.is_file():
                        errors.append(f"规模 {size} Excel 导出文件不存在：{artifact_path_value!r}")
                    else:
                        try:
                            actual_bytes = artifact_path.stat().st_size
                            if actual_bytes != details.get("bytes"):
                                errors.append(
                                    f"规模 {size} Excel 导出文件大小与现场不一致："
                                    f"report={details.get('bytes')}, actual={actual_bytes}"
                                )
                            actual_sha = _sha256_file(artifact_path)
                            if isinstance(sha, str) and actual_sha.lower() != sha.lower():
                                errors.append(f"规模 {size} Excel 导出 SHA-256 与现场不一致")
                            if not zipfile.is_zipfile(artifact_path):
                                errors.append(f"规模 {size} Excel 导出文件不是有效 XLSX/ZIP")
                            else:
                                try:
                                    with zipfile.ZipFile(artifact_path) as archive:
                                        if archive.testzip() is not None:
                                            errors.append(f"规模 {size} Excel 导出 ZIP 校验失败")
                                        required_members = {"[Content_Types].xml", "xl/workbook.xml"}
                                        missing_members = sorted(required_members - set(archive.namelist()))
                                        if missing_members:
                                            errors.append(
                                                f"规模 {size} Excel 导出缺少 XLSX 成员：{'、'.join(missing_members)}"
                                            )
                                except (OSError, zipfile.BadZipFile) as exc:
                                    errors.append(
                                        f"规模 {size} Excel 导出无法作为 XLSX 读取：{type(exc).__name__}: {exc}"
                                    )
                                try:
                                    import openpyxl

                                    workbook = openpyxl.load_workbook(
                                        artifact_path,
                                        read_only=True,
                                        data_only=False,
                                    )
                                    if not workbook.sheetnames:
                                        errors.append(f"规模 {size} Excel 导出没有可读取的工作表")
                                    workbook.close()
                                except Exception as exc:  # noqa: BLE001 - 外部文件必须 fail-closed
                                    errors.append(
                                        f"规模 {size} Excel 导出无法由 openpyxl 打开："
                                        f"{type(exc).__name__}: {exc}"
                                    )
                            if isinstance(details.get("bytes"), int) and _valid_sha256(sha):
                                expected_output_hashes.add((details["bytes"], sha.lower()))
                        except OSError as exc:
                            errors.append(
                                f"规模 {size} Excel 导出文件无法复核：{type(exc).__name__}: {exc}"
                            )
    errors.extend(
        _validate_acceptance_bundle(
            report,
            expected_input_hashes=expected_input_hashes,
            expected_output_hashes=expected_output_hashes,
        )
    )
    if len(result_sizes) != len(set(result_sizes)):
        errors.append("results 的规模不得重复")
    if set(result_sizes) != set(PERFORMANCE_SIZES):
        errors.append(
            f"completed results 必须覆盖 {list(PERFORMANCE_SIZES)}，实际={result_sizes}"
        )
    if config.get("skip_export") is True:
        errors.append("completed 报告不得设置 skip_export=true")
    return errors


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
        validation_errors = _validate_performance_report(
            report, artifact_root=_performance_artifact_root(report_path)
        )
        config = report.get("config") if isinstance(report.get("config"), dict) else {}
        sizes = config.get("sizes", [])
        results = report.get("results") if isinstance(report.get("results"), list) else []
        result_statuses = [
            result.get("status") if isinstance(result, dict) else None for result in results
        ]
        if validation_errors:
            status = "failed" if report.get("status") == "completed" else "conditional"
            detail = (
                f"性能报告结构未满足发布证据要求：{'；'.join(validation_errors)}；"
                f"status={report.get('status')}, sizes={sizes}, results={result_statuses}"
            )
        elif report.get("status") == "completed":
            status = "passed"
            detail = f"已读取完整现场报告；sizes={sizes}，results={len(result_statuses)}"
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
            # 发布清单必须在现场仍存在时复核每个导出文件的真实字节数和
            # SHA-256；验证完成后再按原始 keep_workspace 语义清理精确目录。
            keep_workspace=True,
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
    if not isinstance(report, dict):
        return _item(
            "performance_1w_5w_20w",
            "1万/5万/20万行性能与取消机制",
            "failed",
            evidence=str(bench_dir),
            detail="性能基准返回值不是 JSON 对象。",
            command=command,
        )
    artifact_root: Path | None = None
    workspace_value = report.get("workspace") if isinstance(report, dict) else None
    if isinstance(workspace_value, str) and workspace_value.strip():
        artifact_root = Path(workspace_value)
    workspace_scope_error: str | None = None
    if artifact_root is not None:
        allowed_workspace_root = (bench_dir / "work").resolve()
        resolved_workspace = artifact_root.expanduser().resolve()
        if resolved_workspace.parent != allowed_workspace_root:
            workspace_scope_error = (
                "性能现场必须是本次输出目录 work/ 下的单次 run 子目录，"
                f"实际={resolved_workspace}"
            )
            artifact_root = None
    validation_errors = _validate_performance_report(report, artifact_root=artifact_root)
    if workspace_scope_error:
        validation_errors.append(workspace_scope_error)
    benchmark_status = report.get("status")
    # 取消是 P0-06 要求的受控终态：现场保留、可恢复，但不能把未完成的
    # 规模测试误报为失败或通过。只有明确的运行异常才是 failed。
    if validation_errors:
        status = "failed" if benchmark_status == "completed" else "conditional"
        detail = (
            f"性能报告结构或现场证据未满足发布要求：{'；'.join(validation_errors)}；"
            f"status={benchmark_status}, sizes={report.get('config', {}).get('sizes') if isinstance(report.get('config'), dict) else None}, "
            f"results={len(report.get('results', [])) if isinstance(report.get('results'), list) else None}"
        )
    else:
        status = (
            "passed"
            if benchmark_status == "completed"
            else "conditional"
            if benchmark_status == "cancelled"
            else "failed"
        )
        report_config = report.get("config") if isinstance(report.get("config"), dict) else {}
        report_results = report.get("results") if isinstance(report.get("results"), list) else []
        detail = (
            f"status={report.get('status')}, sizes={report_config.get('sizes')}, "
            f"results={len(report_results)}"
        )
    if status == "passed" and not keep_workspace:
        workspace = Path(workspace_value).expanduser().resolve() if isinstance(workspace_value, str) else None
        if workspace is not None:
            allowed_workspace_root = (bench_dir / "work").resolve()
            if workspace.parent != allowed_workspace_root:
                status = "failed"
                detail += "；性能现场路径不在本次输出目录的单次 run 子目录，拒绝清理/放行"
            else:
                try:
                    shutil.rmtree(workspace)
                except OSError as exc:
                    status = "failed"
                    detail += f"；性能现场清理失败：{type(exc).__name__}: {exc}"
    return _item(
        "performance_1w_5w_20w",
        "1万/5万/20万行性能与取消机制",
        status,
        evidence=str(report.get("output_paths", {}).get("json", bench_dir)),
        detail=detail,
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
            unavailable_count = int(golden.get("not_available_case_count") or 0)
            pending_only_unavailable = (
                pending_count > 0
                and pending_count == unavailable_count
                and real_case_count == 0
            )
            known_statuses = {"PASS", "FAIL", "PENDING", "INCOMPARABLE"}
            unknown_count = sum(
                count
                for status, count in comparison_counts.items()
                if status not in known_statuses
            )
            canonical_detail = ", ".join(
                f"{status}={count}" for status, count in sorted(comparison_counts.items())
            )
            canonical_error_detail = (
                f"canonical_errors={'；'.join(canonical_errors)}；"
                if canonical_errors
                else ""
            )
            if (
                golden.get("status") != "passed"
                or canonical_errors
                or comparison_counts.get("FAIL", 0)
                or comparison_counts.get("INCOMPARABLE", 0)
                or unknown_count
            ):
                golden_status = "failed"
                golden_gate_status = "failed"
            elif real_case_count == 0 and allow_no_real and (
                pending_count == 0 or pending_only_unavailable
            ):
                golden_status = "conditional"
                golden_gate_status = "development_override"
            elif real_case_count == 0:
                golden_status = "failed"
                golden_gate_status = "blocked"
            elif pending_count:
                golden_status = "failed"
                golden_gate_status = "blocked"
            else:
                golden_status = "passed"
                golden_gate_status = "passed"
            golden_note = (
                "生产发布门禁已阻断：请补齐脱敏真实案例。"
                if golden_gate_status == "blocked"
                else "黄金回归包含 PENDING/INCOMPARABLE 或未知状态，不能形成生产结论。"
                if golden_gate_status == "failed"
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
                        f"comparison_status_counts={canonical_detail}；"
                        f"matching_benchmark_status_counts={golden.get('matching_benchmark_status_counts') or {}}；"
                        f"{canonical_error_detail}"
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
