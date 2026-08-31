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
import importlib.metadata
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from costguard.core.db import migrations
from costguard.core.evidence.finding import canonical_json, stable_fingerprint

LEGACY_STALE_SIGNATURE = "legacy:stale"
# 运行条件不变但本次重跑未形成可用结果时使用的非当前标记。它与旧库迁移
# 的 ``legacy:stale`` 分开，便于读取面和审计记录区分“历史旧数据”和“本次
# 校核尝试失效”。两者都不会匹配当前 Run Contract 签名。
INVALIDATED_RUN_SIGNATURE = "run:invalidated"
CONTRACT_FORMAT_VERSION = 1


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """按块计算文件 SHA-256；不把整个工作簿读入内存。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


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


def _app_version() -> str:
    """读取发布版本；开发树未安装时从仓库 pyproject.toml 读取。"""
    root = Path(__file__).resolve().parents[4]
    pyproject = root / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        text = ""
    match = re.search(r"(?m)^version\s*=\s*[\"']([^\"']+)[\"']", text)
    if match:
        return match.group(1)
    try:
        return importlib.metadata.version("costguard")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _source_files(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, original_name, sha256, size_bytes, file_type, stored_path
           FROM source_files WHERE project_id=? ORDER BY id""",
        (project_id,),
    ).fetchall()
    result = []
    for row in rows:
        stored = Path(row["stored_path"])
        actual_sha = None
        if stored.is_file():
            try:
                actual_sha = sha256_file(stored)
            except OSError:
                actual_sha = None
        result.append({
            "file_id": int(row["id"]),
            "original_name": row["original_name"],
            "sha256": row["sha256"],
            "stored_sha256": actual_sha,
            "size_bytes": int(row["size_bytes"]),
            "file_type": row["file_type"],
        })
    return result


def _sheet_scope(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT rs.id, rs.batch_id, rs.sheet_index, rs.sheet_name, rs.period_id,
                  rs.n_rows, rs.n_cols, rs.merged_ranges_json,
                  rs.hidden_rows_json, rs.hidden_cols_json,
                  pb.file_id, sf.sha256 AS file_sha256
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
            "sheet_index": int(row["sheet_index"]),
            "sheet_name": row["sheet_name"],
            "period_id": int(row["period_id"]) if row["period_id"] is not None else None,
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
        """SELECT id, period_no, title, direction, contract_party, tax_mode, note
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
    return [dict(row) for row in rows]


def _contract_facts(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT cd.id AS doc_id, cd.doc_type, cd.title, cd.file_id,
                  cf.fact_key, cf.fact_value, cf.quote_text, cf.location, cf.confidence
           FROM contract_docs cd LEFT JOIN contract_facts cf ON cf.doc_id=cd.id
           WHERE cd.project_id=? ORDER BY cd.id, cf.id""",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


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
                "source_reference": _loads(row["source_reference_json"], {}),
            }
            for row in entries
        ],
    }


def _rule_config() -> dict[str, Any]:
    # 延迟导入避免 contracts 包初始化时与 anomalies.rules 的 Finding 导入形成环。
    from costguard.core.anomalies import rules

    names = ("ROUND_TOL", "PRICE_CHANGE_PCT", "QTY_SPIKE_RATIO", "LARGE_INT_THRESHOLDS", "UNIT_ALIASES")
    values = {name: getattr(rules, name) for name in names if hasattr(rules, name)}
    values["rule_ids"] = [rule.__name__ for rule in rules.ALL_RULES]
    return values


def build_run_contract_components(
    conn: sqlite3.Connection,
    project_id: int,
    config: dict[str, Any] | None = None,
    *,
    code_version: str | None = None,
) -> dict[str, Any]:
    """构造可审阅的运行契约组成部分。"""
    schema_version = migrations.current_version(conn)
    return {
        "format_version": CONTRACT_FORMAT_VERSION,
        "project_id": int(project_id),
        "schema_version": schema_version,
        "code_version": code_version or _app_version(),
        "source_files": _source_files(conn, project_id),
        "sheet_scope": _sheet_scope(conn, project_id),
        "mappings": _mappings(conn, project_id),
        "periods": _periods(conn, project_id),
        "aliases": _aliases(conn, project_id),
        "contract_facts": _contract_facts(conn, project_id),
        "import_manifest": _manifest_scope(conn, project_id),
        "data_fingerprint": _line_item_digest(conn, project_id),
        "rules": _rule_config(),
        "config": config or {},
    }


