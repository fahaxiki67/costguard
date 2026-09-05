"""运行契约（Run Contract）与成果登记。

一次校核、异常检测、匹配或导出只有在同一组输入、映射、规则、代码和
数据库结构下才可互相引用。本模块把这组条件规范化为 SHA-256 签名，并
把旧签名保留为历史、从当前读取面剔除。

注意：``migrations.connect`` 使用 SQLite autocommit，因此本模块的写操作
显式开启事务。所有路径只登记项目库中的只读副本和结构化配置，不读取
``local_private_data/``。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Any

from jiadun import branding
from jiadun.core.db import migrations
from jiadun.core.engine.sheet_digest import sheet_cell_digest
from jiadun.core.evidence.finding import canonical_json, stable_fingerprint
from jiadun.version import app_version

LEGACY_STALE_SIGNATURE = "legacy:stale"
# 运行条件不变但本次重跑未形成可用结果时使用的非当前标记。它与旧库迁移
# 的 ``legacy:stale`` 分开，便于读取面和审计记录区分“历史旧数据”和“本次
# 校核尝试失效”。两者都不会匹配当前 Run Contract 签名。
INVALIDATED_RUN_SIGNATURE = "run:invalidated"
# v2（2026-09-05）：sheet_scope 纳入 list_kind（任务书 B5 角色变更失效），
# 摘要计算收敛到 engine.sheet_digest 单一实现。旧签名全部失效并重建。
CONTRACT_FORMAT_VERSION = 2
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_TRANSACTION_COMMIT_FAILURE_ATTR = "_jiadun_transaction_commit_failure"
FAIL_CLOSED_STATUS = "unavailable"
_FAIL_CLOSED_FORMAT_VERSION = 1
_PENDING_CLEAR_KEY = "pending_clear"
# 当前项目结果的 fail-closed 边界只能由聚合校核覆盖证明解除。不要从调用方
# 传入的任意字符串推断解除依据；侧车中会保存本次边界的恢复运行类型。
DEFAULT_RECOVERY_RUN_KIND = "aggregate_validation"
_FAIL_CLOSED_STATES: dict[str, dict[str, Any]] = {}
_FAIL_CLOSED_LOCK = threading.RLock()


def _validate_recovery_run_kind(value: str | None) -> str:
    """固定当前成果的恢复依据，不允许调用方改成其他运行类型。"""
    if value is None:
        return DEFAULT_RECOVERY_RUN_KIND
    normalized = str(value)
    if normalized != DEFAULT_RECOVERY_RUN_KIND:
        raise ValueError(
            "当前运行级不可用边界只能由 aggregate_validation 成功运行解除"
        )
    return DEFAULT_RECOVERY_RUN_KIND


class CurrentResultsUnavailableError(RuntimeError):
    """当前运行结果不可用，调用方不得继续生成或登记当前成果。"""

    def __init__(self, message: str, *, state: dict[str, Any] | None = None):
        super().__init__(message)
        self.state = state or {}


# 兼容不同调用方对运行级不可用异常的命名。
RunUnavailableError = CurrentResultsUnavailableError


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """按块计算文件 SHA-256；不把整个工作簿读入内存。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def _safe_normalized_stored_path(value: Any) -> tuple[str, str | None]:
    """规范化存储副本路径，并把异常归类为可审计的路径错误。"""
    raw = str(value or "").strip()
    if not raw:
        return "", "empty"
    try:
        return str(Path(raw).expanduser().resolve(strict=False)), None
    except RuntimeError:
        # pathlib 对符号链接循环以 RuntimeError 表示；它仍然属于不可读取
        # 的输入证据，不能让状态刷新把异常泄漏到 UI 或导出调用方。
        return "", "symlink_loop"
    except (OSError, ValueError):
        # ValueError 覆盖嵌入 NUL 等非法路径；OSError 覆盖权限、ELOOP 等
        # 文件系统边界。统一交给源文件闸门生成 fail-closed 缺口。
        return "", "invalid"


def _normalized_stored_path(value: Any) -> str:
    """把受控源文件副本路径规范化为可比较的绝对路径。

    Run Contract 记录的是解析器实际读取的 ``stored_path``，因此比较时不能
    仅依赖 SQL 中的原始字符串（相对路径、``~`` 和符号链接写法可能不同）。
    ``resolve(strict=False)`` 不要求路径当前存在；缺失/不可读由读取闸门单独
    报告，避免把一个无法访问的副本静默变成另一条路径。
    """
    normalized, _error = _safe_normalized_stored_path(value)
    return normalized


def _stored_path_error_text(error: str | None) -> str:
    return {
        "empty": "路径为空",
        "invalid": "路径格式无效或不可解析",
        "symlink_loop": "符号链接循环",
    }.get(error or "", "路径不可解析")


def _strict_size_bytes(value: Any) -> int | None:
    """只接受数据库中真实的非负整数文件大小，不允许截断浮点值。"""
    # SQLite 的 INTEGER 列仍可能被外部 SQL 写入 REAL/TEXT；不能用 int(value)
    # 把 6.9 静默截断为 6，也不能把 bool 当成合法文件大小。
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _is_transaction_commit_failure(error: BaseException) -> bool:
    """判断异常是否来自外层事务的 COMMIT，而不是事务体内的写入。"""
    return bool(getattr(error, _TRANSACTION_COMMIT_FAILURE_ATTR, False))


