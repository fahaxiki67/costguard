"""Jiadun 大项目性能基准（仅使用合成数据）。

本脚本驱动现有的真实导入、分页/搜索、异常检测、匹配、双向校核和 Excel
导出链路，记录每个阶段的墙钟耗时、Python 峰值分配和进程 RSS 高水位，输出
机器可读 JSON 与中文 Markdown 报告。

安全边界
---------
* ``--sizes`` 表示一个项目的**总明细行数**；行数在对上、对下两个方向之间
  尽量均分，因此报告同时记录 ``total_detail_rows`` 与各方向行数。
* 所有输入工作簿、SQLite 项目和导出文件都写入本次运行的独立合成工作目录；
  默认输出目录是仓库外的系统临时目录，不读取 ``local_private_data``，也不
  接触用户工程资料或正式成果。
* 默认成功后清理可再生的合成输入/项目目录；需要复查现场时使用
  ``--keep-workspace``。失败或 Ctrl-C 时保留现场并写出当前已完成结果。
* ``--skip-export`` 只跳过 Excel 导出阶段，不改变前面的真实业务链路。
* 这是性能/容量基准，不是业务结论；合成数据不代表任何真实项目金额。

示例
----
    uv run python scripts/performance_benchmark.py --sizes 10000 50000 200000
    uv run python scripts/performance_benchmark.py --sizes 10000 --skip-export
    uv run python scripts/performance_benchmark.py --sizes 10000 --output /tmp/cg-bench \
        --keep-workspace

输出文件
--------
每次运行写入唯一的 ``runs/<run_id>/`` 目录：其中的
``performance_benchmark.json`` 保存完整结构化结果，
``performance_benchmark.md`` 保存适合人工查看的阶段表。报告会明确标记跳过、
失败和取消的阶段，不会把未执行阶段写成零耗时或通过；对应的 ``work/<run_id>/``
仅在需要复查或运行失败/取消时保留。
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import time
import tracemalloc
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# 允许直接 ``python scripts/performance_benchmark.py`` 运行源码检出版本。
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if SRC_ROOT.is_dir() and str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from jiadun.core.acceptance import build_acceptance_bundle  # noqa: E402
from jiadun.core.contracts import run_contract  # noqa: E402

DEFAULT_SIZES = (10_000, 50_000, 200_000)
MAX_SIZE = 1_000_000
PAGE_SIZE = 500
SYNTHETIC_SEED = 20260831
JSON_NAME = "performance_benchmark.json"
MARKDOWN_NAME = "performance_benchmark.md"


def default_output_dir() -> Path:
    """返回默认报告目录；不落在仓库、``local_private_data`` 或用户项目内。"""

    return Path(tempfile.gettempdir()) / "jiadun-performance-benchmark"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _emit(message: str, progress: Callable[[str], None] | None = None) -> None:
    """输出可见阶段进度，同时允许测试/调用方收集消息。"""

    if progress is not None:
        progress(message)
        return
    print(f"[价盾性能基准] {message}", flush=True)


def _rss_mb() -> float | None:
    """读取进程 RSS 高水位，兼容 macOS、Linux 与无 ``resource`` 的环境。"""

    try:
        import resource

        value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, AttributeError, OSError):
        return None
    # macOS 返回 bytes；Linux/大多数 Unix 返回 KiB。
    if sys.platform == "darwin":
        return round(value / (1024 * 1024), 3)
    return round(value / 1024, 3)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return value.name
    if isinstance(value, set | frozenset):
        return sorted(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_error(exc: BaseException) -> str:
    """错误只保留类型和短消息，避免把运行目录绝对路径塞入报告。"""

    text = str(exc).replace("\\", "/")
    # 输入/项目路径只属于临时现场，不属于性能报告；保守地替换常见绝对路径。
    if "/" in text:
        pieces = text.split("/")
        text = "…/" + "/".join(pieces[-2:])
    text = text.strip()
    if len(text) > 240:
        text = text[:237] + "..."
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _termination_payload(
    status: str,
    *,
    size: int | None,
    stage: str,
    reason: str,
    at: str | None = None,
) -> dict[str, Any]:
    """生成可追溯的失败/取消终止证据。"""

    payload: dict[str, Any] = {
        "status": status,
        "at": at or _now(),
        "stage": stage or "未记录阶段",
        "reason": reason or "未记录原因",
    }
    if size is not None:
        payload["size"] = size
    return payload


@dataclass
class StageResult:
    name: str
    status: str = "pending"  # completed | failed | skipped | cancelled
    elapsed_seconds: float | None = None
    rss_before_mb: float | None = None
    rss_after_mb: float | None = None
    rss_high_water_mb: float | None = None
    rss_delta_mb: float | None = None
    python_peak_mb: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "elapsed_seconds": self.elapsed_seconds,
            "rss_before_mb": self.rss_before_mb,
            "rss_after_mb": self.rss_after_mb,
            "rss_high_water_mb": self.rss_high_water_mb,
            "rss_delta_mb": self.rss_delta_mb,
            "python_peak_mb": self.python_peak_mb,
            "details": self.details,
            "error": self.error,
        }


class BenchmarkCancelledError(Exception):
    """调用方通过 cancel_check 请求取消当前基准。"""


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise BenchmarkCancelledError("已收到取消请求")


def _measure_stage(
    name: str,
    operation: Callable[[], dict[str, Any] | None],
    *,
    progress: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[StageResult, Any | None]:
    """运行一个阶段并记录指标；异常转为阶段失败，调用方可安全收束。"""

    _emit(f"开始：{name}", progress)
    gc.collect()
    before_rss = _rss_mb()
    if tracemalloc.is_tracing():
        tracemalloc.reset_peak()
    started = time.perf_counter()
    result = StageResult(name=name, rss_before_mb=before_rss)
    value: Any | None = None
    try:
        _check_cancel(cancel_check)
        value = operation()
    except BenchmarkCancelledError as exc:
        result.status = "cancelled"
        result.error = str(exc)
    except Exception as exc:  # 阶段级记录；不吞 Ctrl-C
        result.status = "failed"
        result.error = _safe_error(exc)
    else:
        result.status = "completed"
        if isinstance(value, dict):
            result.details = value
    finally:
        result.elapsed_seconds = round(time.perf_counter() - started, 6)
        result.rss_after_mb = _rss_mb()
        result.rss_high_water_mb = result.rss_after_mb
        if before_rss is not None and result.rss_after_mb is not None:
            result.rss_delta_mb = round(result.rss_after_mb - before_rss, 3)
        if tracemalloc.is_tracing():
            _current, peak = tracemalloc.get_traced_memory()
            result.python_peak_mb = round(peak / (1024 * 1024), 3)
    if result.status == "completed":
        _emit(f"完成：{name}（{result.elapsed_seconds:.3f}s）", progress)
    elif result.status == "cancelled":
        _emit(f"取消：{name}；已保留现场，可查看报告后收束", progress)
    else:
        _emit(f"失败：{name}；已保留现场，可查看报告后收束", progress)
    return result, value


def _parse_sizes(values: list[str] | tuple[int, ...] | None) -> list[int]:
    """解析 ``--sizes 10000 50000`` 与 ``--sizes 10000,50000`` 两种写法。"""

    if values is None:
        return list(DEFAULT_SIZES)
    tokens: list[str] = []
    for value in values:
        tokens.extend(part.strip() for part in str(value).split(","))
    sizes: list[int] = []
    for token in tokens:
        if not token:
            continue
        try:
            size = int(token)
        except ValueError as exc:
            raise ValueError(f"--sizes 包含无法识别的行数：{token!r}") from exc
        if size <= 0 or size > MAX_SIZE:
            raise ValueError(f"--sizes 必须在 1–{MAX_SIZE} 之间：{size}")
        if size not in sizes:
            sizes.append(size)
    if not sizes:
        raise ValueError("--sizes 至少需要一个正整数")
    return sizes


def _split_direction_rows(total: int) -> dict[str, int]:
    upward = total // 2
    downward = total - upward
    return {"upward": upward, "downward": downward}


def _workbook_path(work_dir: Path, direction: str, rows: int) -> Path:
    label = "对上结算" if direction == "upward" else "对下结算"
    return work_dir / f"benchmark-{label}-第1期-{rows}行.xlsx"


def _generate_workbook(
    path: Path,
    direction: str,
    n_rows: int,
    *,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """用 write-only openpyxl 生成一个可由真实导入器读取的合成工作簿。"""

    import openpyxl

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("第1期")
    ws.append(["清单编码", "清单名称", "项目特征", "计量单位", "工程量", "综合单价", "合价", "税率"])
    subtotal = Decimal("0")
    for index in range(1, n_rows + 1):
        if index == 1 or index % 1000 == 0:
            _check_cancel(cancel_check)
        # 固定种子和 Decimal 计算保证每次基准输入相同，且金额不依赖 float。
        quantity = (Decimal(index % 1000) + Decimal("1")) / Decimal("10")
        unit_price = Decimal("10.00") + (Decimal(index % 50) / Decimal("10"))
        amount = (quantity * unit_price).quantize(Decimal("0.01"))
        subtotal += amount
        ws.append([
            f"CG{index:09d}",
            f"合成清单项{index}",
            f"{direction}-benchmark-{SYNTHETIC_SEED}",
            "m3",
            quantity,
            unit_price,
            amount,
            Decimal("0.09"),
        ])
    # 唯一合计级控制值，便于 C 路径正常参与；不添加本页小计以免重复控制。
    ws.append([None, "合计", None, None, None, None, subtotal, None])
    wb.properties.creator = "Jiadun（价盾） synthetic performance benchmark"
    wb.save(path)
    return {
        "direction": direction,
        "rows": n_rows,
        "subtotal": str(subtotal),
        "file": path.name,
        "bytes": path.stat().st_size,
        "sha256": _file_sha256(path),
    }


def _generate_inputs(
    work_dir: Path,
    total_rows: int,
    *,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    direction_rows = _split_direction_rows(total_rows)
    files = []
    for direction, n_rows in direction_rows.items():
        files.append(_generate_workbook(
            _workbook_path(work_dir, direction, n_rows), direction, n_rows,
            cancel_check=cancel_check,
        ))
    return {
        "total_detail_rows": total_rows,
        "rows_per_direction": direction_rows,
        "files": files,
    }


def _open_project(work_dir: Path, total_rows: int):
    """创建隔离项目，并将项目模型 settings 重定向到本次合成工作目录。"""

    from jiadun.core.models import project as project_model

    # project_model 会持久登记工作空间；基准运行不应触碰用户设置文件。
    project_model._SETTINGS_FILE = work_dir / "settings.json"
    info = project_model.create_project(f"性能基准-{total_rows}行", work_dir / "projects")
    reopened, conn = project_model.open_project(Path(info.workspace_path))
    return reopened, conn


def _query_items(
    conn,
    project_id: int,
    *,
    page: int = 0,
    page_size: int = PAGE_SIZE,
    search: str = "",
) -> tuple[int, list[Any]]:
    """分页查询等价于工作台明细页的“总数 + 当前页”。"""

    where = ["sp.project_id=?", "json_extract(li.flags_json, '$.subtotal') IS NOT 1"]
    params: list[Any] = [project_id]
    if search:
        where.append("(li.code LIKE ? OR li.name LIKE ? OR li.unit LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    predicate = " AND ".join(where)
    total = conn.execute(
        f"""SELECT COUNT(*) AS n FROM line_items li
            JOIN settlement_periods sp ON sp.id=li.period_id
            WHERE {predicate}""",
        params,
    ).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT li.id, li.code, li.name, li.unit, li.quantity, li.unit_price, li.amount,
                   sp.period_no, sp.direction
            FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
            WHERE {predicate}
            ORDER BY li.id LIMIT ? OFFSET ?""",
        [*params, page_size, page * page_size],
    ).fetchall()
    return int(total), rows