def compute_run_signature(components: dict[str, Any]) -> str:
    return stable_fingerprint(components)


@dataclass(frozen=True)
class RunContract:
    contract_id: int
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
        project_id=int(row["project_id"]),
        signature=row["signature"],
        components=_loads(row["components_json"], {}),
        created_at=row["created_at"],
        invalidated_at=row["invalidated_at"],
    )


def get_current_contract(conn: sqlite3.Connection, project_id: int) -> RunContract | None:
    row = conn.execute(
        """SELECT id, project_id, signature, components_json, created_at, invalidated_at
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
    components = build_run_contract_components(
        conn, project_id, config=config, code_version=code_version
    )
    signature = compute_run_signature(components)
    current = get_current_contract(conn, project_id)
    if current and current.signature == signature:
        return current

    now = _now()
    with _transaction(conn):
        if current:
            conn.execute(
                "UPDATE run_contracts SET invalidated_at=? WHERE project_id=? AND invalidated_at IS NULL",
                (now, project_id),
            )
        row = conn.execute(
            "SELECT id, project_id, signature, components_json, created_at, invalidated_at "
            "FROM run_contracts WHERE project_id=? AND signature=?",
            (project_id, signature),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE run_contracts SET components_json=?, created_at=?, invalidated_at=NULL WHERE id=?",
                (canonical_json(components), now, row["id"]),
            )
        else:
            cur = conn.execute(
                """INSERT INTO run_contracts(
                       project_id, signature, components_json, created_at, invalidated_at
                   ) VALUES (?,?,?,?,NULL)""",
                (project_id, signature, canonical_json(components), now),
            )
            row = conn.execute(
                """SELECT id, project_id, signature, components_json, created_at, invalidated_at
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
        """SELECT id, project_id, signature, components_json, created_at, invalidated_at
           FROM run_contracts WHERE project_id=? AND signature=?""",
        (project_id, signature),
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
    changed = 0
    with _transaction(conn, "adopt_unsigned_records"):
        for table in ("period_totals", "crosscheck_results", "matches", "anomalies"):
            cur = conn.execute(
                f"UPDATE {table} SET run_signature=? WHERE project_id=? AND run_signature IS NULL",
                (signature, project_id),
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
    signature = current_run_signature(conn, project_id)
    if signature:
        if table_alias == "cr":
            return (
                f"{table_alias}.run_signature=? AND "
                f"{table_alias}.status NOT IN ('invalidated', 'stale')",
                (signature,),
            )
        return f"{table_alias}.run_signature=?", (signature,)
    return f"{table_alias}.run_signature IS NULL", ()


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
    signature = run_signature or current_run_signature(conn, project_id, ensure=True)
    file_sha = sha256_file(target) if target.is_file() else None
    now = _now()
    with _transaction(conn, "register_export"):
        conn.execute(
            """UPDATE export_runs SET status='stale'
               WHERE project_id=? AND kind=? AND status='current'""",
            (project_id, kind),
        )
        cur = conn.execute(
            """INSERT INTO export_runs(
                   project_id, kind, path, run_signature, file_sha256,
                   generated_at, status, metadata_json
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                project_id, kind, str(target), signature, file_sha, now,
                "current" if file_sha else "missing",
                canonical_json(metadata or {}),
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
    signature = current_run_signature(conn, project_id)
    result = []
    for row in conn.execute(sql, params):
        status = row["status"]
        target = Path(row["path"])
        if not signature or row["run_signature"] != signature:
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
            "file_sha256": row["file_sha256"],
            "generated_at": row["generated_at"],
            "status": status,
            "metadata": _loads(row["metadata_json"], {}),
        })
    return result


# 便于不同层按常见命名导入，避免把数据库字段名散落在 UI/导出代码中。
build_contract_components = build_run_contract_components
run_signature = current_run_signature
record_export_run = register_export