def _database_path(conn: sqlite3.Connection, project_id: int) -> Path | None:
    """读取连接实际打开的项目库路径；移动项目时不依赖过期的 workspace_path。"""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        rows = []
    for row in rows:
        try:
            name, filename = row[1], row[2]
        except (IndexError, KeyError, TypeError):
            name = row["name"]
            filename = row["file"]
        if name == "main" and filename:
            return Path(str(filename)).expanduser().resolve()

    # 仅作为内存/特殊连接的兼容回退；普通项目连接优先使用 PRAGMA 的真实路径。
    try:
        row = conn.execute(
            "SELECT workspace_path FROM projects WHERE id=?", (project_id,)
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row and row["workspace_path"]:
        return (Path(row["workspace_path"]).expanduser() / "project.db").resolve()
    return None


def fail_closed_state_path(conn: sqlite3.Connection, project_id: int) -> Path:
    """返回与项目数据库同目录的运行级不可用侧车路径。"""
    db_path = _database_path(conn, project_id)
    if db_path is None:
        raise RuntimeError(f"cannot resolve project database path for project {project_id}")
    # 同一 project.db 可能承载多个项目；项目 ID 必须属于状态文件命名空间，
    # 否则项目 A 的失败会把项目 B 一起锁死，或项目 B 清理时误删 A 的边界。
    return db_path.with_name(f"{branding.RUN_STATE_PREFIX}{int(project_id)}.json")


def legacy_fail_closed_state_paths(
    conn: sqlite3.Connection, project_id: int
) -> tuple[Path, ...]:
    """返回旧版 CostGuard 侧车候选路径，仅读取、不创建、不删除。

    v0.1.8 的历史实现带有 ``project.db`` 前缀；早期/外部工具可能使用不带
    前缀的标准形式，因此两种旧路径都纳入只读发现。
    """
    db_path = _database_path(conn, project_id)
    if db_path is None:
        return ()
    candidates = (
        db_path.with_name(f"{branding.LEGACY_RUN_STATE_PREFIX}{int(project_id)}.json"),
        db_path.with_name(
            f".{db_path.name}{branding.LEGACY_RUN_STATE_PREFIX}{int(project_id)}.json"
        ),
    )
    current = fail_closed_state_path(conn, project_id)
    return tuple(path for path in candidates if path != current)


def _state_key(conn: sqlite3.Connection, project_id: int) -> str:
    try:
        return f"{fail_closed_state_path(conn, project_id)}::{int(project_id)}"
    except Exception:
        # 该回退仅在连接无法提供数据库路径时使用，明确属于当前连接范围。
        return f"connection:{id(conn)}::{int(project_id)}"


def _write_fail_closed_state_file(path: Path, payload: dict[str, Any]) -> None:
    """原子写入侧车；临时文件和目标文件位于同一目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _fail_closed_payload(
    project_id: int,
    reason: str,
    *,
    run_signature: str | None = None,
    error: BaseException | None = None,
    persisted: bool,
    persistence: str,
    persistence_error: str | None = None,
    recovery_run_kind: str = DEFAULT_RECOVERY_RUN_KIND,
) -> dict[str, Any]:
    recovery_run_kind = _validate_recovery_run_kind(recovery_run_kind)
    limitations = []
    if not persisted:
        limitations.append(
            "运行级状态仅保存在当前进程内；新进程无法读取该边界，跨进程物理限制需人工处理"
        )
    return {
        "format_version": _FAIL_CLOSED_FORMAT_VERSION,
        "project_id": int(project_id),
        "status": FAIL_CLOSED_STATUS,
        "recovery_run_kind": str(recovery_run_kind),
        "reason": str(reason),
        "run_signature": run_signature,
        "error_type": type(error).__name__ if error is not None else None,
        "error_message": str(error) if error is not None else None,
        "created_at": _now(),
        "persisted": bool(persisted),
        "persistence": persistence,
        "persistence_error": persistence_error,
        "physical_limitations": limitations,
    }


def _invalid_state_payload(project_id: int, error: BaseException) -> dict[str, Any]:
    payload = _fail_closed_payload(
        project_id,
        "运行级不可用状态文件无法读取，当前结果不可用",
        error=error,
        persisted=True,
        persistence="sidecar",
        persistence_error=str(error),
    )
    payload["corrupt"] = True
    return payload


def set_fail_closed_state(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    reason: str,
    run_signature: str | None = None,
    error: BaseException | None = None,
    recovery_run_kind: str = DEFAULT_RECOVERY_RUN_KIND,
) -> dict[str, Any]:
    """设置运行级不可用边界，优先写项目侧车，失败时保留进程内边界。"""
    recovery_run_kind = _validate_recovery_run_kind(recovery_run_kind)
    path: Path | None
    try:
        path = fail_closed_state_path(conn, project_id)
    except Exception as path_error:
        payload = _fail_closed_payload(
            project_id,
            reason,
            run_signature=run_signature,
            error=error,
            persisted=False,
            persistence="process",
            persistence_error=str(path_error),
            recovery_run_kind=recovery_run_kind,
        )
        with _FAIL_CLOSED_LOCK:
            _FAIL_CLOSED_STATES[_state_key(conn, project_id)] = payload
        return dict(payload)

    payload = _fail_closed_payload(
        project_id,
        reason,
        run_signature=run_signature,
        error=error,
        persisted=True,
        persistence="sidecar",
        recovery_run_kind=recovery_run_kind,
    )
    key = _state_key(conn, project_id)
    try:
        _write_fail_closed_state_file(path, payload)
    except Exception as persistence_error:
        payload = _fail_closed_payload(
            project_id,
            reason,
            run_signature=run_signature,
            error=error,
            persisted=False,
            persistence="process",
            persistence_error=str(persistence_error),
            recovery_run_kind=recovery_run_kind,
        )
        with _FAIL_CLOSED_LOCK:
            _FAIL_CLOSED_STATES[key] = payload
        return dict(payload)

    with _FAIL_CLOSED_LOCK:
        # 持久化成功后不缓存文件状态，避免其他连接/进程清除侧车后本进程继续
        # 读取旧缓存；仅保留侧车不可写时的进程内状态。
        _FAIL_CLOSED_STATES.pop(key, None)
    return dict(payload)


def get_fail_closed_state(
    conn: sqlite3.Connection, project_id: int
) -> dict[str, Any] | None:
    """读取运行级不可用边界；进程内兜底优先于侧车。"""
    key = _state_key(conn, project_id)
    with _FAIL_CLOSED_LOCK:
        process_state = _FAIL_CLOSED_STATES.get(key)
        if process_state is not None:
            return dict(process_state)

    try:
        current_path = fail_closed_state_path(conn, project_id)
        paths = [(current_path, False)] + [
            (path, True) for path in legacy_fail_closed_state_paths(conn, project_id)
        ]
    except Exception:
        return None
    for path, is_legacy in paths:
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state payload is not an object")
            if int(raw.get("project_id")) != int(project_id):
                raise ValueError("state project_id does not match")
            if raw.get("status") != FAIL_CLOSED_STATUS:
                raise ValueError("state status is not fail-closed")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            # 侧车存在但无法可信读取时采取更安全的阻断，避免把损坏状态当作
            # “没有边界”。调用方可以从 persistence_error 看到物理限制。
            return _invalid_state_payload(project_id, error)
        raw["persisted"] = True
        raw["persistence"] = "sidecar"
        raw.setdefault("physical_limitations", [])
        # 仅在进程内标记来源，不能把内部路径写回旧侧车；清除逻辑据此避免
        # 误删 legacy 文件。
        raw["_sidecar_path"] = str(path)
        raw["_legacy_sidecar"] = bool(is_legacy)
        return dict(raw)
    return None


def defer_fail_closed_state_clear(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    run_signature: str,
    coverage_run_id: int,
    coverage_run_kind: str,
) -> None:
    """把边界清除延后到调用方外层事务真正提交之后。"""
    coverage_run_kind = _validate_recovery_run_kind(coverage_run_kind)
    state = get_fail_closed_state(conn, project_id)
    if state is None:
        return
    payload = dict(state)
    payload[_PENDING_CLEAR_KEY] = {
        "run_signature": run_signature,
        "coverage_run_id": int(coverage_run_id),
        "coverage_run_kind": str(coverage_run_kind),
        "requires_outer_commit": True,
    }
    key = _state_key(conn, project_id)
    try:
        path = fail_closed_state_path(conn, project_id)
        _write_fail_closed_state_file(path, payload)
    except Exception as persistence_error:
        # 旧侧车若存在仍然保持 fail-closed；同进程缓存只补充待清除元数据，
        # 新进程即使读不到该元数据也只会继续阻断，不会误暴露旧成功。
        payload["pending_clear_error"] = str(persistence_error)
        with _FAIL_CLOSED_LOCK:
            _FAIL_CLOSED_STATES[key] = payload
        return
    payload["persisted"] = True
    payload["persistence"] = "sidecar"
    payload["persistence_error"] = None
    payload["physical_limitations"] = []
    with _FAIL_CLOSED_LOCK:
        # 成功写入新的侧车版本后，避免本进程继续持有不带最新元数据的副本。
        _FAIL_CLOSED_STATES.pop(key, None)


def _pending_clear_is_committed(
    conn: sqlite3.Connection, project_id: int, state: dict[str, Any]
) -> bool:
    """仅当待清除运行的 coverage 行已在外层事务外可见时返回真。"""
    pending = state.get(_PENDING_CLEAR_KEY)
    if not isinstance(pending, dict) or conn.in_transaction:
        return False
    try:
        signature = str(pending["run_signature"])
        coverage_run_id = int(pending["coverage_run_id"])
        coverage_run_kind = str(pending["coverage_run_kind"])
    except (KeyError, TypeError, ValueError):
        return False
    return _coverage_proof_is_valid(
        conn,
        project_id,
        run_signature=signature,
        coverage_run_id=coverage_run_id,
        coverage_run_kind=coverage_run_kind,
        required_run_kind=DEFAULT_RECOVERY_RUN_KIND,
    )


def _aggregate_expected_coverage_keys(
    conn: sqlite3.Connection, project_id: int
) -> tuple[str, ...] | None:
    """从当前项目期次事实重建聚合校核的必需 A/B/C 键。

    没有任何期次的空项目没有可证明的业务覆盖范围；保留初始化流程的兼容
    性，但一旦项目存在期次，就不再接受调用方自定义的覆盖范围。
    """
    try:
        rows = conn.execute(
            "SELECT id FROM settlement_periods WHERE project_id=? ORDER BY id",
            (project_id,),
        ).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    return tuple(
        f"period:{int(row['id'])}:{path}"
        for row in rows
        for path in ("path_a", "path_b", "path_c")
    )


def _coverage_proof_is_valid(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    run_signature: str | None,
    coverage_run_id: int | None,
    coverage_run_kind: str | None,
    required_run_kind: str = DEFAULT_RECOVERY_RUN_KIND,
) -> bool:
    """验证清除不可用边界所需的当前完整 coverage 证明。"""
    if (
        not run_signature
        or coverage_run_id is None
        or not coverage_run_kind
        or str(coverage_run_kind) != str(required_run_kind)
    ):
        return False
    current_contract = get_current_contract(conn, project_id)
    if (
        conn.in_transaction
        or current_contract is None
        or current_contract.signature != run_signature
    ):
        return False
    try:
        row = conn.execute(
            """SELECT id, run_signature, run_kind, status, completed_at,
                              expected_json, executed_json,
                              skipped_json, failed_json, critical_failed_json,
                              metadata_json, run_id
                       FROM detection_runs WHERE id=? AND project_id=?""",
            (int(coverage_run_id), project_id),
        ).fetchone()
    except (TypeError, ValueError, sqlite3.Error):
        return False
    if row is None or row["run_signature"] != run_signature:
        return False
    if row["run_id"] != current_contract.run_id:
        return False
    if (
        int(row["id"]) != int(coverage_run_id)
        or row["run_kind"] != str(coverage_run_kind)
        or row["status"] != "complete"
        or not row["completed_at"]
    ):
        return False
    # 证明必须来自当前 run_kind/signature 下最新的一行；旧的 complete 行
    # 不能在更新的 failed/partial 行之后重新打开历史结果。
    latest = conn.execute(
        """SELECT id FROM detection_runs
           WHERE project_id=? AND run_signature=? AND run_id=? AND run_kind=?
           ORDER BY id DESC LIMIT 1""",
        (project_id, run_signature, current_contract.run_id, str(coverage_run_kind)),
    ).fetchone()
    if latest is None or int(latest["id"]) != int(coverage_run_id):
        return False

    expected = _strict_json_list(row["expected_json"], allow_empty=False)
    executed = _strict_json_list(row["executed_json"], allow_empty=False)
    skipped = _strict_json_mapping(row["skipped_json"])
    failed = _strict_json_mapping(row["failed_json"])
    critical_failed = _strict_json_list(row["critical_failed_json"], allow_empty=True)
    if expected is None or executed is None or skipped is None or failed is None:
        return False
    if critical_failed is None:
        return False
    # 集合相等之外还检查重复项，避免重复 executed 键被错误解释为完整证明。
    if not (
        len(expected) == len(set(expected))
        and len(executed) == len(set(executed))
        and set(expected) == set(executed)
        and not skipped
        and not failed
        and not critical_failed
    ):
        return False
    required_expected = _aggregate_expected_coverage_keys(conn, project_id)
    if required_expected is None:
        # 空项目没有业务期次，保留初始化阶段的通用覆盖率兼容性。
        return True
    if tuple(expected) != required_expected:
        return False

    # 项目一旦存在期次，coverage 不能只凭调用方声明的 JSON 形状清除边界。
    # 它必须由 run_crosscheck 产生，并且能和当前签名下的校核结果、期间汇总
    # 行及对应证据逐期勾稽；否则“完整 coverage”可能在没有任何业务结果时
    # 直接打开旧成果。
    metadata = _strict_json_mapping(row["metadata_json"])
    if metadata is None or metadata.get("producer") != "run_crosscheck":
        return False
    required_period_ids = [
        int(key.split(":")[1]) for key in required_expected[::3]
    ]
    metadata_period_ids = metadata.get("period_ids")
    if (
        not isinstance(metadata_period_ids, list)
        or any(isinstance(value, bool) or not isinstance(value, int) for value in metadata_period_ids)
        or sorted(metadata_period_ids) != sorted(required_period_ids)
        or metadata.get("result_count") != len(required_period_ids)
    ):
        return False
    placeholders = ",".join("?" for _ in required_period_ids)
    result_rows = conn.execute(
        f"""SELECT period_id, evidence_id, checked_at, status
               FROM crosscheck_results
               WHERE project_id=? AND run_signature=? AND run_id=?
                 AND period_id IN ({placeholders})""",
        (project_id, run_signature, current_contract.run_id, *required_period_ids),
    ).fetchall()
    if len(result_rows) != len(required_period_ids):
        return False
    result_by_period = {int(result["period_id"]): result for result in result_rows}
    if set(result_by_period) != set(required_period_ids):
        return False
    for period_id in required_period_ids:
        result = result_by_period[period_id]
        if not result["evidence_id"] or not result["checked_at"] or result["status"] == "invalidated":
            return False
        evidence = conn.execute(
            """SELECT 1 FROM evidence
               WHERE id=? AND project_id=? AND kind='cross_check'
                 AND run_signature=? AND run_id=?""",
            (result["evidence_id"], project_id, run_signature, current_contract.run_id),
        ).fetchone()
        if evidence is None:
            return False
        total_counts = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN run_signature=? AND run_id=? THEN 1 ELSE 0 END) AS current_count,
                      SUM(CASE WHEN run_signature=? AND run_id=? AND evidence_id=? THEN 1 ELSE 0 END)
                         AS linked_count
               FROM period_totals
               WHERE project_id=? AND period_id=?""",
            (
                run_signature, current_contract.run_id,
                run_signature, current_contract.run_id, result["evidence_id"],
                project_id, period_id,
            ),
        ).fetchone()
        if (
            int(total_counts["total"] or 0) == 0
            or int(total_counts["current_count"] or 0) != int(total_counts["total"] or 0)
            or int(total_counts["linked_count"] or 0) == 0
        ):
            return False
    return True