def _json_amount(value: Any) -> str | None:
    return None if value is None else str(value)


def _crosscheck_details(results: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "period_no": result.period_no,
            "direction": result.direction,
            "verification_level": result.verification_level,
            "status": result.status,
            "detail_rows": result.detail_rows,
            "path_a_total": _json_amount(result.path_a_total),
            "path_b_total": _json_amount(result.path_b_total),
            "control_status": result.control_status,
            "control_diff": _json_amount(result.control_diff),
            "range_unproven_sheets": result.range_unproven_sheets,
        }
        for result in results
    ]


def _mark_skipped(name: str, reason: str) -> StageResult:
    return StageResult(name=name, status="skipped", details={"reason": reason})


def _mark_record_termination(
    record: dict[str, Any],
    status: str,
    *,
    stage: str,
    reason: str,
) -> None:
    """把单个规模的非完成终态和原因一起写入记录。"""

    at = _now()
    record["status"] = status
    record["termination"] = _termination_payload(
        status,
        size=record.get("size"),
        stage=stage,
        reason=reason,
        at=at,
    )
    if status == "cancelled":
        record["cancelled_at"] = at
    elif status == "failed":
        record["failed_at"] = at


def _run_size(
    total_rows: int,
    run_work_dir: Path,
    *,
    skip_export: bool,
    progress: Callable[[str], None] | None,
    cancel_check: Callable[[], bool] | None,
) -> dict[str, Any]:
    """运行单个规模；某阶段失败后停止该规模，保留前序结果和现场。"""

    size_work = run_work_dir / f"size-{total_rows}"
    input_dir = size_work / "inputs"
    size_work.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "size": total_rows,
        "status": "running",
        "total_detail_rows": total_rows,
        "rows_per_direction": _split_direction_rows(total_rows),
        "stages": [],
        "workspace_retained": True,
    }
    conn = None
    old_settings_file = None
    try:
        # ---- 合成输入 ----
        stage, generation = _measure_stage(
            "合成数据生成",
            lambda: _generate_inputs(input_dir, total_rows, cancel_check=cancel_check),
            progress=progress,
            cancel_check=cancel_check,
        )
        record["stages"].append(stage.as_dict())
        if stage.status != "completed":
            _mark_record_termination(
                record,
                stage.status,
                stage=stage.name,
                reason=stage.error or f"阶段 {stage.name} 未完成",
            )
            return record
        record["input"] = generation

        # ---- 隔离项目 + 导入 ----
        from jiadun.core.models import project as project_model

        old_settings_file = project_model._SETTINGS_FILE

        def import_operation() -> dict[str, Any]:
            nonlocal conn
            from jiadun.core.engine import settlement_io

            info, conn = _open_project(size_work, total_rows)
            reports = []
            for direction, n_rows in _split_direction_rows(total_rows).items():
                _check_cancel(cancel_check)
                source = _workbook_path(input_dir, direction, n_rows)
                report = settlement_io.import_settlement_file(
                    conn,
                    info.project_id,
                    Path(info.workspace_path),
                    source,
                    period_no=1,
                    direction=direction,
                )
                reports.append({
                    "direction": direction,
                    "status": report.status,
                    "period_no": report.period_no,
                    "period_id": report.period_id,
                    "sheets": len(report.sheets),
                    "parsed_items": sum(s.n_items for s in report.sheets),
                })
                if report.status != "ok":
                    raise RuntimeError(f"{direction} 导入未完成：{report.status}")
            imported = conn.execute(
                "SELECT COUNT(*) AS n FROM line_items WHERE json_extract(flags_json, '$.subtotal') IS NOT 1"
            ).fetchone()["n"]
            return {
                "project_id": info.project_id,
                "workspace_name": Path(info.workspace_path).name,
                "reports": reports,
                "imported_detail_rows": int(imported),
            }

        stage, imported = _measure_stage(
            "Excel 合成导入", import_operation, progress=progress, cancel_check=cancel_check
        )
        record["stages"].append(stage.as_dict())
        if stage.status != "completed":
            _mark_record_termination(
                record,
                stage.status,
                stage=stage.name,
                reason=stage.error or f"阶段 {stage.name} 未完成",
            )
            return record
        project_id = int(imported["project_id"])
        record["project_id"] = project_id

        # ---- 明细页分页 / 搜索 ----
        def page_operation() -> dict[str, Any]:
            total, rows = _query_items(conn, project_id, page=0, page_size=PAGE_SIZE)
            if total != total_rows or len(rows) != min(PAGE_SIZE, total_rows):
                raise AssertionError(f"分页计数不一致：total={total}, page={len(rows)}")
            return {
                "total_rows": total,
                "page_size": PAGE_SIZE,
                "returned_rows": len(rows),
                "range": [1, len(rows)],
            }

        stage, _ = _measure_stage(
            "清单分页打开", page_operation, progress=progress, cancel_check=cancel_check
        )
        record["stages"].append(stage.as_dict())
        if stage.status != "completed":
            _mark_record_termination(
                record,
                stage.status,
                stage=stage.name,
                reason=stage.error or f"阶段 {stage.name} 未完成",
            )
            return record

        def search_operation() -> dict[str, Any]:
            # 该编码同时存在于两个方向，检索结果应为 2 条（总规模至少 2）。
            search_term = "CG000000001"
            total, rows = _query_items(conn, project_id, page=0, page_size=PAGE_SIZE, search=search_term)
            if total != 2 or len(rows) != 2:
                raise AssertionError(f"搜索结果不一致：total={total}, page={len(rows)}")
            return {
                "search": search_term,
                "total_rows": total,
                "returned_rows": len(rows),
                "page_size": PAGE_SIZE,
            }

        stage, _ = _measure_stage(
            "清单搜索", search_operation, progress=progress, cancel_check=cancel_check
        )
        record["stages"].append(stage.as_dict())
        if stage.status != "completed":
            _mark_record_termination(
                record,
                stage.status,
                stage=stage.name,
                reason=stage.error or f"阶段 {stage.name} 未完成",
            )
            return record

        # ---- 异常检测 ----
        def anomaly_operation() -> dict[str, Any]:
            from jiadun.core.anomalies import engine as anomaly_engine

            findings = anomaly_engine.run_anomalies(conn, project_id)
            by_severity = Counter(f.severity for f in findings)
            return {
                "finding_count": len(findings),
                "by_severity": dict(sorted(by_severity.items())),
            }

        stage, _ = _measure_stage(
            "异常检测", anomaly_operation, progress=progress, cancel_check=cancel_check
        )
        record["stages"].append(stage.as_dict())
        if stage.status != "completed":
            _mark_record_termination(
                record,
                stage.status,
                stage=stage.name,
                reason=stage.error or f"阶段 {stage.name} 未完成",
            )
            return record

        # ---- 匹配 ----
        def matching_operation() -> dict[str, Any]:
            from jiadun.core.matching import matching

            groups = matching.match_items(conn, project_id)
            by_level = Counter(group.level for group in groups)
            return {
                "group_count": len(groups),
                "by_level": dict(sorted(by_level.items())),
                "persisted": False,
                "persist_note": "基准只测匹配计算，避免把大批待复核候选写成业务确认",
            }

        stage, _ = _measure_stage(
            "匹配计算", matching_operation, progress=progress, cancel_check=cancel_check
        )
        record["stages"].append(stage.as_dict())
        if stage.status != "completed":
            _mark_record_termination(
                record,
                stage.status,
                stage=stage.name,
                reason=stage.error or f"阶段 {stage.name} 未完成",
            )
            return record

        # ---- 聚合 + A/B/C 双向校核 ----
        def crosscheck_operation() -> dict[str, Any]:
            from jiadun.core.engine import aggregate, crosscheck

            # persist_period_totals 会使对应期次的旧校核结果失效。先完成两个方向
            # 的聚合/持久化，再运行 A/B/C，避免第二个方向的持久化清掉第一个方向
            # 刚刚写入的校核结果。
            aggregates_by_direction = {}
            for direction in ("upward", "downward"):
                aggregates_by_direction[direction] = aggregate.aggregate_project(
                    conn, project_id, direction=direction
                )
            for direction in ("upward", "downward"):
                aggregate.persist_period_totals(
                    conn, project_id, aggregates_by_direction[direction]
                )
            all_results = []
            for direction in ("upward", "downward"):
                all_results.extend(
                    crosscheck.run_crosscheck(conn, project_id, [1], direction=direction)
                )
            return {
                "checks": _crosscheck_details(all_results),
                "verification_levels": dict(
                    Counter(result.verification_level for result in all_results)
                ),
                "run_contract_signature": run_contract.current_run_signature(
                    conn, project_id
                ),
            }

        stage, _ = _measure_stage(
            "对上/对下双向校核", crosscheck_operation, progress=progress,
            cancel_check=cancel_check,
        )
        record["stages"].append(stage.as_dict())
        if stage.status != "completed":
            _mark_record_termination(
                record,
                stage.status,
                stage=stage.name,
                reason=stage.error or f"阶段 {stage.name} 未完成",
            )
            return record

        # ---- Excel 导出 ----
        if skip_export:
            record["stages"].append(
                _mark_skipped("Excel 审核底稿导出", "命令行指定 --skip-export").as_dict()
            )
        else:
            def export_operation() -> dict[str, Any]:
                from jiadun.core.export import excel_export

                export_dir = Path(conn.execute(
                    "SELECT workspace_path FROM projects WHERE id=?", (project_id,)
                ).fetchone()["workspace_path"]) / "exports"
                export_dir.mkdir(parents=True, exist_ok=True)
                path = excel_export.export_workbook(conn, project_id, export_dir)
                return {
                    "file": Path(path).name,
                    "path": str(path),
                    "bytes": Path(path).stat().st_size,
                    "sha256": _file_sha256(Path(path)),
                }

            stage, _ = _measure_stage(
                "Excel 审核底稿导出", export_operation, progress=progress,
                cancel_check=cancel_check,
            )
            record["stages"].append(stage.as_dict())
            if stage.status != "completed":
                _mark_record_termination(
                    record,
                    stage.status,
                    stage=stage.name,
                    reason=stage.error or f"阶段 {stage.name} 未完成",
                )
                return record

        record["run_contract_signature"] = run_contract.current_run_signature(
            conn, project_id
        )
        record["status"] = "completed"
        return record
    except KeyboardInterrupt:
        # KeyboardInterrupt 不会被阶段级 ``except Exception`` 捕获；补写一个
        # 明确的“规模执行”终止阶段，避免 termination.stage 指向不存在的步骤。
        if not any(
            isinstance(stage, dict) and stage.get("name") == "规模执行"
            for stage in record.get("stages", [])
        ):
            record["stages"].append(
                StageResult(
                    name="规模执行",
                    status="cancelled",
                    elapsed_seconds=0.0,
                    details={"reason": "KeyboardInterrupt", "scope": "规模级阶段外中断"},
                    error="KeyboardInterrupt",
                ).as_dict()
            )
        _mark_record_termination(
            record,
            "cancelled",
            stage="规模执行",
            reason="KeyboardInterrupt",
        )
        return record
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if old_settings_file is not None:
            try:
                from jiadun.core.models import project as project_model

                project_model._SETTINGS_FILE = old_settings_file
            except Exception:
                pass


