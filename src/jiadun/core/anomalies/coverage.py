"""检测执行覆盖率与技术失败记录。

覆盖率是运行完整性事实，不是业务问题模型。它单独记录本次运行应执行、
已执行、明确跳过和失败的检查，避免“只返回发现项”被误读成“全量检查通过”。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jiadun.core.contracts import run_contract

NOT_STARTED = "not_started"
PARTIAL = "partial"
COMPLETE = "complete"
FAILED = "failed"
UNAVAILABLE = run_contract.FAIL_CLOSED_STATUS
VALID_STATUSES = {NOT_STARTED, PARTIAL, COMPLETE, FAILED, UNAVAILABLE}
ANOMALY_DETECTION = "anomaly_detection"
AGGREGATE_VALIDATION = "aggregate_validation"
_AGGREGATE_VALIDATION_TOKEN = object()


def _unique(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    elif not isinstance(values, (list, tuple, set)):
        values = [values]
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if text not in seen:
            seen.add(text)
            result.append(text)
    return tuple(result)


def _mapping(values: Any) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    return {str(key): str(value) for key, value in values.items()}


def _raw_sequence(values: Any) -> tuple[tuple[str, ...], bool]:
    """把输入变成可审计的字符串序列，并标记不支持的容器类型。"""
    if values is None:
        return (), False
    if isinstance(values, str):
        return (values,), False
    if isinstance(values, (list, tuple, set)):
        return tuple(str(value) for value in values), False
    return (str(values),), True


@dataclass(frozen=True)
class DetectionCoverage:
    """一次检测运行的可审阅覆盖率快照。"""

    expected: tuple[str, ...] = ()
    executed: tuple[str, ...] = ()
    skipped: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    critical_failed: tuple[str, ...] = ()
    unavailable: bool = False
    invalid: bool = False
    invalid_reasons: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.unavailable:
            return UNAVAILABLE
        expected = set(self.expected)
        unexpected = (
            (set(self.executed) | set(self.skipped) | set(self.failed)
             | set(self.critical_failed)) - expected
        )
        if self.invalid or unexpected:
            return FAILED
        accounted = set(self.executed) | set(self.skipped) | set(self.failed)
        if not expected:
            return NOT_STARTED
        if self.failed or self.critical_failed:
            return FAILED
        if self.skipped:
            return PARTIAL
        if not expected.issubset(accounted):
            return PARTIAL
        return COMPLETE

    @property
    def executed_count(self) -> int:
        return len(self.executed)

    @property
    def expected_count(self) -> int:
        return len(self.expected)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "expected": list(self.expected),
            "executed": list(self.executed),
            "skipped": dict(self.skipped),
            "failed": dict(self.failed),
            "critical_failed": list(self.critical_failed),
            "unavailable": self.unavailable,
            "invalid": self.invalid,
            "invalid_reasons": list(self.invalid_reasons),
            "expected_count": self.expected_count,
            "executed_count": self.executed_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
        }


def coverage_from_values(
    expected: Any = (),
    executed: Any = (),
    skipped: Any = None,
    failed: Any = None,
    critical_failed: Any = (),
) -> DetectionCoverage:
    """从列表/JSON 兼容值构造快照，并保留失败原因。"""
    expected_values = _unique(expected)
    executed_values = _unique(executed)
    skipped_values = _mapping(skipped)
    failed_values = _mapping(failed)
    critical_values = _unique(critical_failed)
    expected_set = set(expected_values)
    unexpected = (
        (set(executed_values) | set(skipped_values) | set(failed_values)
         | set(critical_values)) - expected_set
    )
    invalid_reasons: list[str] = []
    if unexpected:
        invalid_reasons.append("出现未声明的覆盖键: " + ", ".join(sorted(unexpected)))
    if expected is not None and not isinstance(expected, (str, list, tuple, set)):
        invalid_reasons.append("expected 不是列表或字符串")
    if executed is not None and not isinstance(executed, (str, list, tuple, set)):
        invalid_reasons.append("executed 不是列表或字符串")
    if skipped is not None and not isinstance(skipped, dict):
        invalid_reasons.append("skipped 不是对象")
    if failed is not None and not isinstance(failed, dict):
        invalid_reasons.append("failed 不是对象")
    if critical_failed is not None and not isinstance(critical_failed, (str, list, tuple, set)):
        invalid_reasons.append("critical_failed 不是列表或字符串")
    for field_name, raw_value in (
        ("expected", expected),
        ("executed", executed),
        ("critical_failed", critical_failed),
    ):
        raw_values, unsupported = _raw_sequence(raw_value)
        if unsupported:
            invalid_reasons.append(f"{field_name} 不是列表或字符串")
        if len(raw_values) != len(set(raw_values)):
            invalid_reasons.append(f"{field_name} 存在重复键")
    if isinstance(skipped, dict):
        raw_keys = tuple(str(key) for key in skipped)
        if len(raw_keys) != len(set(raw_keys)):
            invalid_reasons.append("skipped 存在重复键")
    if isinstance(failed, dict):
        raw_keys = tuple(str(key) for key in failed)
        if len(raw_keys) != len(set(raw_keys)):
            invalid_reasons.append("failed 存在重复键")
    return DetectionCoverage(
        expected=expected_values,
        executed=executed_values,
        skipped=skipped_values,
        failed=failed_values,
        critical_failed=critical_values,
        invalid=bool(invalid_reasons),
        invalid_reasons=tuple(dict.fromkeys(invalid_reasons)),
    )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def record_detection_run(
    conn: sqlite3.Connection,
    project_id: int,
    coverage: DetectionCoverage,
    *,
    started_at: str | None = None,
    completed_at: str | None = None,
    run_signature: str | None = None,
    run_id: str | None = None,
    run_kind: str = ANOMALY_DETECTION,
    error_summary: str | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
    _internal_token: object | None = None,
) -> int:
    """保存一次覆盖率快照；``commit=False`` 用于与异常写入同一事务。"""
    if run_kind == AGGREGATE_VALIDATION and _internal_token is not _AGGREGATE_VALIDATION_TOKEN:
        has_periods = conn.execute(
            "SELECT 1 FROM settlement_periods WHERE project_id=? LIMIT 1",
            (project_id,),
        ).fetchone()
        if has_periods:
            raise ValueError(
                "aggregate_validation coverage 只能由 run_crosscheck 业务事务记录"
            )
    signature = run_signature or run_contract.current_run_signature(
        conn, project_id, ensure=True
    )
    if run_id is None:
        active = run_contract.get_current_contract(conn, project_id)
        if active is not None and active.signature == signature:
            run_id = active.run_id
    started = started_at or _now()
    finished = completed_at or _now()

    def _insert() -> int:
        cur = conn.execute(
            """INSERT INTO detection_runs(
                   project_id, run_signature, run_kind, started_at, completed_at, status,
                   expected_json, executed_json, skipped_json, failed_json,
                   critical_failed_json, error_summary, metadata_json, run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                project_id,
                signature,
                run_kind,
                started,
                finished,
                coverage.status,
                json.dumps(list(coverage.expected), ensure_ascii=False),
                json.dumps(list(coverage.executed), ensure_ascii=False),
                json.dumps(coverage.skipped, ensure_ascii=False),
                json.dumps(coverage.failed, ensure_ascii=False),
                json.dumps(list(coverage.critical_failed), ensure_ascii=False),
                error_summary,
                json.dumps(metadata or {}, ensure_ascii=False, default=str),
                run_id,
            ),
        )
        return int(cur.lastrowid)

    if commit:
        with run_contract._transaction(conn, "record_detection_run"):
            return _insert()
    return _insert()