def clear_fail_closed_state(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    run_signature: str | None = None,
    coverage_run_id: int | None = None,
    coverage_run_kind: str | None = None,
) -> None:
    """仅在当前完整成功 coverage 证明存在时清除边界。"""
    # 没有边界时清除是幂等操作。先检查状态也兼容 :memory: 或特殊连接：
    # 这类连接无法解析持久化路径，但没有待清除的状态时不应让正常成功
    # 运行凭空失败；已有进程 fallback 时仍会继续走下面的路径并报告限制。
    state = get_fail_closed_state(conn, project_id)
    if state is None:
        return
    if conn.in_transaction:
        raise RuntimeError("外层事务尚未提交，不能清除当前结果不可用边界")
    try:
        stored_run_kind = _validate_recovery_run_kind(
            state.get("recovery_run_kind")
        )
        supplied_run_kind = _validate_recovery_run_kind(coverage_run_kind)
    except ValueError as error:
        raise RuntimeError("运行级不可用状态的恢复依据非法，继续保持不可用") from error
    if supplied_run_kind != stored_run_kind:
        raise RuntimeError("清除运行级不可用边界需要匹配边界类型的完整成功运行证明")
    proof = _coverage_proof_is_valid(
        conn,
        project_id,
        run_signature=run_signature,
        coverage_run_id=coverage_run_id,
        coverage_run_kind=supplied_run_kind,
        required_run_kind=DEFAULT_RECOVERY_RUN_KIND,
    )
    if not proof:
        raise RuntimeError("清除运行级不可用边界需要当前完整成功运行证明")
    if state.get("_legacy_sidecar"):
        raise RuntimeError("legacy CostGuard 运行状态侧车只读，不能由价盾自动删除")
    path = fail_closed_state_path(conn, project_id)
    # 先完成文件删除，再移除进程内状态。任何异常都会保留进程内状态，且
    # 侧车若仍存在也会继续阻断新连接。
    try:
        path.unlink(missing_ok=True)
    except TypeError:  # Python 3.8 兼容分支
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    with _FAIL_CLOSED_LOCK:
        _FAIL_CLOSED_STATES.pop(_state_key(conn, project_id), None)


def current_results_available(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    allow_state_clear: bool = True,
) -> dict[str, Any]:
    """返回所有当前成果读取/导出共用的运行级可用性。"""
    state = get_fail_closed_state(conn, project_id)
    if (
        allow_state_clear
        and state is not None
        and _pending_clear_is_committed(conn, project_id, state)
    ):
        pending = state.get(_PENDING_CLEAR_KEY) or {}
        try:
            clear_fail_closed_state(
                conn,
                project_id,
                run_signature=pending.get("run_signature"),
                coverage_run_id=pending.get("coverage_run_id"),
                coverage_run_kind=pending.get("coverage_run_kind"),
            )
        except Exception:
            # 清除失败时维持不可用边界；读取接口不能把清除异常误降级为可用。
            state = get_fail_closed_state(conn, project_id) or state
        else:
            state = None
    if state is None:
        current = get_current_contract(conn, project_id)
        try:
            contract_gaps = current_contract_gaps(conn, project_id, current)
        except Exception as error:
            # 路径损坏、符号链接循环或其他文件系统异常都必须进入统一的
            # unavailable 状态；读取/导出层不能把异常当作“没有当前结果限制”。
            contract_gaps = [
                "当前 Run Contract 闸门检查异常：" + type(error).__name__
            ]
        if contract_gaps:
            # 这是输入证据闸门产生的运行级不可用，不等同于数据库侧车；
            # 不在这里写侧车，避免只读状态查询产生副作用，但所有读取/导出
            # 调用仍必须看到 fail-closed 结果和具体副本缺口。
            return {
                "available": False,
                "status": FAIL_CLOSED_STATUS,
                "fail_closed": True,
                "reason": "当前 Run Contract 证据闸门未通过：" + "；".join(contract_gaps),
                "run_signature": current.signature if current else None,
                "run_id": current.run_id if current else None,
                "persisted": None,
                "persistence": None,
                "physical_limitations": [],
                "state": None,
            }
        return {
            "available": True,
            "status": "available",
            "fail_closed": False,
            "reason": None,
            "run_signature": current.signature if current else None,
            "run_id": current.run_id if current else None,
            "persisted": None,
            "persistence": None,
            "physical_limitations": [],
            "state": None,
        }
    return {
        "available": False,
        "status": FAIL_CLOSED_STATUS,
        "fail_closed": True,
        "reason": state.get("reason"),
        "run_signature": state.get("run_signature") or current_run_signature(conn, project_id),
        "run_id": (get_current_contract(conn, project_id).run_id
                   if get_current_contract(conn, project_id) else None),
        "persisted": state.get("persisted", False),
        "persistence": state.get("persistence"),
        "physical_limitations": list(state.get("physical_limitations") or []),
        "state": state,
    }


def require_current_results_available(
    conn: sqlite3.Connection, project_id: int, *, operation: str = "当前操作"
) -> dict[str, Any]:
    """在读取当前成果或登记导出前执行统一 fail-closed 门控。"""
    availability = current_results_available(conn, project_id)
    if not availability["available"]:
        reason = availability.get("reason") or "运行级不可用边界已生效"
        prefix = "数据库不可写，" if availability.get("state") is not None else ""
        raise CurrentResultsUnavailableError(
            f"{operation}不可用：{prefix}当前结果不可用；{reason}",
            state=availability.get("state"),
        )
    return availability


@contextmanager
def _transaction(conn: sqlite3.Connection, name: str = "run_contract") -> Iterator[None]:
    """兼容 autocommit 和调用方已有事务的可回滚事务。"""
    savepoint = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if conn.in_transaction:
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return
    conn.execute("BEGIN")
    try:
        yield
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    else:
        try:
            # 使用 Connection API 而不是反复执行同一条 SQL。sqlite3 的
            # 语句缓存可能让后续 ``execute('COMMIT')`` 不再次经过
            # authorizer；连接 API 会对每次外层提交执行真实提交检查。
            conn.commit()
        except Exception as exc:
            # 保留原始异常类型和消息，同时给需要安全重放完整事务的调用方
            # 一个不依赖异常文本的内部判别信号。事务体内的 DML 异常不会
            # 经过这里，因此不会被误当作一次性 COMMIT 故障。
            try:
                setattr(exc, _TRANSACTION_COMMIT_FAILURE_ATTR, True)
            except (AttributeError, TypeError):
                pass
            # SQLite 在 authorizer/驱动拒绝 COMMIT 时可能仍保持事务状态。
            # 先清理连接，再把提交异常原样交给调用方；否则同一连接会继续
            # 看到未提交的 Evidence、汇总或校核结果。
            if conn.in_transaction:
                try:
                    conn.rollback()
                except Exception as rollback_exc:
                    raise exc from rollback_exc
            raise


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _strict_json_list(value: Any, *, allow_empty: bool) -> tuple[str, ...] | None:
    """读取 coverage proof 的字符串列表，不把坏 JSON 降级为空列表。"""
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, list) or (not allow_empty and not decoded):
        return None
    if any(not isinstance(item, str) or not item.strip() for item in decoded):
        return None
    return tuple(decoded)


def _strict_json_mapping(value: Any) -> dict[str, Any] | None:
    """读取 coverage proof 的对象字段；类型错误必须使证明失效。"""
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    if any(not isinstance(key, str) for key in decoded):
        return None
    return decoded


def _app_version() -> str:
    """读取统一的运行时版本入口，避免 Run Contract 自行解析版本。"""
    return app_version()