def _summary_status(status: str) -> str:
    return {
        "completed": "已完成",
        "failed": "失败，已保留现场",
        "cancelled": "已取消，已保留现场",
        "running": "进行中",
    }.get(status, status)


def _stage_status(status: str) -> str:
    return {
        "completed": "完成",
        "failed": "失败",
        "skipped": "跳过",
        "cancelled": "取消",
        "pending": "未执行",
    }.get(status, status)


def _write_reports(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / JSON_NAME
    markdown_path = output_dir / MARKDOWN_NAME
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# 价盾（Jiadun）大项目性能基准",
        "",
        f"- 生成时间：{report.get('generated_at', '未记录')}",
        f"- 基准状态：{_summary_status(report.get('status', '未记录'))}",
        f"- 测试环境：{report.get('environment', {}).get('system', '未记录')} "
        f"{report.get('environment', {}).get('machine', '未记录')}，"
        f"Python {report.get('environment', {}).get('python', '未记录')}",
        "- 行数口径：每个规模是项目总明细行数，对上/对下各方向尽量均分；小计/合计行不计入明细行数。",
        f"- 导出设置：{'跳过 Excel 导出' if report.get('config', {}).get('skip_export') else '包含 Excel 审核底稿导出'}",
        "- 数据安全：仅使用本次运行生成的合成工作簿；不读取 `local_private_data/`，不修改原始资料或正式成果。",
        "",
        "## 规模结果",
        "",
        "| 规模（总明细行） | 对上 | 对下 | 总体状态 | 工作目录 |",
        "|---:|---:|---:|---|---|",
    ]
    for item in report.get("results", []):
        rows = item.get("rows_per_direction", {})
        retained = "保留" if item.get("workspace_retained") else "已清理"
        lines.append(
            f"| {item.get('size', '未记录'):,} | {rows.get('upward', '未记录'):,} "
            f"| {rows.get('downward', '未记录'):,} | {_summary_status(item.get('status', '未记录'))} | {retained} |"
        )
    lines.extend(["", "## 阶段指标", ""])
    for item in report.get("results", []):
        lines.extend([
            f"### {item.get('size', '未记录'):,} 行项目",
            "",
            "| 阶段 | 状态 | 耗时（秒） | Python 峰值（MB） | RSS 高水位（MB） | RSS 增量（MB） | 关键结果 |",
            "|---|---|---:|---:|---:|---:|---|",
        ])
        for stage in item.get("stages", []):
            details = stage.get("details") or {}
            key = ""
            if "total_rows" in details:
                key = f"总数 {details['total_rows']}，当前页 {details.get('returned_rows', '未记录')}"
            elif "finding_count" in details:
                key = f"异常 {details['finding_count']} 项"
            elif "group_count" in details:
                key = f"匹配组 {details['group_count']} 组"
            elif "checks" in details:
                levels = details.get("verification_levels", {})
                key = f"双向校核 {len(details['checks'])} 期，级别 {levels}"
            elif "file" in details:
                key = f"{details['file']}（{details.get('bytes', '未记录')} B）"
            elif "reason" in details:
                key = details["reason"]
            lines.append(
                f"| {stage.get('name', '未记录')} | {_stage_status(stage.get('status', '未记录'))} "
                f"| {stage.get('elapsed_seconds', '—')} | {stage.get('python_peak_mb', '—')} "
                f"| {stage.get('rss_high_water_mb', '—')} | {stage.get('rss_delta_mb', '—')} | {key} |"
            )
        lines.append("")
        failures = [s for s in item.get("stages", []) if s.get("status") == "failed"]
        if failures:
            lines.append("阶段失败信息：")
            lines.extend(f"- {s.get('name')}：{s.get('error', '未记录')}" for s in failures)
            lines.append("")
    lines.extend([
        "## 解释与限制",
        "",
        "- 耗时包含当前阶段调用的真实价盾管线；Python 峰值由 `tracemalloc` 记录，RSS 是进程级高水位，二者口径不同。",
        "- “匹配计算”只测候选分组，不把大批待复核候选写入 `matches` 表，也不把候选解释为人工确认。",
        "- 双向校核报告保留 A/B/C 路径和校核级别；合成资料的结果只用于容量/耗时观察，不构成任何业务结论。",
        "- 本报告不能替代 WPS、macOS Excel、Windows Excel 真机打开/重算/保存/重开验证，也不能替代真实案例回归。",
    ])
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def run_benchmark(
    sizes: list[int] | tuple[int, ...] | None = None,
    *,
    output: Path | str | None = None,
    skip_export: bool = False,
    keep_workspace: bool = False,
    progress: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """运行基准并返回 JSON 对象；支持阶段边界和生成循环取消。"""

    parsed_sizes = _parse_sizes(sizes)
    output_dir = Path(output) if output is not None else default_output_dir()
    output_dir = output_dir.expanduser().resolve()
    private_marker = f"{os.sep}local_private_data{os.sep}"
    if private_marker in f"{output_dir}{os.sep}":
        raise ValueError("性能基准输出不能写入 local_private_data；请指定独立的输出目录")
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    run_work_dir = output_dir / "work" / run_id
    report_dir = output_dir / "runs" / run_id
    run_work_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": 1,
        "benchmark": "Jiadun synthetic performance benchmark",
        "benchmark_version": run_contract._app_version(),
        "generated_at": _now(),
        "status": "running",
        "environment": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "config": {
            "sizes": parsed_sizes,
            "skip_export": skip_export,
            "keep_workspace": keep_workspace,
            "page_size": PAGE_SIZE,
            "synthetic_seed": SYNTHETIC_SEED,
            "size_definition": "项目总明细行数，对上/对下方向尽量均分；不含小计/合计行",
        },
        "results": [],
        "output_files": {"json": JSON_NAME, "markdown": MARKDOWN_NAME},
    }
    tracemalloc.start()
    active_size: int | None = None
    try:
        for index, size in enumerate(parsed_sizes, start=1):
            active_size = size
            _emit(f"规模 {index}/{len(parsed_sizes)}：{size:,} 行项目", progress)
            result = _run_size(
                size,
                run_work_dir,
                skip_export=skip_export,
                progress=progress,
                cancel_check=cancel_check,
            )
            report["results"].append(result)
            # 先写入该规模的终态，再落盘。否则取消发生在规模收束后，
            # 旧报告会短暂保留 ``running``，让恢复工具无法区分已取消现场。
            if result.get("status") != "completed":
                report["status"] = result.get("status", "failed")
                termination = result.get("termination")
                if isinstance(termination, dict):
                    report["termination"] = {
                        **termination,
                        "size": result.get("size"),
                    }
                    if report["status"] == "cancelled":
                        report["cancelled_at"] = termination.get("at", _now())
                        report["cancelled_size"] = result.get("size")
            # 每个规模完成即落盘，Ctrl-C 或进程异常时至少保留已完成规模。
            report["generated_at"] = _now()
            _write_reports(report, report_dir)
            if result.get("status") != "completed":
                break
            active_size = None
        else:
            report["status"] = "completed"
    except KeyboardInterrupt:
        report["status"] = "cancelled"
        cancelled_at = _now()
        report["cancelled_at"] = cancelled_at
        if active_size is not None:
            # 中断可能发生在 _run_size 尚未返回时（例如解析器逐格落库中）。
            # 记录规模和现场入口，避免报告只显示前一规模而掩盖半成品现场。
            report["cancelled_size"] = active_size
            report["termination"] = _termination_payload(
                "cancelled",
                size=active_size,
                stage="规模执行",
                reason="KeyboardInterrupt",
                at=cancelled_at,
            )
            if not any(item.get("size") == active_size for item in report["results"]):
                report["results"].append(
                    {
                        "size": active_size,
                        "status": "cancelled",
                        "total_detail_rows": active_size,
                        "rows_per_direction": _split_direction_rows(active_size),
                        "stages": [
                            StageResult(
                                name="规模执行",
                                status="cancelled",
                                elapsed_seconds=0.0,
                                details={"reason": "KeyboardInterrupt"},
                                error="KeyboardInterrupt",
                            ).as_dict()
                        ],
                        "workspace_retained": True,
                        "termination": report["termination"],
                        "cancelled_at": cancelled_at,
                    }
                )
    except Exception as exc:
        report["status"] = "failed"
        report["fatal_error"] = _safe_error(exc)
        failed_at = _now()
        report["termination"] = _termination_payload(
            "failed",
            size=active_size,
            stage="基准运行",
            reason=report["fatal_error"],
            at=failed_at,
        )
        if active_size is not None and not any(
            item.get("size") == active_size for item in report["results"]
        ):
            report["results"].append(
                {
                    "size": active_size,
                    "status": "failed",
                    "total_detail_rows": active_size,
                    "rows_per_direction": _split_direction_rows(active_size),
                    "stages": [
                        StageResult(
                            name="基准运行",
                            status="failed",
                            elapsed_seconds=0.0,
                            details={"reason": report["fatal_error"]},
                            error=report["fatal_error"],
                        ).as_dict()
                    ],
                    "workspace_retained": True,
                    "termination": report["termination"],
                    "failed_at": failed_at,
                }
            )
    finally:
        if tracemalloc.is_tracing():
            # Ctrl-C 可能在阶段返回后才被解释；清理阶段不得再次把已捕获
            # 的取消升级为未写报告的 traceback。停用失败不影响现场报告。
            try:
                tracemalloc.stop()
            except BaseException as exc:  # noqa: BLE001 - 终态报告优先于清理异常
                report["finalization_warning"] = _safe_error(exc)
                report["status"] = "cancelled"
                report.setdefault("cancelled_at", _now())
        report["generated_at"] = _now()
        # 先在现场仍存在时计算输入/输出哈希，再按成功/保留设置清理精确目录。
        flattened_stages = []
        output_paths: list[Path] = []
        input_paths: list[Path] = []
        signatures: dict[str, str | None] = {}
        for result in report["results"]:
            size = result.get("size")
            signatures[str(size)] = result.get("run_contract_signature")
            for stage in result.get("stages", []):
                flattened_stages.append({"size": size, **stage})
                details = stage.get("details") or {}
                artifact = details.get("path")
                if artifact:
                    output_paths.append(Path(artifact))
            for item in (result.get("input") or {}).get("files", []):
                name = item.get("file")
                if name:
                    input_paths.append(run_work_dir / f"size-{size}" / "inputs" / name)
        report["output_paths"] = {
            "json": str(report_dir / JSON_NAME),
            "markdown": str(report_dir / MARKDOWN_NAME),
        }
        report["acceptance_bundle"] = build_acceptance_bundle(
            run_id=run_id,
            repo_root=REPO_ROOT,
            input_paths=input_paths,
            output_paths=output_paths,
            stages=flattened_stages,
            run_contract_signature=signatures,
            config=report["config"],
        )
        # 失败/取消时保留现场；成功时只在用户未要求保留时清理精确目录。
        if report.get("status") == "completed" and not keep_workspace:
            try:
                shutil.rmtree(run_work_dir)
            except OSError as exc:
                report["workspace_cleanup_error"] = _safe_error(exc)
            else:
                for result in report["results"]:
                    result["workspace_retained"] = False
        else:
            report["workspace"] = str(run_work_dir)
        _write_reports(report, report_dir)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用合成数据测量价盾 1万/5万/20万行导入、分页搜索、异常、匹配、校核和 Excel 导出性能。"
    )
    parser.add_argument(
        "--sizes",
        nargs="+",
        default=None,
        metavar="N",
        help="项目总明细行数，可空格或逗号分隔；默认：10000 50000 200000",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"JSON/Markdown 报告目录；默认：{default_output_dir()}",
    )
    parser.add_argument(
        "--skip-export",
        action="store_true",
        help="跳过 Excel 审核底稿导出，仅测导入、分页/搜索、异常、匹配和双向校核",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="成功后保留本次合成输入、SQLite 项目和导出现场，便于复查；默认成功后清理",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        sizes = _parse_sizes(args.sizes)
        report = run_benchmark(
            sizes,
            output=args.output,
            skip_export=args.skip_export,
            keep_workspace=args.keep_workspace,
        )
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    output_paths = report.get("output_paths", {})
    print(f"JSON：{output_paths.get('json', '未生成')}")
    print(f"Markdown：{output_paths.get('markdown', '未生成')}")
    return 0 if report.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
