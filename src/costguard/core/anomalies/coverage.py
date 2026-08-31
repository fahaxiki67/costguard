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

from costguard.core.contracts import run_contract

NOT_STARTED = "not_started"
PARTIAL = "partial"
COMPLETE = "complete"
FAILED = "failed"
VALID_STATUSES = {NOT_STARTED, PARTIAL, COMPLETE, FAILED}
ANOMALY_DETECTION = "anomaly_detection"
AGGREGATE_VALIDATION = "aggregate_validation"


def _unique(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
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


@dataclass(frozen=True)
class DetectionCoverage:
    """一次检测运行的可审阅覆盖率快照。"""

    expected: tuple[str, ...] = ()
    executed: tuple[str, ...] = ()
    skipped: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    critical_failed: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        expected = set(self.expected)
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
    executed_values = tuple(item for item in _unique(executed) if item in set(expected_values))
    skipped_values = {
        key: value for key, value in _mapping(skipped).items() if key in set(expected_values)
    }
    failed_values = {
        key: value for key, value in _mapping(failed).items() if key in set(expected_values)
    }
    critical_values = tuple(
        item for item in _unique(critical_failed) if item in set(expected_values)
    )
    return DetectionCoverage(
        expected=expected_values,
        executed=executed_values,
        skipped=skipped_values,
        failed=failed_values,
        critical_failed=critical_values,
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
    run_kind: str = ANOMALY_DETECTION,
    error_summary: str | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> int:
    """保存一次覆盖率快照；``commit=False`` 用于与异常写入同一事务。"""
    signature = run_signature or run_contract.current_run_signature(
        conn, project_id, ensure=True
    )
    started = started_at or _now()
    finished = completed_at or _now()

    def _insert() -> int:
        cur = conn.execute(
            """INSERT INTO detection_runs(
                   project_id, run_signature, run_kind, started_at, completed_at, status,
                   expected_json, executed_json, skipped_json, failed_json,
                   critical_failed_json, error_summary, metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
    signature = run_contract.current_run_signature(conn, project_id)
    if not signature:
        return None
    return conn.execute(
        """SELECT * FROM detection_runs
           WHERE project_id=? AND run_signature=? AND run_kind=?
           ORDER BY id DESC LIMIT 1""",
        (project_id, signature, run_kind),
    ).fetchone()


def current_detection_coverage(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    run_kind: str = ANOMALY_DETECTION,
) -> DetectionCoverage:
    row = current_detection_run(conn, project_id, run_kind=run_kind)
    return _row_to_coverage(row) if row else DetectionCoverage()


def coverage_summary(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    run_kind: str = ANOMALY_DETECTION,
) -> dict[str, Any]:
    """返回 UI/Excel/Word 可共用的简洁覆盖率字段。"""
    row = current_detection_run(conn, project_id, run_kind=run_kind)
    coverage = _row_to_coverage(row) if row else DetectionCoverage()
    result = coverage.as_dict()
    result["run_id"] = int(row["id"]) if row else None
    result["run_kind"] = run_kind
    result["run_signature"] = row["run_signature"] if row else None
    result["started_at"] = row["started_at"] if row else None
    result["completed_at"] = row["completed_at"] if row else None
    result["error_summary"] = row["error_summary"] if row else None
    return result
