"""Excel 审核底稿导出阶段内 profiling（宪章 §七：先 profiling 再优化）。

对 ``export_workbook`` 做阶段级计时与 cProfile 热点分析，输出 JSON 与
中文 Markdown 报告。阶段口径（与宪章要求一一对应）：

- ``build_model``            报告模型构建（DB 查询 + 对象构建）
- ``sheet:<名称>``           各成果页写入（DB 查询 + worksheet population）
- ``autowidth``              列宽自适应（各写入器内部累计）
- ``style``                  有效区域统一样式
- ``data_sheet_prep``        冻结/筛选/打印表头
- ``evidence_links``         Evidence 工作簿内超链接
- ``save``                   序列化落盘（含受控产物登记）

同时记录 tracemalloc Python 峰值、进程 RSS 高水位与输出文件大小。

数据安全：默认在系统临时目录生成合成项目（与 performance_benchmark
同一合成器），或用 ``--project`` 复用既有基准现场；不读取
``local_private_data/``，不修改任何原始资料。

示例
----
    uv run python scripts/export_profile.py --rows 10000
    uv run python scripts/export_profile.py --project /tmp/.../workspace
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import sys
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if SRC_ROOT.is_dir() and str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

JSON_NAME = "export_profile.json"
MARKDOWN_NAME = "export_profile.md"


def _rss_mb() -> float | None:
    try:
        import resource

        value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, AttributeError, OSError):
        return None
    if sys.platform == "darwin":
        return round(value / (1024 * 1024), 3)
    return round(value / 1024, 3)


class StageTimer:
    """把模块函数包装成累计计时器；不改产品代码。"""

    def __init__(self) -> None:
        self.stages: dict[str, float] = {}
        self.calls: dict[str, int] = {}
        self._order: list[str] = []

    def wrap(self, module: Any, name: str, stage: str) -> None:
        original = getattr(module, name)
        timer = self

        def instrumented(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - started
                timer.stages[stage] = timer.stages.get(stage, 0.0) + elapsed
                timer.calls[stage] = timer.calls.get(stage, 0) + 1
                if stage not in timer._order:
                    timer._order.append(stage)

        setattr(module, name, instrumented)

    def measure(self, stage: str, operation: Callable[[], Any]) -> Any:
        started = time.perf_counter()
        try:
            return operation()
        finally:
            self.stages[stage] = self.stages.get(stage, 0.0) + (
                time.perf_counter() - started
            )
            self.calls[stage] = self.calls.get(stage, 0) + 1
            self._order.append(stage)

    def ordered(self) -> list[dict[str, Any]]:
        return [
            {
                "stage": stage,
                "seconds": round(self.stages[stage], 4),
                "calls": self.calls[stage],
            }
            for stage in sorted(self.stages, key=self._order.index)
        ]


def _prepare_synthetic_project(rows: int, work_dir: Path) -> tuple[Any, int, Path]:
    """生成并导入合成项目（复用 performance_benchmark 的合成器与口径）。"""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import performance_benchmark as bench  # noqa: PLC0415 - 复用基准合成器

    from jiadun.core.engine import aggregate, crosscheck, settlement_io
    from jiadun.core.models import project as project_model

    work_dir.mkdir(parents=True, exist_ok=True)
    project_model._SETTINGS_FILE = work_dir / "settings.json"
    bench._generate_inputs(work_dir / "inputs", rows)
    info, conn = bench._open_project(work_dir, rows)
    project_id = int(info.project_id)
    try:
        for direction, n_rows in bench._split_direction_rows(rows).items():
            source = bench._workbook_path(work_dir / "inputs", direction, n_rows)
            report = settlement_io.import_settlement_file(
                conn, project_id, Path(info.workspace_path), source,
                period_no=1, direction=direction,
            )
            if report.status != "ok":
                raise RuntimeError(f"{direction} 导入未完成：{report.status}")
        # export 前置门控要求已有校核/聚合成果（与基准链路一致）。
        for direction in ("upward", "downward"):
            aggregates = aggregate.aggregate_project(conn, project_id, direction=direction)
            aggregate.persist_period_totals(conn, project_id, aggregates)
        for direction in ("upward", "downward"):
            crosscheck.run_crosscheck(conn, project_id, [1], direction=direction)
    except Exception:
        conn.close()
        raise
    return conn, project_id, Path(info.workspace_path)


def _open_existing_project(project_dir: Path) -> tuple[Any, int, Path]:
    from jiadun.core.models import project as project_model

    reopened, conn = project_model.open_project(project_dir)
    row = conn.execute(
        "SELECT id, workspace_path FROM projects ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        conn.close()
        raise ValueError(f"项目目录中没有项目：{project_dir}")
    return conn, int(row["id"]), Path(row["workspace_path"])


def run_profile(
    *,
    rows: int | None,
    project: Path | None,
    output: Path,
) -> dict[str, Any]:
    from jiadun.core.export import excel_export

    output = output.expanduser().resolve()
    if f"{os.sep}local_private_data{os.sep}" in f"{output}{os.sep}":
        raise ValueError("profiling 输出不能写入 local_private_data")

    timer = StageTimer()
    timer.wrap(excel_export, "build_report_model", "build_model")
    for name in (
        "export_cover_page", "export_management_summary", "export_settlement_summary",
        "export_updown_comparison", "export_control_conclusions",
        "export_rate_rules_sheet", "export_diff_sheets", "export_diff_radar_sheet",
        "export_project_versions_sheets", "export_historical_price_sheet",
        "export_anomaly_lists", "export_contract_risks", "export_evidence_index",
        "export_audit_worksheet",
    ):
        timer.wrap(excel_export, name, f"sheet:{name.removeprefix('export_')}")
    timer.wrap(excel_export, "_autowidth", "autowidth")
    timer.wrap(excel_export, "_style_used_range", "style")
    timer.wrap(excel_export, "_prepare_data_sheet", "data_sheet_prep")
    timer.wrap(excel_export, "_link_evidence_references", "evidence_links")

    conn = None
    try:
        if project is not None:
            conn, project_id, workspace = _open_existing_project(project.resolve())
            source = {"mode": "existing_project", "workspace": workspace.name}
        else:
            assert rows is not None
            work = output / "work" / f"rows-{rows}"
            conn, project_id, workspace = _prepare_synthetic_project(rows, work)
            source = {"mode": "synthetic", "rows": rows}

        export_dir = output / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        gc.collect()
        rss_before = _rss_mb()
        tracemalloc.start()

        import cProfile
        import pstats

        profiler = cProfile.Profile()
        profiler.enable()
        path = timer.measure(
            "export_workbook_total",
            lambda: excel_export.export_workbook(conn, project_id, export_dir),
        )
        profiler.disable()
        _current, python_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rss_after = _rss_mb()

        stats = pstats.Stats(profiler)
        # 函数级热点（累计时间 Top 25），只保留可读的聚合行。
        top_functions: list[dict[str, Any]] = []
        total_tt = 0.0
        entries: list[tuple[str, str, Any]] = []
        for func, (_cc, nc, tt, ct, _callers) in stats.stats.items():
            total_tt += tt
            filename, lineno, name = func
            entries.append((ct, tt, (filename, lineno, name), nc))
        entries.sort(reverse=True)
        for ct, tt, (filename, lineno, name), nc in entries[:25]:
            top_functions.append({
                "function": f"{Path(filename).name}:{lineno}:{name}",
                "cumulative_seconds": round(ct, 4),
                "self_seconds": round(tt, 4),
                "calls": nc,
            })

        artifact = Path(path)
        report: dict[str, Any] = {
            "schema_version": 1,
            "profile": "Jiadun export_workbook stage profile",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "environment": {
                "system": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
            },
            "source": source,
            "stages": timer.ordered(),
            "python_peak_mb": round(python_peak / (1024 * 1024), 3),
            "rss_before_mb": rss_before,
            "rss_after_mb": rss_after,
            "output_file": {
                "name": artifact.name,
                "bytes": artifact.stat().st_size,
            },
            "top_functions_by_cumulative": top_functions,
            "note": (
                "合成数据性能观察，不构成业务结论；阶段包装器不改产品代码，"
                "save 阶段含受控产物登记，autowidth 为各写入器内部累计。"
            ),
        }
        output.mkdir(parents=True, exist_ok=True)
        (output / JSON_NAME).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        _write_markdown(report, output / MARKDOWN_NAME, total_tt)
        return report
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _write_markdown(report: dict[str, Any], path: Path, profile_total: float) -> None:
    lines = [
        "# 价盾 Excel 审核底稿导出 profiling 报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 数据来源：{report['source']}",
        f"- Python 峰值：{report['python_peak_mb']} MB；"
        f"RSS 前后：{report['rss_before_mb']} → {report['rss_after_mb']} MB",
        f"- 输出文件：{report['output_file']['name']}"
        f"（{report['output_file']['bytes']:,} B）",
        f"- cProfile 统计总耗时（自身）：{profile_total:.3f}s",
        "",
        "## 阶段耗时",
        "",
        "| 阶段 | 调用次数 | 累计耗时（秒） |",
        "|---|---:|---:|",
    ]
    for stage in report["stages"]:
        lines.append(
            f"| {stage['stage']} | {stage['calls']} | {stage['seconds']:.3f} |"
        )
    lines.extend([
        "",
        "## 函数级热点（cProfile 累计时间 Top 25）",
        "",
        "| 函数 | 累计（秒） | 自身（秒） | 调用次数 |",
        "|---|---:|---:|---:|",
    ])
    for entry in report["top_functions_by_cumulative"]:
        lines.append(
            f"| `{entry['function']}` | {entry['cumulative_seconds']:.3f} "
            f"| {entry['self_seconds']:.3f} | {entry['calls']:,} |"
        )
    lines.extend([
        "",
        "## 限制",
        "",
        "- 本报告只覆盖 `export_workbook` 一次调用；包装器自身的开销"
        "（约每阶段微秒级）已包含在阶段数字中。",
        "- 合成数据不代表真实业务规模下的异常/匹配分布；优化决策必须同时"
        "引用本报告与 performance_benchmark 的全链路阶段数字。",
        "- 不改变任何金额口径；本工具只读项目数据库并写独立导出目录。",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="对 Excel 审核底稿导出做阶段级 profiling（宪章 §七）。"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--rows", type=int, default=None,
                        help="生成该规模的合成项目（对上/对下均分）后 profiling")
    source.add_argument("--project", type=Path, default=None,
                        help="复用既有项目工作区目录（如基准 --keep-workspace 现场）")
    parser.add_argument("--output", type=Path,
                        default=Path("/tmp/jiadun-export-profile"),
                        help="报告与导出目录（默认 /tmp/jiadun-export-profile）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.rows is not None and not 1 <= args.rows <= 1_000_000:
        raise SystemExit("--rows 必须在 1–1000000 之间")
    report = run_profile(rows=args.rows, project=args.project, output=args.output)
    print(f"JSON：{args.output / JSON_NAME}")
    print(f"Markdown：{args.output / MARKDOWN_NAME}")
    total = sum(s["seconds"] for s in report["stages"]
                if s["stage"] == "export_workbook_total")
    print(f"export_workbook 总耗时：{total:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