def _source_files(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    verify_stored_files: bool = True,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, original_path, original_name, sha256, size_bytes, file_type,
                         stored_path, imported_at
           FROM source_files WHERE project_id=? ORDER BY id""",
        (project_id,),
    ).fetchall()
    result = []
    for row in rows:
        stored_path = _normalized_stored_path(row["stored_path"])
        stored = Path(stored_path)
        size_bytes = _strict_size_bytes(row["size_bytes"])
        if size_bytes is None:
            raise RuntimeError(
                f"源文件登记文件大小无效 file_id={int(row['id'])}"
            )
        actual_sha = None
        if verify_stored_files and stored.is_file():
            try:
                actual_sha = sha256_file(stored)
            except OSError:
                actual_sha = None
        result.append({
            "file_id": int(row["id"]),
            "original_path": row["original_path"],
            "original_name": row["original_name"],
            "sha256": row["sha256"],
            "stored_sha256": actual_sha,
            "stored_path": stored_path,
            "size_bytes": size_bytes,
            "file_type": row["file_type"],
            "imported_at": row["imported_at"],
        })
    return result


def source_file_contract_gaps(
    conn: sqlite3.Connection,
    project_id: int,
    contract: RunContract | None,
    *,
    allow_unbound_sources: bool = False,
) -> list[str]:
    """检查当前 Run Contract 已登记源文件的不可变身份。

    ``source_files`` 是原始证据链的根。新导入文件会使运行契约产生新签名，
    但已经被旧契约引用的文件不能通过直接 SQL 改名、改 SHA、改大小或挪到
    另一个项目后继续复用旧成果。该读取层检查与 v43 数据库触发器互补，
    既保护新迁移库，也能在旧库/外部脚本绕过触发器时 fail-closed。

    ``allow_unbound_sources`` 只供受控的 ``ensure_run_contract`` 入口使用：
    新导入源文件应促成新合同，不应被“当前读取层”误报为旧身份篡改。
    摘要、导出和其它只读路径保持默认严格模式，将未纳入合同的新文件视为
    当前输入边界缺口；已登记文件的删除或身份变化无论在哪个模式都拒绝。

    ``stored_path`` 是解析器实际读取的受控副本路径，必须与契约一致；对新
    建契约还会在每次当前读取时重新计算副本 SHA-256，发现重定向、覆盖或
    缺失即 fail-closed。哈希按块读取，避免把大文件整体载入内存；即使成本
    较高也不能为了性能跳过原始证据根的完整性校验。
    """
    rows = conn.execute(
        """SELECT id, project_id, original_path, original_name, sha256,
                         size_bytes, file_type, stored_path, imported_at
             FROM source_files WHERE project_id=? ORDER BY id""",
        (int(project_id),),
    ).fetchall()

    if contract is None:
        # 初次建立合同前也必须验证已经登记的源文件副本。空项目没有输入
        # 边界，可以继续创建空合同；一旦存在 source_files，缺失、目录、空
        # 路径、非法路径或无法读取的副本都必须阻断合同建立，避免出现带
        # ``stored_sha256=None`` 的“当前”合同。
        gaps: list[str] = []
        for row in rows:
            file_id = int(row["id"])
            actual_path, path_error = _safe_normalized_stored_path(row["stored_path"])
            if path_error:
                gaps.append(
                    f"源文件存储副本路径无效（{_stored_path_error_text(path_error)}）"
                    f" file_id={file_id}"
                )
            if not actual_path:
                gaps.append(f"源文件存储副本不存在或不可读取 file_id={file_id}")
                continue
            stored = Path(actual_path)
            try:
                is_file = stored.is_file()
            except (OSError, ValueError, RuntimeError):
                is_file = False
            if not is_file:
                gaps.append(f"源文件存储副本不存在或不可读取 file_id={file_id}")
                continue
            try:
                actual_sha = sha256_file(stored)
                registered_sha = row["sha256"]
                if (
                    not isinstance(registered_sha, str)
                    or not _SHA256_RE.fullmatch(registered_sha)
                ):
                    gaps.append(
                        f"源文件登记 SHA-256 格式无效 file_id={file_id}"
                    )
                elif actual_sha != registered_sha.lower():
                    gaps.append(
                        f"源文件登记 SHA-256 与存储副本内容不一致 file_id={file_id}"
                    )
                registered_size = _strict_size_bytes(row["size_bytes"])
                if registered_size is None:
                    gaps.append(
                        f"源文件登记文件大小无效 file_id={file_id}"
                    )
                else:
                    try:
                        actual_size = stored.stat().st_size
                    except (OSError, ValueError, RuntimeError):
                        gaps.append(
                            f"源文件存储副本大小不可读取 file_id={file_id}"
                        )
                    else:
                        if registered_size != actual_size:
                            gaps.append(
                                f"源文件登记文件大小与存储副本不一致 file_id={file_id}"
                            )
            except (OSError, ValueError, RuntimeError):
                gaps.append(f"源文件存储副本不存在或不可读取 file_id={file_id}")
        return gaps

    components = contract.components if isinstance(contract.components, dict) else {}
    expected_files = components.get("source_files")
    if not isinstance(expected_files, list):
        return ["Run Contract 缺少 source_files 文件清单"]

    actual_by_id = {int(row["id"]): row for row in rows}
    gaps: list[str] = []
    seen_ids: set[int] = set()
    identity_fields = (
        "original_path", "original_name", "sha256", "size_bytes",
        "file_type", "imported_at",
    )
    for expected in expected_files:
        if not isinstance(expected, dict):
            gaps.append("Run Contract source_files 清单项不是对象")
            continue
        try:
            file_id = int(expected["file_id"])
        except (KeyError, TypeError, ValueError):
            gaps.append("Run Contract source_files 缺少合法 file_id")
            continue
        if file_id in seen_ids:
            gaps.append(f"Run Contract source_files 重复 file_id={file_id}")
            continue
        seen_ids.add(file_id)
        row = actual_by_id.get(file_id)
        if row is None:
            gaps.append(f"Run Contract 引用的源文件不存在 file_id={file_id}")
            continue
        if int(row["project_id"]) != int(project_id):
            gaps.append(f"源文件项目归属不一致 file_id={file_id}")
            continue
        for field in identity_fields:
            # 旧契约可能没有新增的可选字段；对已登记字段严格比较，避免
            # 升级旧库时凭空制造无法恢复的历史差异。
            if field not in expected:
                continue
            actual = row[field]
            wanted = expected[field]
            if field == "size_bytes":
                actual_size = _strict_size_bytes(actual)
                wanted_size = _strict_size_bytes(wanted)
                if actual_size is None or wanted_size is None:
                    gaps.append(
                        f"源文件身份中的文件大小无效 file_id={file_id}"
                    )
                    continue
                actual = actual_size
                wanted = wanted_size
            if actual != wanted:
                gaps.append(
                    f"源文件身份与当前 Run Contract 不一致 file_id={file_id} field={field}"
                )
        actual_path, actual_path_error = _safe_normalized_stored_path(row["stored_path"])
        if actual_path_error:
            gaps.append(
                f"源文件存储副本路径无效（{_stored_path_error_text(actual_path_error)}）"
                f" file_id={file_id}"
            )
        expected_path = expected.get("stored_path")
        if expected_path is None:
            # v0.1.10 以前的合同未绑定解析器实际读取的副本路径；旧结果不能
            # 继续作为当前证据使用。受控 ensure_run_contract 会在新合同写入
            # 完整路径后恢复读取，但摘要/导出读取面必须先 fail-closed。
            gaps.append(
                f"Run Contract source_files 缺少存储副本路径 file_id={file_id}"
            )
        else:
            wanted_path, expected_path_error = _safe_normalized_stored_path(expected_path)
            if expected_path_error:
                gaps.append(
                    "Run Contract 存储副本路径无效（"
                    f"{_stored_path_error_text(expected_path_error)}）"
                    f" file_id={file_id}"
                )
            if not actual_path_error and not expected_path_error and actual_path != wanted_path:
                gaps.append(
                    f"源文件存储副本路径与当前 Run Contract 不一致 file_id={file_id}"
                )

        # ``stored_sha256`` 只会在创建契约时成功读取副本的情况下被记录。
        # 对这类新契约，当前读取必须重新哈希同一副本；不能仅比较 source_files
        # 表里的元数据，因为解析器实际消费的是 stored_path 指向的文件内容。
        expected_stored_sha = expected.get("stored_sha256")
        if not isinstance(expected_stored_sha, str) or not _SHA256_RE.fullmatch(
            expected_stored_sha
        ):
            gaps.append(
                f"Run Contract 缺少有效的存储副本 SHA-256 file_id={file_id}"
            )
        stored = Path(actual_path) if actual_path else None
        try:
            is_file = bool(stored and stored.is_file())
        except (OSError, ValueError, RuntimeError):
            is_file = False
        if not is_file:
            gaps.append(f"源文件存储副本不存在或不可读取 file_id={file_id}")
        else:
            try:
                actual_stored_sha = sha256_file(stored)
            except (OSError, ValueError, RuntimeError):
                gaps.append(f"源文件存储副本不存在或不可读取 file_id={file_id}")
            else:
                registered_sha = row["sha256"]
                if (
                    not isinstance(registered_sha, str)
                    or not _SHA256_RE.fullmatch(registered_sha)
                ):
                    gaps.append(
                        f"源文件登记 SHA-256 格式无效 file_id={file_id}"
                    )
                elif actual_stored_sha != registered_sha.lower():
                    gaps.append(
                        f"源文件登记 SHA-256 与存储副本内容不一致 file_id={file_id}"
                    )
                registered_size = _strict_size_bytes(row["size_bytes"])
                if registered_size is None:
                    gaps.append(
                        f"源文件登记文件大小无效 file_id={file_id}"
                    )
                else:
                    try:
                        actual_size = stored.stat().st_size
                    except (OSError, ValueError, RuntimeError):
                        gaps.append(
                            f"源文件存储副本大小不可读取 file_id={file_id}"
                        )
                    else:
                        if registered_size != actual_size:
                            gaps.append(
                                f"源文件登记文件大小与存储副本不一致 file_id={file_id}"
                            )
                if (
                    isinstance(expected_stored_sha, str)
                    and _SHA256_RE.fullmatch(expected_stored_sha)
                    and actual_stored_sha != expected_stored_sha
                ):
                    gaps.append(
                        f"源文件存储副本内容与当前 Run Contract 不一致 file_id={file_id}"
                    )
    if not allow_unbound_sources:
        for file_id in sorted(set(actual_by_id) - seen_ids):
            gaps.append(f"当前项目存在未纳入 Run Contract 的源文件 file_id={file_id}")
    return gaps


def line_item_contract_gaps(
    conn: sqlite3.Connection,
    project_id: int,
    contract: RunContract | None,
) -> list[str]:
    """检查当前清单明细是否仍与运行契约的数据指纹一致。

    line_items 是 A 路径的业务投影，虽然金额仍由 Decimal 计算，但它不是
    原始网格本身。外部脚本若在运行后插入、删除或改写一行，不能继续复用
    旧校核结果；当前摘要必须把该漂移作为证据缺口，而不是只拿覆盖证明
    的金额快照显示绿色。
    """
    if contract is None:
        return []
    components = contract.components if isinstance(contract.components, dict) else {}
    expected = components.get("data_fingerprint")
    if not isinstance(expected, dict):
        return ["Run Contract 缺少 line_items 数据指纹"]
    actual = _line_item_digest(conn, int(project_id))
    gaps: list[str] = []
    for field in ("line_item_count", "line_items_sha256"):
        if expected.get(field) != actual.get(field):
            gaps.append(f"line_items 数据指纹与当前 Run Contract 不一致 field={field}")
    return gaps


def mapping_contract_gaps(
    conn: sqlite3.Connection,
    project_id: int,
    contract: RunContract | None,
) -> list[str]:
    """检查当前表头/数据范围映射是否仍是运行契约中的快照。

    表头确认通过受控流程会删除旧行、写入新映射并重新形成 Run Contract；
    读取旧运行时若有人直接追加一条“最新表头”，必须先降级，不能让新的
    col_map 把原始金额列排除后仍沿用旧 A/B/C 结果。
    """
    if contract is None:
        return []
    components = contract.components if isinstance(contract.components, dict) else {}
    expected = components.get("mappings")
    if not isinstance(expected, list):
        return ["Run Contract 缺少 table_headers 映射快照"]
    actual = _mappings(conn, int(project_id))
    if len(actual) != len(expected):
        return ["table_headers 映射数量与当前 Run Contract 不一致"]
    gaps: list[str] = []
    for index, (wanted, current) in enumerate(zip(expected, actual, strict=True)):
        if wanted != current:
            header_id = current.get("header_id") if isinstance(current, dict) else None
            gaps.append(
                f"table_headers 映射与当前 Run Contract 不一致 index={index} header_id={header_id}"
            )
    return gaps


def period_contract_gaps(
    conn: sqlite3.Connection,
    project_id: int,
    contract: RunContract | None,
) -> list[str]:
    """检查期次业务元数据是否仍与当前 Run Contract 快照一致。

    期次标题、方向、合同方和计税口径都会影响 A/B/C 的业务解释。受控 API
    修改这些字段后会形成新合同；旧库或外部 SQL 绕过入口时，摘要必须先把
    旧结果降级，不能只因金额仍相等就继续显示当前充分。
    """
    if contract is None:
        return []
    components = contract.components if isinstance(contract.components, dict) else {}
    expected = components.get("periods")
    if not isinstance(expected, list):
        return ["Run Contract 缺少 settlement_periods 期次快照"]
    actual = _periods(conn, int(project_id))
    if len(actual) != len(expected):
        return ["settlement_periods 期次数量与当前 Run Contract 不一致"]
    gaps: list[str] = []
    for index, (wanted, current) in enumerate(zip(expected, actual, strict=True)):
        if wanted != current:
            period_id = current.get("id") if isinstance(current, dict) else None
            gaps.append(
                "settlement_periods 期次快照与当前 Run Contract 不一致 "
                f"index={index} period_id={period_id}"
            )
    return gaps


def sheet_scope_contract_gaps(
    conn: sqlite3.Connection,
    project_id: int,
    contract: RunContract | None,
) -> list[str]:
    """检查 Sheet/原始网格范围快照是否仍与当前 Run Contract 一致。

    ``raw_sheets`` 的工作表名、所属文件/期次、行列边界、隐藏/合并元数据和
    原始网格的轻量计数都属于取数范围事实。新库由 v42/v43 触发器保护；读取
    层还必须覆盖旧库或外部 SQL 绕过触发器的情况。
    """
    if contract is None:
        return []
    components = contract.components if isinstance(contract.components, dict) else {}
    expected = components.get("sheet_scope")
    if not isinstance(expected, list):
        return ["Run Contract 缺少 raw_sheets 范围快照"]
    actual = _sheet_scope(conn, int(project_id))
    if len(actual) != len(expected):
        return ["raw_sheets 工作表数量与当前 Run Contract 不一致"]
    gaps: list[str] = []
    for index, (wanted, current) in enumerate(zip(expected, actual, strict=True)):
        if wanted != current:
            sheet_id = current.get("sheet_id") if isinstance(current, dict) else None
            gaps.append(
                "raw_sheets 范围快照与当前 Run Contract 不一致 "
                f"index={index} sheet_id={sheet_id}"
            )
    return gaps


def contract_input_gaps(
    conn: sqlite3.Connection,
    project_id: int,
    contract: RunContract | None,
) -> list[str]:
    """复核 Run Contract 中尚未有专用读取闸门的项目输入快照。

    现有专用检查负责源文件身份、Sheet 范围、表头映射、期次和清单指纹；
    其余合同组成（项目版本、清洗决定、别名、规则目录、合同事实、权威
    清单和人工审计快照）也必须在外部 SQL/旧库绕过受控 API 时 fail-closed。
    这里使用与建合同相同的确定性序列化重新计算输入签名，并报告发生变化
    的顶层组成。受控 ``stored_path`` 的路径和文件内容由
    ``source_file_contract_gaps`` 专用闸门实时校验；本函数只比较其余输入
    组成，避免在同一次读取中重复扫描文件。
    """
    if contract is None:
        return []
    expected = contract.components if isinstance(contract.components, dict) else {}
    try:
        actual = build_run_contract_components(
            conn, int(project_id), verify_stored_files=False
        )
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return [f"Run Contract 输入快照无法重算：{type(exc).__name__}"]

    def comparable(value: Any, *, key: str) -> Any:
        if key != "source_files" or not isinstance(value, list):
            return value
        # stored_sha256 是受控副本内容校验值；文件路径和内容由
        # source_file_contract_gaps 专门比较，不能让本函数的签名重算逻辑
        # 因为避免重复哈希而放松原始证据根闸门。
        return [
            {field: item[field] for field in item if field != "stored_sha256"}
            if isinstance(item, dict) else item
            for item in value
        ]

    gaps: list[str] = []
    keys = sorted(set(expected) | set(actual))
    for key in keys:
        if key in {"source_files", "sheet_scope", "mappings", "periods", "data_fingerprint"}:
            continue
        # 持久化合同已经经过 canonical_json：Decimal 会变成字符串，
        # set/frozenset 会变成有序列表。直接比较 Python 对象会把同一规则
        # 快照误判为变化，进而令所有当前 Evidence 失效。用与签名生成完全
        # 相同的规范化序列比较语义值；真实字段变化仍会产生不同 JSON。
        expected_value = canonical_json(comparable(expected.get(key), key=key))
        actual_value = canonical_json(comparable(actual.get(key), key=key))
        if expected_value != actual_value:
            gaps.append(f"Run Contract 输入组成发生变化 field={key}")
    return gaps


def current_contract_gaps(
    conn: sqlite3.Connection,
    project_id: int,
    contract: RunContract | None,
) -> list[str]:
    """集中检查所有会改变当前结算解释的运行契约组成。

    只读摘要、匹配、异常和导出共用这条入口；不能只检查源文件副本，而
    放过期次、字段映射、明细投影、Sheet 范围或规则输入的直接漂移。函数
    不写数据库，也不调用 ``current_scope``，避免读取闸门递归。
    """
    checks = (
        lambda: source_file_contract_gaps(conn, project_id, contract),
        lambda: line_item_contract_gaps(conn, project_id, contract),
        lambda: mapping_contract_gaps(conn, project_id, contract),
        lambda: period_contract_gaps(conn, project_id, contract),
        lambda: sheet_scope_contract_gaps(conn, project_id, contract),
        lambda: contract_input_gaps(conn, project_id, contract),
    )
    gaps: list[str] = []
    for check in checks:
        gaps.extend(check())
    return list(dict.fromkeys(gaps))


def _sheet_scope(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    # F-3 性能修复：摘要计算委托 engine.sheet_digest（缓存感知，算法唯一），
    # 同一次构建内再作记忆化避免重复扫描。
    _cell_digest_memo: dict[int, str] = {}

    def raw_cell_digest(sheet_id: int) -> str:
        if sheet_id in _cell_digest_memo:
            return _cell_digest_memo[sheet_id]
        value = sheet_cell_digest(conn, int(sheet_id))
        _cell_digest_memo[sheet_id] = value
        return value

    rows = conn.execute(
        """SELECT rs.id, rs.batch_id, rs.sheet_index, rs.sheet_name, rs.period_id,
                  rs.n_rows, rs.n_cols, rs.merged_ranges_json,
                  rs.hidden_rows_json, rs.hidden_cols_json,
                  rs.sheet_status, rs.sheet_status_reason,
                  rs.sheet_status_updated_at, rs.sheet_status_actor,
                  rs.list_kind,
                  pb.file_id, pb.parser, pb.parsed_at, pb.status AS batch_status,
                  pb.stats_json AS batch_stats_json, sf.sha256 AS file_sha256
           FROM raw_sheets rs
           JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE sf.project_id=? ORDER BY rs.id""",
        (project_id,),
    ).fetchall()
    result = []
    for row in rows:
        raw_cell_meta = conn.execute(
            """SELECT COUNT(*) AS cell_count, MAX(row) AS max_row, MAX(col) AS max_col
               FROM raw_cells WHERE sheet_id=?""",
            (row["id"],),
        ).fetchone()
        result.append({
            "sheet_id": int(row["id"]),
            "batch_id": int(row["batch_id"]),
            "file_id": int(row["file_id"]),
            "file_sha256": row["file_sha256"],
            "parser": row["parser"],
            "parsed_at": row["parsed_at"],
            "batch_status": row["batch_status"],
            "batch_stats": _loads(row["batch_stats_json"], {}),
            "sheet_index": int(row["sheet_index"]),
            "sheet_name": row["sheet_name"],
            "period_id": int(row["period_id"]) if row["period_id"] is not None else None,
            "sheet_status": row["sheet_status"],
            "sheet_status_reason": row["sheet_status_reason"],
            "sheet_status_updated_at": row["sheet_status_updated_at"],
            "sheet_status_actor": row["sheet_status_actor"],
            # B5：清单类型（人工角色标注）纳入合同范围。角色变更即签名变化，
            # 旧运行自动失效，旧结果不得伪装为当前分析。
            "list_kind": row["list_kind"],
            "n_rows": int(row["n_rows"]),
            "n_cols": int(row["n_cols"]),
            "merged_ranges": _loads(row["merged_ranges_json"], []),
            "hidden_rows": _loads(row["hidden_rows_json"], []),
            "hidden_cols": _loads(row["hidden_cols_json"], []),
            # 文件 SHA 是保真输入的主指纹；这些轻量网格边界元数据还可以捕获
            # 手工修复造成的截断/扩展，而无需为大表重复序列化全部 raw_cells。
            "raw_cell_count": int(raw_cell_meta["cell_count"] or 0),
            "raw_cell_max_row": raw_cell_meta["max_row"],
            "raw_cell_max_col": raw_cell_meta["max_col"],
            "raw_cell_sha256": raw_cell_digest(int(row["id"])),
        })
    return result


def _mappings(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT th.id, th.sheet_id, th.header_row_lo, th.header_row_hi,
                  th.col_map_json, th.confidence, th.needs_review,
                  th.data_row_start, th.data_row_end,
                  th.data_range_status, th.data_range_method,
                  th.data_range_evidence_json
           FROM table_headers th
           JOIN raw_sheets rs ON rs.id=th.sheet_id
           JOIN parse_batches pb ON pb.id=rs.batch_id
           JOIN source_files sf ON sf.id=pb.file_id
           WHERE sf.project_id=? ORDER BY th.id""",
        (project_id,),
    ).fetchall()
    return [
        {
            "header_id": int(row["id"]),
            "sheet_id": int(row["sheet_id"]),
            "header_row_lo": int(row["header_row_lo"]),
            "header_row_hi": int(row["header_row_hi"]),
            "col_map": _loads(row["col_map_json"], {}),
            "confidence": str(row["confidence"]),
            "needs_review": bool(row["needs_review"]),
            "data_row_start": row["data_row_start"],
            "data_row_end": row["data_row_end"],
            "data_range_status": row["data_range_status"],
            "data_range_method": row["data_range_method"],
            "data_range_evidence": _loads(row["data_range_evidence_json"], {}),
        }
        for row in rows
    ]


