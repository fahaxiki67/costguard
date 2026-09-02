"""正式分析入口。

工作台的“分析”操作只应从这里进入。入口固定先运行包含 A/B/C 的项目校核，
再运行异常规则；任一阶段失败都保留已经得到的结果，但整体状态保持失败关闭，
不得让调用方把部分结果误当成项目级通过结论。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from jiadun.core.anomalies import engine as anomaly_engine
from jiadun.core.engine import crosscheck

ANALYSIS_STAGE_ORDER = ("crosscheck", "anomalies")
"""正式分析阶段顺序；crosscheck 内部包含 A、B、C 三条路径。"""

SAME_ROW_SET_WARNING = "读取/计算过程不同但参与行集相同，不能证明行集完整性"


def same_row_set_warning(status: str | None) -> str:
    """返回 ``same_row_set`` 的业务语义提示，不把相同行集写成独立证明。"""
    return SAME_ROW_SET_WARNING if status == "same_row_set" else ""


@dataclass
class AnalysisResult:
    """一次正式分析的阶段结果和 fail-closed 状态。

    ``status`` 描述编排是否跑完；``conclusion`` 描述当前结果能否形成项目结论。
    两者故意分开，避免“两个阶段都返回了”被误读为业务审核通过。
    """

    crosscheck_results: list[Any] = field(default_factory=list)
    anomaly_findings: list[Any] = field(default_factory=list)
    stage_order: tuple[str, ...] = ANALYSIS_STAGE_ORDER
    status: str = "failed"  # 'completed' | 'failed'
    conclusion: str = "cannot_conclude"  # 'can_conclude' | 'conditional' | 'cannot_conclude'
    failed_stage: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def error(self) -> str | None:
        """兼容调用方读取的首个错误；完整错误仍保存在 ``errors``。"""
        return self.errors[0] if self.errors else None

    @property
    def failed_stages(self) -> tuple[str, ...]:
        """返回失败阶段，按正式执行顺序排列。"""
        return tuple(
            stage for stage in ANALYSIS_STAGE_ORDER
            if any(message.startswith(f"{stage}:") for message in self.errors)
        )

    @property
    def fail_closed(self) -> bool:
        """当前结果是否仍受项目级结论闸门约束。"""
        return self.conclusion != "can_conclude"

    @property
    def ok(self) -> bool:
        """仅表示编排阶段未抛出异常，不代表业务结论已经确认。"""
        return self.status == "completed"

    def as_dict(self) -> dict[str, Any]:
        """返回适合日志/UI 的无业务计算副作用快照。"""
        return {
            "status": self.status,
            "conclusion": self.conclusion,
            "stage_order": list(self.stage_order),
            "completed_stages": [
                stage for stage in self.stage_order if stage not in self.failed_stages
            ],
            "failed_stages": list(self.failed_stages),
            "errors": list(self.errors),
            "fail_closed": self.fail_closed,
            "crosscheck_count": len(self.crosscheck_results),
            "anomaly_count": len(self.anomaly_findings),
        }


def _error_text(stage: str, error: BaseException) -> str:
    message = str(error).strip() or type(error).__name__
    return f"{stage}: {message[:1000]}"


def _anomaly_stage_has_technical_failure(findings: list[Any]) -> bool:
    """识别异常引擎已捕获并返回的规则级技术失败。"""
    return any(
        str(getattr(finding, "rule_id", "")).startswith("rule_error_")
        or getattr(finding, "detection_mode", None) == "technical_failure"
        for finding in findings
    )


def _materialize_period_totals(conn: Any, project_id: int) -> None:
    """在正式校核前物化当前导入的聚合行。

    导入阶段先保留原始网格和清洗明细；``period_totals`` 是校核结果回写
    所需的逐清单投影，只有在用户真正启动正式分析时才建立/刷新。把这一步
    放在编排入口，既让新项目可以直接走到 A/B/C，又保留
    ``crosscheck.run_crosscheck`` 对手工/损坏数据库缺行的严格检测。非
    SQLite 的编排测试连接不执行数据库副作用。
    """
    if not isinstance(conn, sqlite3.Connection):
        return
    from jiadun.core.engine import aggregate

    aggs = aggregate.aggregate_project(
        conn,
        project_id,
        include_all_directions=True,
        persist_derived_flags=False,
    )
    if aggs:
        aggregate.persist_period_totals(conn, project_id, aggs)


def run_analysis(conn, project_id: int) -> AnalysisResult:
    """按固定顺序执行一次项目分析并返回 fail-closed 结果。

    ``crosscheck.run_crosscheck_project`` 是 A/B/C 的正式核心入口。即使该阶段
    抛出异常，也继续尝试异常检测，便于用户获得可复核的局部问题；但任何阶段
    失败都不会升级为项目级结论。真正的项目状态仍由共享报告状态和证据闸门
    决定，本函数不复制金额计算或业务责任判断。
    """
    result = AnalysisResult(stage_order=ANALYSIS_STAGE_ORDER)

    try:
        _materialize_period_totals(conn, project_id)
        result.crosscheck_results = list(
            crosscheck.run_crosscheck_project(conn, project_id)
        )
    except Exception as exc:  # noqa: BLE001 - 正式入口需收集阶段失败并继续收束
        result.errors.append(_error_text("crosscheck", exc))

    try:
        result.anomaly_findings = list(
            anomaly_engine.run_anomalies(conn, project_id)
        )
        if _anomaly_stage_has_technical_failure(result.anomaly_findings):
            result.errors.append("anomalies: 异常规则覆盖不完整，当前结果需人工复核")
    except Exception as exc:  # noqa: BLE001 - 由返回值统一 fail-closed
        result.errors.append(_error_text("anomalies", exc))

    result.failed_stage = result.failed_stages[0] if result.failed_stages else None
    result.status = "failed" if result.errors else "completed"
    # 结论不在编排层自行升级。即使两个阶段都完成，也必须由共享报告的
    # Evidence/覆盖闸门决定；当前入口默认保守返回不可形成项目结论，避免
    # “阶段完成”被误读为“项目通过”。
    result.conclusion = "cannot_conclude" if result.errors else "conditional"
    return result


# 给旧调用方和不同界面入口保留明确的语义别名，所有别名都指向同一固定编排。
run_project_analysis = run_analysis
run_full_analysis = run_analysis


__all__ = [
    "ANALYSIS_STAGE_ORDER",
    "AnalysisResult",
    "SAME_ROW_SET_WARNING",
    "run_analysis",
    "run_full_analysis",
    "run_project_analysis",
    "same_row_set_warning",
]