def _json_value(value: str | None, fallback: Any) -> Any:
    try:
        result = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return result


def _row_to_coverage(row: sqlite3.Row) -> DetectionCoverage:
    return coverage_from_values(
        _json_value(row["expected_json"], []),
        _json_value(row["executed_json"], []),
        _json_value(row["skipped_json"], {}),
        _json_value(row["failed_json"], {}),
        _json_value(row["critical_failed_json"], []),
    )


def current_detection_run(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    run_kind: str = ANOMALY_DETECTION,
) -> sqlite3.Row | None:
    """只读取当前 Run Contract 下最近一次检测，不混入历史签名。"""
    fail_closed = run_contract.get_fail_closed_state(conn, project_id)
    current_contract = run_contract.get_current_contract(conn, project_id)
    signature = current_contract.signature if current_contract else None
    if not signature or not current_contract.run_id:
        return None
    row = conn.execute(
        """SELECT * FROM detection_runs
           WHERE project_id=? AND run_signature=? AND run_id=? AND run_kind=?
           ORDER BY id DESC LIMIT 1""",
        (project_id, signature, current_contract.run_id, run_kind),
    ).fetchone()
    # 运行级边界生效时，旧的 complete/partial 运行不能穿过边界。若本次
    # 失败覆盖已真实写入，则保留 FAILED 事实供诊断；不把它当作可用成果。
    if fail_closed and (row is None or row["status"] != FAILED):
        return None
    return row


def current_detection_coverage(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    run_kind: str = ANOMALY_DETECTION,
) -> DetectionCoverage:
    row = current_detection_run(conn, project_id, run_kind=run_kind)
    if row:
        return _row_to_coverage(row)
    return DetectionCoverage(
        unavailable=run_contract.get_fail_closed_state(conn, project_id) is not None
    )


def coverage_summary(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    run_kind: str = ANOMALY_DETECTION,
    read_only: bool = False,
) -> dict[str, Any]:
    """返回 UI/Excel/Word 可共用的简洁覆盖率字段。

    只读摘要不得尝试清除运行级 fail-closed 侧车；清除动作必须由显式的
    成功运行恢复流程完成。
    """
    availability = run_contract.current_results_available(
        conn, project_id, allow_state_clear=not read_only
    )
    row = current_detection_run(conn, project_id, run_kind=run_kind)
    coverage = _row_to_coverage(row) if row else DetectionCoverage(
        unavailable=not availability["available"]
    )
    result = coverage.as_dict()
    result["run_id"] = int(row["id"]) if row else None
    result["run_kind"] = run_kind
    result["run_signature"] = row["run_signature"] if row else None
    result["contract_run_id"] = row["run_id"] if row else None
    result["started_at"] = row["started_at"] if row else None
    result["completed_at"] = row["completed_at"] if row else None
    result["error_summary"] = row["error_summary"] if row else None
    result["availability_status"] = availability["status"]
    result["fail_closed"] = not availability["available"]
    result["current_results_available"] = availability["available"]
    result["availability_reason"] = availability.get("reason")
    result["physical_limitations"] = list(availability.get("physical_limitations") or [])
    return result