def _periods(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, period_no, title, source_file_id, direction,
                          contract_party, tax_mode, note
           FROM settlement_periods WHERE project_id=? ORDER BY id""",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _strip_derived_flags(value: Any) -> Any:
    flags = dict(value) if isinstance(value, dict) else {}
    # aggregate_project 只补充这些计算元数据；它们不是新的原始输入，不能
    # 让导出/校核过程中签名自我变化。
    for key in (
        "amount_source", "amount_status", "amount_check_status", "calculated_amount",
        "calculated_amount_source", "amount_check",
    ):
        flags.pop(key, None)
    return flags


def _line_item_digest(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT li.period_id, li.sheet_id, li.code, li.name, li.feature, li.unit,
                  li.quantity, li.unit_price, li.amount, li.tax_rate,
                  li.qty_evid, li.price_evid, li.amount_evid, li.flags_json
           FROM line_items li JOIN settlement_periods sp ON sp.id=li.period_id
           WHERE sp.project_id=? ORDER BY li.id""",
        (project_id,),
    )
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        record = {
            "period_id": row["period_id"],
            "sheet_id": row["sheet_id"],
            "code": row["code"],
            "name": row["name"],
            "feature": row["feature"],
            "unit": row["unit"],
            "quantity": row["quantity"],
            "unit_price": row["unit_price"],
            "amount": row["amount"],
            "tax_rate": row["tax_rate"],
            "qty_evid": _loads(row["qty_evid"], row["qty_evid"]),
            "price_evid": _loads(row["price_evid"], row["price_evid"]),
            "amount_evid": _loads(row["amount_evid"], row["amount_evid"]),
            "flags": _strip_derived_flags(_loads(row["flags_json"], {})),
        }
        digest.update(canonical_json(record).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return {"line_item_count": count, "line_items_sha256": digest.hexdigest()}


def _aliases(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT direction, canonical_key, alias_text, mapping_basis, confirmed_by, confirmed_at
           FROM item_aliases WHERE project_id=? ORDER BY id""",
        (project_id,),
    ).fetchall()
    legacy = [dict(row) for row in rows]
    # v27 知识库是新的权威别名快照；保留旧 item_aliases 仅为兼容历史项目，
    # 但新 active/revoked 版本必须进入合同指纹，撤销也会使旧运行退出 current。
    try:
        from jiadun.core.matching import knowledge

        current = knowledge.current_alias_snapshot(conn, project_id)
    except (ImportError, sqlite3.Error):
        current = []
    return [
        {"source": "item_aliases", **item}
        for item in legacy
    ] + [
        {"source": "alias_knowledge", **item}
        for item in current
    ]


def _contract_facts(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    """读取合同事实投影；被人工拒绝的事实不再进入运行契约。

    candidate/needs_review 事实仍随载荷输出并带状态标记——它们是候选，
    不是已确认合同事实；只有 confirmed 才能视为已确认。
    """
    rows = conn.execute(
        """SELECT cd.id AS doc_id, cd.doc_type, cd.title, cd.file_id,
                  cf.fact_key, cf.fact_value, cf.quote_text, cf.location, cf.confidence,
                  cf.review_status, cf.reviewed_at, cf.reviewed_by, cf.review_reason,
                  di.parse_status AS document_parse_status
           FROM contract_docs cd LEFT JOIN contract_facts cf ON cf.doc_id=cd.id
           LEFT JOIN document_intake di ON di.file_id=cd.file_id AND di.project_id=cd.project_id
           WHERE cd.project_id=?
             AND di.file_id IS NOT NULL
             AND di.category='upward_contract'
             AND di.parse_status='parsed'
           ORDER BY cd.id, cf.id""",
        (project_id,),
    ).fetchall()
    facts: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("fact_key") is None:
            continue
        status = item.get("review_status")
        if status == "rejected":
            continue
        item["review_status"] = status or "candidate"
        facts.append(item)
    return facts


def _fact_review_summary(facts: list[dict[str, Any]]) -> dict[str, int]:
    """确认情况汇总：候选不是已确认事实，汇总让界面和报告无需重算即可见。"""
    summary = {"confirmed": 0, "candidate": 0, "needs_review": 0, "rejected": 0}
    for fact in facts:
        summary[fact.get("review_status") or "candidate"] = (
            summary.get(fact.get("review_status") or "candidate", 0) + 1
        )
    return summary


def _manifest_scope(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """把当前权威应到清单纳入运行契约；没有清单时明确记录不可用。"""
    manifest = conn.execute(
        """SELECT * FROM import_manifests
           WHERE project_id=? ORDER BY declared_at DESC, id DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    if not manifest:
        return {"authoritative": False, "status": "not_available", "entries": []}
    entries = conn.execute(
        """SELECT logical_key, expected_name, expected_sha256, expected_period_no,
                  expected_direction, expected_sheet_name, required, received_file_id,
                  state, note, source_reference_json
           FROM import_manifest_entries WHERE manifest_id=? ORDER BY id""",
        (manifest["id"],),
    ).fetchall()
    return {
        "authoritative": True,
        "manifest_id": int(manifest["id"]),
        "manifest_key": manifest["manifest_key"],
        "source": manifest["source"],
        "declared_by": manifest["declared_by"],
        "declared_at": manifest["declared_at"],
        "control_hash": manifest["control_hash"],
        "version": manifest["version"],
        "status": manifest["status"],
        "manifest_note": manifest["note"],
        "entries": [
            {
                "logical_key": row["logical_key"],
                "expected_name": row["expected_name"],
                "expected_sha256": row["expected_sha256"],
                "expected_period_no": row["expected_period_no"],
                "expected_direction": row["expected_direction"],
                "expected_sheet_name": row["expected_sheet_name"],
                "required": bool(row["required"]),
                "received_file_id": row["received_file_id"],
                "state": row["state"],
                "note": row["note"],
                "source_reference": _loads(row["source_reference_json"], {}),
            }
            for row in entries
        ],
    }


def _rule_config(
    conn: sqlite3.Connection | None = None,
    project_id: int | None = None,
) -> dict[str, Any]:
    # 延迟导入避免 contracts 包初始化时与 anomalies.rules 的 Finding 导入形成环。
    from jiadun.core.anomalies import rules

    names = ("ROUND_TOL", "PRICE_CHANGE_PCT", "QTY_SPIKE_RATIO", "LARGE_INT_THRESHOLDS", "UNIT_ALIASES")
    values = {name: getattr(rules, name) for name in names if hasattr(rules, name)}
    values["rule_ids"] = [rule.__name__ for rule in rules.ALL_RULES]
    values["version"] = "anomaly-rules-v1"
    if conn is not None and project_id is not None:
        try:
            from jiadun.core.anomalies import catalog

            configured = catalog.rule_config_snapshot(conn, int(project_id))
        except sqlite3.Error:
            # 迁移中的极早期数据库可能尚未建立配置表；保留全启用
            # 默认快照，不能把规则配置缺失误报为规则全部关闭。
            values["catalog_version"] = "unavailable"
            values["enabled_rule_ids"] = list(values["rule_ids"])
            values["disabled_rule_ids"] = []
            values["configurations"] = []
        else:
            values["catalog_version"] = configured["version"]
            values["enabled_rule_ids"] = configured["enabled_rule_ids"]
            values["disabled_rule_ids"] = configured["disabled_rule_ids"]
            values["configurations"] = configured["configurations"]
    else:
        values["catalog_version"] = "unbound"
        values["enabled_rule_ids"] = list(values["rule_ids"])
        values["disabled_rule_ids"] = []
        values["configurations"] = []
    return values


def _cleaning_scope(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    """保存清洗提议/决定的完整快照，避免仅凭事件数量判断输入。"""
    rows = conn.execute(
        """SELECT event_key, subject_type, subject_id, field_name,
                  before_json, proposed_json, status, reason, actor, created_at,
                  decided_at, decided_by, decision_note, evidence_id, audit_id
           FROM cleaning_changes WHERE project_id=? ORDER BY id""",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _project_version_scope(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """把最新项目版本的事实快照纳入合同，但排除运行绑定字段。"""
    try:
        row = conn.execute(
            """SELECT id, version_no, version_kind, title, description,
                      source_manifest_id, snapshot_sha256, item_count,
                      created_by, created_at, reason, evidence_id, audit_id,
                      metadata_json
               FROM project_versions WHERE project_id=?
               ORDER BY version_no DESC, id DESC LIMIT 1""",
            (int(project_id),),
        ).fetchone()
    except sqlite3.Error:
        return {"status": "not_available", "version": None}
    if row is None:
        return {"status": "not_available", "version": None}
    return {
        "status": "available",
        "version": {
            "id": int(row["id"]),
            "version_no": int(row["version_no"]),
            "version_kind": row["version_kind"],
            "title": row["title"],
            "description": row["description"] or "",
            "source_manifest_id": row["source_manifest_id"],
            "snapshot_sha256": row["snapshot_sha256"],
            "item_count": int(row["item_count"] or 0),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "reason": row["reason"],
            "evidence_id": row["evidence_id"],
            "audit_id": row["audit_id"],
            "metadata": _loads(row["metadata_json"], {}),
        },
    }


def _human_confirmation_snapshot(
    conn: sqlite3.Connection, project_id: int
) -> list[dict[str, Any]]:
    """把影响当前结算解释的人工操作前后值、原因和时间纳入合同快照。

    历史资产关闭/采集只写长期资产，不改变当前结算口径，因此由专用的
    ``project_version``/历史资产 Evidence 链负责追溯，不重复触发当前结果
    的输入漂移。

    ``run_id``/``run_signature`` 是这条审计记录的绑定结果，不是人工操作
    本身的输入。若把绑定字段也纳入指纹，人工操作完成后为了绑定新合同
    又会改变合同指纹，造成“刚绑定即失效”的自引用循环。这里保留操作
    身份、前后值、原因和时间等责任证据，排除仅用于定位当前运行的绑定
    字段；绑定字段仍保留在 ``audit_log``，可由 Evidence/Audit 查询追溯。
    """
    rows = conn.execute(
        """SELECT ts, actor, action, target, before_json, after_json, reason
           FROM audit_log
          WHERE project_id=?
            AND action NOT IN ('close_project_for_history', 'collect_historical_prices')
          ORDER BY id""",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def build_run_contract_components(
    conn: sqlite3.Connection,
    project_id: int,
    config: dict[str, Any] | None = None,
    *,
    code_version: str | None = None,
    verify_stored_files: bool = True,
) -> dict[str, Any]:
    """构造可审阅的运行契约组成部分。"""
    schema_version = migrations.current_version(conn)
    aliases = _aliases(conn, project_id)
    manifest_scope = _manifest_scope(conn, project_id)
    facts_for_payload = _contract_facts(conn, project_id)
    return {
        "format_version": CONTRACT_FORMAT_VERSION,
        # 产品身份属于运行契约的一部分。品牌/发行包从 CostGuard 迁移为
        # Jiadun 后，即使代码版本仍为 0.1.8，旧运行也必须退出当前结果，
        # 由新程序重新形成可追溯的当前证据。
        "product_id": branding.PRODUCT_SLUG,
        "product_name": branding.PRODUCT_DISPLAY_NAME,
        "project_id": int(project_id),
        "schema_version": schema_version,
        "code_version": code_version or _app_version(),
        # 项目版本链尚未创建时显式记录不可用；创建版本后只纳入最新
        # 版本的事实快照，运行绑定字段不参与指纹。
        "project_version": _project_version_scope(conn, project_id),
        "source_files": _source_files(
            conn, project_id, verify_stored_files=verify_stored_files
        ),
        "sheet_scope": _sheet_scope(conn, project_id),
        "mappings": _mappings(conn, project_id),
        "periods": _periods(conn, project_id),
        "aliases": aliases,
        "alias_library_version": stable_fingerprint(aliases),
        "human_confirmation_snapshot": _human_confirmation_snapshot(conn, project_id),
        "cleaning_rule_version": "cleaning-v1",
        "cleaning_changes": _cleaning_scope(conn, project_id),
        "matching_rule_version": "matching-v1",
        "matching_rule_config": {
            "composite_key_version": "composite-key-v1",
            "similarity_confirmed": "97.0",
            "similarity_suspected": "85.0",
        },
        "anomaly_rule_version": "anomaly-rules-v1",
        "selected_rule_ids": _rule_config(conn, project_id)["enabled_rule_ids"],
        "decimal_precision": int(getcontext().prec),
        "decimal_rounding": str(ROUND_HALF_UP),
        "decimal_scale": "0.01",
        # 当前项目模型没有可靠的金额单位字段；unknown 必须触发后续人工
        # 口径确认，不能从项目名、税率或文件名推断人民币/元。
        "amount_unit": "unknown",
        "contract_facts": facts_for_payload,
        "contract_fact_review_summary": _fact_review_summary(facts_for_payload),
        "import_manifest": manifest_scope,
        # P0-03 要求合同明确绑定权威清单的状态快照；保留旧键供兼容读取，
        # 新键让审阅者无需从组件名称猜测其语义。
        "manifest_state_snapshot": manifest_scope,
        "data_fingerprint": _line_item_digest(conn, project_id),
        "rules": _rule_config(conn, project_id),
        "config": config or {},
    }


def compute_run_signature(components: dict[str, Any]) -> str:
    return stable_fingerprint(components)


@dataclass(frozen=True)
class RunContract:
    contract_id: int
    run_id: str
    project_id: int
    signature: str
    components: dict[str, Any]
    created_at: str
    invalidated_at: str | None = None

    @property
    def run_signature(self) -> str:
        return self.signature


def _row_to_contract(row: sqlite3.Row) -> RunContract:
    return RunContract(
        contract_id=int(row["id"]),
        run_id=row["run_id"],
        project_id=int(row["project_id"]),
        signature=row["signature"],
        components=_loads(row["components_json"], {}),
        created_at=row["created_at"],
        invalidated_at=row["invalidated_at"],
    )


def get_current_contract(conn: sqlite3.Connection, project_id: int) -> RunContract | None:
    row = conn.execute(
        """SELECT id, run_id, project_id, signature, components_json, created_at, invalidated_at
           FROM run_contracts WHERE project_id=? AND invalidated_at IS NULL
           ORDER BY id DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    return _row_to_contract(row) if row else None


def has_materialized_contract(conn: sqlite3.Connection, project_id: int) -> bool:
    return bool(conn.execute("SELECT 1 FROM run_contracts WHERE project_id=? LIMIT 1", (project_id,)).fetchone())


def current_run_signature(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    ensure: bool = False,
    config: dict[str, Any] | None = None,
) -> str | None:
    if ensure:
        return ensure_run_contract(conn, project_id, config=config).signature
    current = get_current_contract(conn, project_id)
    return current.signature if current else None


def ensure_run_contract(
    conn: sqlite3.Connection,
    project_id: int,
    config: dict[str, Any] | None = None,
    *,
    code_version: str | None = None,
) -> RunContract:
    """创建或切换当前运行契约，并自动使旧导出失效。"""
    current = get_current_contract(conn, project_id)
    # 运行契约切换前先检查已引用的源文件身份。若外部脚本直接改写了
    # source_files，不能把被篡改后的元数据当成“新输入”创建绿色新合同；
    # 让上层入口进入 fail-closed，并保留原始合同和审计现场。
    identity_gaps = source_file_contract_gaps(
        conn, project_id, current, allow_unbound_sources=True
    )
    # 受控的项目搬迁只会改变副本路径；路径本身是合同组成的一部分，因而
    # 必须让旧合同退出 current 并新建 run，但不应阻断用户打开已搬迁项目。
    # 内容缺失/内容漂移/元数据篡改仍是不可安全自动恢复的阻断项，必须保留
    # fail-closed，不能把被替换的文件当成一次正常搬迁。
    if current is None:
        # 初次建合同没有可供“搬迁重绑”的旧运行；任何源文件副本缺口都必须
        # 阻断创建，只有空项目或所有副本均可读时才允许形成当前合同。
        blocking_identity_gaps = identity_gaps
    else:
        blocking_identity_gaps = [
            gap
            for gap in identity_gaps
            if "存储副本路径与当前 Run Contract 不一致" not in gap
            and "Run Contract source_files 缺少存储副本路径" not in gap
        ]
    if blocking_identity_gaps:
        raise RuntimeError(
            "当前 Run Contract 源文件身份不一致："
            + "; ".join(blocking_identity_gaps)
        )
    components = build_run_contract_components(
        conn, project_id, config=config, code_version=code_version
    )
    signature = compute_run_signature(components)
    if current and current.signature == signature:
        return current

    now = _now()
    with _transaction(conn):
        if current:
            conn.execute(
                "UPDATE run_contracts SET invalidated_at=? WHERE project_id=? AND invalidated_at IS NULL",
                (now, project_id),
            )
        # 同一项目即使恢复到历史相同签名，也必须创建新的运行合同。签名用于
        # 结果范围隔离，run_id 才是本次不可变运行身份；不得重新激活或改写旧行。
        run_id = f"run-{uuid.uuid4().hex}"
        cur = conn.execute(
            """INSERT INTO run_contracts(
                   run_id, project_id, signature, components_json, created_at, invalidated_at
               ) VALUES (?,?,?,?,?,NULL)""",
            (run_id, project_id, signature, canonical_json(components), now),
        )
        row = conn.execute(
            """SELECT id, run_id, project_id, signature, components_json, created_at, invalidated_at
               FROM run_contracts WHERE id=?""",
            (cur.lastrowid,),
        ).fetchone()
        # export_runs 是用户可见成果登记，必须明确变成 stale，而不是删除文件或
        # 用新签名覆盖旧记录。
        conn.execute(
            """UPDATE export_runs SET status='stale'
               WHERE project_id=? AND status='current'
                 AND (run_signature IS NULL OR run_signature<>?)""",
            (project_id, signature),
        )
    refreshed = conn.execute(
        """SELECT id, run_id, project_id, signature, components_json, created_at, invalidated_at
           FROM run_contracts WHERE id=?""",
        (row["id"],),
    ).fetchone()
    return _row_to_contract(refreshed)


def ensure_if_materialized(
    conn: sqlite3.Connection,
    project_id: int,
    config: dict[str, Any] | None = None,
) -> RunContract | None:
    """仅在项目已经运行过计算时刷新契约，避免首次导入阻断旧版 UI 测试/流程。"""
    if not has_materialized_contract(conn, project_id):
        return None
    return ensure_run_contract(conn, project_id, config=config)


def adopt_unsigned_records(conn: sqlite3.Connection, project_id: int, signature: str) -> int:
    """把 v8/v9 之后由兼容外部入口新写入的 NULL 结果绑定到当前契约。

    v9 迁移已把真正旧记录改为 ``legacy:stale``，所以这里处理的 NULL 只
    是没有经过新 API 的即时写入（例如插件/旧脚本补充的一条审核问题）。
    不覆盖已有非 NULL 签名，也不改动历史失效记录。
    """
    signature = str(signature or "").strip()
    if not signature:
        raise ValueError("兼容绑定必须提供非空 Run Contract 签名")
    changed = 0
    current = get_current_contract(conn, project_id)
    if current is None or current.signature != signature:
        # 不能把 NULL/NULL 兼容记录绑定到任意外部字符串；这会产生一个
        # 不属于当前运行的“伪身份”，后续 current_scope 既读不到又无法证明
        # 它为何被绑定。调用方必须先取得当前活动合同，再执行兼容收口。
        raise ValueError("兼容绑定签名不是当前活动 Run Contract，拒绝绑定")
    run_id = current.run_id
    with _transaction(conn, "adopt_unsigned_records"):
        for table in ("period_totals", "crosscheck_results", "matches", "anomalies"):
            cur = conn.execute(
                f"UPDATE {table} SET run_signature=?, run_id=? "
                "WHERE project_id=? AND run_signature IS NULL AND run_id IS NULL",
                (signature, run_id, project_id),
            )
            changed += int(cur.rowcount or 0)
    return changed


def current_scope(
    conn: sqlite3.Connection,
    project_id: int,
    table_alias: str,
) -> tuple[str, tuple[Any, ...]]:
    """返回当前成果表的 SQL 范围。

    未产生过运行契约的空/导入中项目只显示新写入的 NULL 签名兼容记录；
    v9 迁移已把真正旧结果标成 ``legacy:stale``。
    """
    # 运行级不可用边界优先于数据库中仍可能存在的旧成功行。除了持久化
    # 侧车，还要在只读路径实时检查源文件副本；否则副本内容漂移时摘要虽
    # 显示 unavailable，异常/匹配/证据详情仍可能通过旧签名取回历史结果。
    # ``allow_state_clear=False`` 保持 current_scope 纯读取，不在 SQL 查询
    # 中隐式删除恢复侧车。
    availability = current_results_available(
        conn, project_id, allow_state_clear=False
    )
    if not availability["available"]:
        # 契约输入发生漂移时，自动生成的成果必须完全退出当前读取面。
        # 但在尚未形成运行身份的兼容/人工入口中，仍可能存在没有
        # ``run_id``/``run_signature`` 的 Finding；这类记录不能被当作当前
        # 校核结果或绿色结论，但保留在问题中心可见，便于人工看到失败
        # 原因、补证并重新运行。持久化 fail-closed 侧车（state 非空）仍
        # 一律返回 1=0，避免旧结果在恢复边界中泄漏。
        if table_alias in {"a", "e", "ev"} and availability.get("state") is None:
            # 若当前合同已经产出过带运行身份的自动结果，输入漂移时必须
            # 把整个派生结果面收紧为 1=0；否则仅凭一个 NULL 身份 Finding
            # 的兼容放行会让旧结果与新问题混在一起。尚未产出任何派生
            # 结果时，才允许问题中心查看未绑定的人工 Finding/源证据。
            current = get_current_contract(conn, project_id)
            materialized = False
            if current is not None:
                for table in ("crosscheck_results", "period_totals", "matches", "anomalies"):
                    row = conn.execute(
                        f"SELECT 1 FROM {table} "
                        "WHERE project_id=? AND run_id=? AND run_signature=? LIMIT 1",
                        (project_id, current.run_id, current.signature),
                    ).fetchone()
                    if row is not None:
                        materialized = True
                        break
            if materialized:
                return "1=0", ()
            if table_alias in {"e", "ev"}:
                # 仅允许回溯没有运行身份的 source 证据；current/human 或
                # cross_check 证据仍必须等待新的完整运行。source 证据只是
                # 原始出处展示，不构成项目级校核结论。
                return (
                    f"{table_alias}.scope='source' AND {table_alias}.kind<>'cross_check' "
                    f"AND {table_alias}.run_signature IS NULL AND {table_alias}.run_id IS NULL",
                    (),
                )
            return (
                "a.run_signature IS NULL AND a.run_id IS NULL "
                "AND COALESCE(a.status, 'open') NOT IN ('stale', 'historical') "
                "AND COALESCE(a.lifecycle_status, 'new') <> 'historical'",
                (),
            )
        return "1=0", ()
    current = get_current_contract(conn, project_id)
    signature = current.signature if current else None
    if signature:
        # v0.1.9 之后同一输入签名允许出现多个不可变运行合同；只按
        # signature 会把历史同签名结果重新带回 current。自动成果表现在
        # 以独立 run_id 作为主筛选键，缺失 run_id 的旧结果继续 fail-closed。
        current_run_id = current.run_id if current else None
        run_id_tables = {"cr", "pt", "m", "a"}
        if table_alias in {"e", "ev"}:
            alias = table_alias
            # source 证据没有运行身份，属于原始资料链，可在当前项目中继续
            # 回溯；current/human 证据必须同时命中当前 signature + run_id。
            # historical 明确排除，避免详情页把旧证据当作当前证据。
            return (
                f"(({alias}.scope='source' AND {alias}.kind<>'cross_check' AND ("
                f"({alias}.run_signature IS NULL AND {alias}.run_id IS NULL) OR "
                f"({alias}.run_signature=? AND {alias}.run_id=?))) OR "
                f"({alias}.scope IN ('current','human') AND "
                f"{alias}.run_signature=? AND {alias}.run_id=?))",
                (signature, current.run_id, signature, current.run_id),
            )
        if table_alias == "a":
            # anomalies 在早期/插件兼容入口中没有统一的 run_id；迁移 v9
            # 已把真正旧结果标为 legacy:stale，因此仅保留当前库中仍为
            # NULL/NULL 的新人工或外部待复核记录。自动检测路径始终写入
            # 当前 run_id，不得把缺失身份的旧自动结果当作当前成功结论。
            return (
                f"(({table_alias}.run_id=? AND {table_alias}.run_signature=? "
                f"AND {table_alias}.status NOT IN ('stale', 'historical') "
                f"AND COALESCE({table_alias}.lifecycle_status, 'new')<>'historical') OR "
                f"({table_alias}.run_id IS NULL AND {table_alias}.run_signature IS NULL "
                f"AND {table_alias}.status NOT IN ('stale', 'historical') "
                f"AND COALESCE({table_alias}.lifecycle_status, 'new')<>'historical'))",
                (current.run_id, signature),
            )
        identity_column = "run_id" if table_alias in run_id_tables else "run_signature"
        identity_value = current_run_id if identity_column == "run_id" else signature
        if table_alias == "cr":
            return (
                f"{table_alias}.run_id=? AND {table_alias}.run_signature=? AND "
                f"{table_alias}.status NOT IN ('invalidated', 'stale')",
                (current_run_id, signature),
            )
        if table_alias in {"pt", "m"}:
            return (
                f"{table_alias}.run_id=? AND {table_alias}.run_signature=?",
                (current_run_id, signature),
            )
        return f"{table_alias}.{identity_column}=?", (identity_value,)
    if table_alias in {"e", "ev"}:
        return (
            f"{table_alias}.scope='source' AND {table_alias}.kind<>'cross_check' "
            f"AND {table_alias}.run_signature IS NULL AND {table_alias}.run_id IS NULL",
            (),
        )
    # 没有任何活动 Run Contract 时，只能保留明确属于“尚未形成运行”的
    # 兼容新写入。旧迁移行、已失效的校核/匹配不能因 NULL/NULL 身份重新
    # 进入当前读取面；否则项目列表或摘要会把历史绿色状态当成当前结论。
    if table_alias == "a":
        return (
            "a.run_signature IS NULL AND a.run_id IS NULL "
            "AND COALESCE(a.status, 'open') NOT IN ('stale', 'historical') "
            "AND COALESCE(a.lifecycle_status, 'new') <> 'historical'",
            (),
        )
    if table_alias == "cr":
        return (
            "cr.run_signature IS NULL AND cr.run_id IS NULL "
            "AND COALESCE(cr.status, 'pending') NOT IN ('invalidated', 'stale')",
            (),
        )
    if table_alias == "pt":
        return (
            "pt.run_signature IS NULL AND pt.run_id IS NULL "
            "AND COALESCE(pt.cross_check_status, 'pending') NOT IN ('invalidated', 'stale')",
            (),
        )
    if table_alias == "m":
        return (
            "m.run_signature IS NULL AND m.run_id IS NULL "
            "AND COALESCE(m.status, 'pending') NOT IN ('historical', 'stale', 'invalidated')",
            (),
        )
    return f"{table_alias}.run_signature IS NULL AND {table_alias}.run_id IS NULL", ()


def register_export(
    conn: sqlite3.Connection,
    project_id: int,
    kind: str,
    path: Path,
    *,
    run_signature: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """登记一个已落盘的成果文件；同类旧文件只标记 stale，不删除。"""
    target = Path(path)
    # 先建立/切换并验证当前 Run Contract，再执行统一读取门控。特别是首次
    # 登记成果时，项目可能尚无合同；若先门控后建合同，缺失副本会被误当作
    # “没有限制”并写入 status='current' 的导出登记。
    active_contract = ensure_run_contract(conn, project_id)
    require_current_results_available(conn, project_id, operation="导出成果登记")
    # 签名是当前 Run Contract 的受控身份，不能让调用方把历史或伪造签名
    # 写成 status='current'。None 表示沿用当前合同；一旦显式传入，必须
    # 与当前活动合同严格相等，空字符串也不能通过 ``or`` 静默回退。
    if run_signature is not None and run_signature != active_contract.signature:
        raise ValueError("导出登记的 Run Contract 签名必须等于当前活动合同")
    signature = active_contract.signature
    run_id = active_contract.run_id
    file_sha = sha256_file(target) if target.is_file() else None
    now = _now()
    with _transaction(conn, "register_export"):
        locked_contract = get_current_contract(conn, project_id)
        if (
            locked_contract is None
            or locked_contract.run_id != active_contract.run_id
            or locked_contract.signature != active_contract.signature
        ):
            raise CurrentResultsUnavailableError(
                "导出登记不可用：当前 Run Contract 在登记期间发生变化"
            )
        # 前置门控与写入之间仍可能发生文件级变化；在同一写事务内再做
        # 一次只读源副本闸门复核，至少把普通外部覆盖/删除窗口收窄到最后
        # 一次检查与 SQLite INSERT 之间。真正需要对抗并发替换时仍应使用
        # 不可变输入快照或已打开的只读文件描述符，见发布限制说明。
        transaction_availability = current_results_available(
            conn, project_id, allow_state_clear=False
        )
        if not transaction_availability["available"]:
            raise CurrentResultsUnavailableError(
                "导出登记不可用：源文件存储副本在登记期间发生变化；"
                + str(transaction_availability.get("reason") or "当前结果不可用"),
                state=transaction_availability.get("state"),
            )
        conn.execute(
            """UPDATE export_runs SET status='stale'
               WHERE project_id=? AND kind=? AND status='current'""",
            (project_id, kind),
        )
        cur = conn.execute(
            """INSERT INTO export_runs(
                   project_id, kind, path, run_signature, file_sha256,
                   generated_at, status, metadata_json, run_id
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                project_id, kind, str(target), signature, file_sha, now,
                "current" if file_sha else "missing",
                canonical_json(metadata or {}),
                run_id,
            ),
        )
    return int(cur.lastrowid)


def export_status(
    conn: sqlite3.Connection,
    project_id: int,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """读取成果登记并重新核对签名和文件 SHA；不删除失效文件。"""
    params: list[Any] = [project_id]
    sql = "SELECT * FROM export_runs WHERE project_id=?"
    if kind is not None:
        sql += " AND kind=?"
        params.append(kind)
    sql += " ORDER BY generated_at DESC, id DESC"
    availability = current_results_available(conn, project_id)
    current_contract = get_current_contract(conn, project_id)
    signature = current_contract.signature if current_contract else None
    result = []
    for row in conn.execute(sql, params):
        status = row["status"]
        target = Path(row["path"])
        if not availability["available"]:
            status = FAIL_CLOSED_STATUS
        elif (
            not current_contract
            or row["run_signature"] != signature
            or row["run_id"] != current_contract.run_id
        ):
            status = "stale"
        elif not target.is_file():
            status = "missing"
        else:
            try:
                actual = sha256_file(target)
            except OSError:
                actual = None
            status = "current" if actual and actual == row["file_sha256"] else "changed"
        result.append({
            "id": int(row["id"]),
            "kind": row["kind"],
            "path": row["path"],
            "run_signature": row["run_signature"],
            "run_id": row["run_id"],
            "file_sha256": row["file_sha256"],
            "generated_at": row["generated_at"],
            "status": status,
            "metadata": _loads(row["metadata_json"], {}),
            "availability_status": availability["status"],
            "current_results_available": availability["available"],
            "availability_reason": availability.get("reason"),
        })
    return result


# 便于不同层按常见命名导入，避免把数据库字段名散落在 UI/导出代码中。
build_contract_components = build_run_contract_components
run_signature = current_run_signature
record_export_run = register_export
