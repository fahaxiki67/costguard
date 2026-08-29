"""SQLite 连接与 Migration 管理。

纪律（ADR-003）：
- schema_version 管理库结构；Migration 前自动备份到 backups/；
- Migration 失败必须回滚，库保持迁移前状态。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

# 每个 migration = (version, 升级 SQL 列表)。只允许追加，不允许修改历史条目。
MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            # ---- 项目与文件登记 ----
            """CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                workspace_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS source_files (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                original_path TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                original_name TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                file_type TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                note TEXT DEFAULT '',
                UNIQUE(project_id, sha256)
            )""",
            # ---- 解析：保真层 ----
            """CREATE TABLE IF NOT EXISTS parse_batches (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES source_files(id),
                parser TEXT NOT NULL,
                parsed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                stats_json TEXT DEFAULT '{}'
            )""",
            """CREATE TABLE IF NOT EXISTS raw_sheets (
                id INTEGER PRIMARY KEY,
                batch_id INTEGER NOT NULL REFERENCES parse_batches(id),
                sheet_index INTEGER NOT NULL,
                sheet_name TEXT NOT NULL,
                n_rows INTEGER NOT NULL,
                n_cols INTEGER NOT NULL,
                merged_ranges_json TEXT DEFAULT '[]',
                hidden_rows_json TEXT DEFAULT '[]',
                hidden_cols_json TEXT DEFAULT '[]'
            )""",
            """CREATE TABLE IF NOT EXISTS raw_cells (
                sheet_id INTEGER NOT NULL REFERENCES raw_sheets(id),
                row INTEGER NOT NULL,
                col INTEGER NOT NULL,
                raw_value TEXT,
                cached_value TEXT,
                is_formula INTEGER NOT NULL DEFAULT 0,
                is_number_stored_as_text INTEGER NOT NULL DEFAULT 0,
                num_fmt TEXT DEFAULT '',
                PRIMARY KEY (sheet_id, row, col)
            )""",
            """CREATE TABLE IF NOT EXISTS table_headers (
                id INTEGER PRIMARY KEY,
                sheet_id INTEGER NOT NULL REFERENCES raw_sheets(id),
                header_row_lo INTEGER NOT NULL,
                header_row_hi INTEGER NOT NULL,
                col_map_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                needs_review INTEGER NOT NULL DEFAULT 0
            )""",
            # ---- 结算业务层 ----
            """CREATE TABLE IF NOT EXISTS settlement_periods (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                period_no INTEGER NOT NULL,
                title TEXT NOT NULL,
                source_file_id INTEGER REFERENCES source_files(id),
                direction TEXT NOT NULL DEFAULT 'unknown',
                contract_party TEXT DEFAULT '',
                tax_mode TEXT DEFAULT 'unknown',
                note TEXT DEFAULT '',
                UNIQUE(project_id, period_no)
            )""",
            """CREATE TABLE IF NOT EXISTS line_items (
                id INTEGER PRIMARY KEY,
                period_id INTEGER NOT NULL REFERENCES settlement_periods(id),
                sheet_id INTEGER REFERENCES raw_sheets(id),
                code TEXT DEFAULT '',
                name TEXT NOT NULL,
                feature TEXT DEFAULT '',
                unit TEXT DEFAULT '',
                quantity TEXT,
                unit_price TEXT,
                amount TEXT,
                tax_rate TEXT,
                qty_evid INTEGER,
                price_evid INTEGER,
                amount_evid INTEGER,
                flags_json TEXT DEFAULT '{}'
            )""",
            "CREATE INDEX IF NOT EXISTS idx_line_items_period ON line_items(period_id)",
            "CREATE INDEX IF NOT EXISTS idx_line_items_code ON line_items(period_id, code)",
            # ---- 别名/映射库 ----
            """CREATE TABLE IF NOT EXISTS item_aliases (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                canonical_key TEXT NOT NULL,
                alias_text TEXT NOT NULL,
                mapping_basis TEXT NOT NULL,
                confirmed_by TEXT DEFAULT 'user',
                confirmed_at TEXT NOT NULL,
                UNIQUE(project_id, alias_text)
            )""",
            # ---- 匹配 ----
            """CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                group_key TEXT NOT NULL,
                item_ids_json TEXT NOT NULL,
                level TEXT NOT NULL,
                method TEXT NOT NULL,
                score REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                reviewed_by TEXT,
                review_note TEXT
            )""",
            # ---- 汇总与校核 ----
            """CREATE TABLE IF NOT EXISTS period_totals (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                period_id INTEGER NOT NULL REFERENCES settlement_periods(id),
                item_key TEXT NOT NULL,
                qty_sum TEXT,
                amount_sum TEXT,
                wavg_price TEXT,
                cross_check_diff TEXT,
                cross_check_status TEXT DEFAULT 'pending',
                evidence_id INTEGER,
                UNIQUE(period_id, item_key)
            )""",
            # ---- 异常 ----
            """CREATE TABLE IF NOT EXISTS anomalies (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                rule_id TEXT NOT NULL,
                severity TEXT NOT NULL,
                subject_type TEXT NOT NULL,
                subject_id INTEGER NOT NULL,
                evidence_id INTEGER,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                resolved_note TEXT,
                created_at TEXT NOT NULL
            )""",
            # ---- 证据链与审计 ----
            """CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                kind TEXT NOT NULL,
                summary TEXT NOT NULL,
                steps_json TEXT NOT NULL DEFAULT '[]',
                sources_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                ts TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                reason TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(project_id, target)",
            # ---- 合同（Phase 6 扩展列亦可） ----
            """CREATE TABLE IF NOT EXISTS contract_docs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                file_id INTEGER REFERENCES source_files(id),
                doc_type TEXT NOT NULL DEFAULT 'unknown',
                title TEXT DEFAULT '',
                parsed_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS contract_facts (
                id INTEGER PRIMARY KEY,
                doc_id INTEGER NOT NULL REFERENCES contract_docs(id),
                fact_key TEXT NOT NULL,
                fact_value TEXT,
                quote_text TEXT NOT NULL,
                location TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                evidence_id INTEGER
            )""",
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
        ],
    ),
]

LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]


class MigrationError(RuntimeError):
    """迁移失败（库保持迁移前状态，备份保留在 backups/）。"""


def connect(db_path: Path) -> sqlite3.Connection:
    """打开项目库连接。WAL 模式；外键开启；显式事务管理（DDL 也可回滚）。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not has_table:
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return int(row["v"] or 0)


def _backup(db_path: Path, backups_dir: Path) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backups_dir / f"project_pre_migration_v{current_version(sqlite3.connect(db_path))}_{stamp}.db"
    # WAL 可能有未 checkpoint 数据，先关闭句柄再拷贝由调用方保证；这里用备份 API。
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    return dest


def migrate(db_path: Path, backups_dir: Path | None = None, log: Callable[[str], None] | None = None) -> int:
    """将库迁移到最新版本。返回最终版本号。

    - 迁移前自动备份；
    - 任一脚本失败则整体回滚，抛 MigrationError。
    """
    db_path = Path(db_path)
    conn = connect(db_path)
    try:
        ver = current_version(conn)
        target = LATEST_SCHEMA_VERSION
        if ver >= target:
            return ver
        backups_dir = backups_dir or db_path.parent / "backups"
        backup_path = _backup(db_path, backups_dir)
        if log:
            log(f"migration backup -> {backup_path}")
        try:
            for version, scripts in MIGRATIONS:
                if version <= ver:
                    continue
                conn.execute("BEGIN")
                try:
                    for sql in scripts:
                        conn.execute(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (version, datetime.now().isoformat(timespec="seconds")),
                    )
                    conn.execute("COMMIT")
                except sqlite3.Error:
                    conn.execute("ROLLBACK")
                    raise
                if log:
                    log(f"applied migration v{version}")
        except sqlite3.Error as exc:
            conn.close()
            raise MigrationError(f"migration failed: {exc}; backup kept at {backup_path}") from exc
        return current_version(conn)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def rollback_to_backup(db_path: Path, backup_path: Path) -> None:
    """迁移失败后从备份恢复当前库。

    用 SQLite backup API 把备份灌回目标库（WAL 一致性由 SQLite 保证），
    不做文件级覆盖——WAL 模式下文件覆盖会留下脏 -wal/-shm 导致 I/O 错误。
    要求目标库无活动连接（单机单实例）。
    """
    src = sqlite3.connect(backup_path)
    dst = connect(db_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
