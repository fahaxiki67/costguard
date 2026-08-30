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
    (
        # v2: 一个工作簿可含多期（每 Sheet 一期），raw_sheets 需要关联期次
        2,
        [
            "ALTER TABLE raw_sheets ADD COLUMN period_id INTEGER REFERENCES settlement_periods(id)",
        ],
    ),
    (
        # v3: 对上/对下各自拥有期号序列——唯一约束放宽为 (project_id, period_no, direction)。
        # SQLite 无法修改约束，按官方流程重建表（外键引用的表名与行 id 保持不变）。
        3,
        [
            """CREATE TABLE settlement_periods_new (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                period_no INTEGER NOT NULL,
                title TEXT NOT NULL,
                source_file_id INTEGER REFERENCES source_files(id),
                direction TEXT NOT NULL DEFAULT 'unknown',
                contract_party TEXT DEFAULT '',
                tax_mode TEXT DEFAULT 'unknown',
                note TEXT DEFAULT '',
                UNIQUE(project_id, period_no, direction)
            )""",
            """INSERT INTO settlement_periods_new
               (id, project_id, period_no, title, source_file_id, direction, contract_party, tax_mode, note)
               SELECT id, project_id, period_no, title, source_file_id, direction, contract_party, tax_mode, note
               FROM settlement_periods""",
            "DROP TABLE settlement_periods",
            "ALTER TABLE settlement_periods_new RENAME TO settlement_periods",
        ],
    ),
    (
        # v4: 人工确认多行表头时，必须持久化实际数据行边界。
        # 双路径 B 复算按同一人工确认的语义范围回读 raw_cells，避免把合计、签字、
        # 控制额等表外行误纳入明细。
        4,
        [
            "ALTER TABLE table_headers ADD COLUMN data_row_start INTEGER",
            "ALTER TABLE table_headers ADD COLUMN data_row_end INTEGER",
        ],
    ),
    (
        # v5: 别名按结算方向隔离；period_totals 明确保存 A/B、C 控制与金额来源。
        # 旧别名无法可靠推断方向，迁移为 unknown，只在未标记方向中继续生效；
        # 对上/对下需要按各自证据重新确认，禁止默认跨方向复用。
        5,
        [
            """CREATE TABLE item_aliases_new (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                direction TEXT NOT NULL DEFAULT 'unknown',
                canonical_key TEXT NOT NULL,
                alias_text TEXT NOT NULL,
                mapping_basis TEXT NOT NULL,
                confirmed_by TEXT DEFAULT 'user',
                confirmed_at TEXT NOT NULL,
                UNIQUE(project_id, direction, alias_text)
            )""",
            """INSERT INTO item_aliases_new
               (id, project_id, direction, canonical_key, alias_text, mapping_basis,
                confirmed_by, confirmed_at)
               SELECT id, project_id, 'unknown', canonical_key, alias_text, mapping_basis,
                      confirmed_by, confirmed_at
               FROM item_aliases""",
            "DROP TABLE item_aliases",
            "ALTER TABLE item_aliases_new RENAME TO item_aliases",
            "ALTER TABLE period_totals ADD COLUMN raw_amount_sum TEXT",
            "ALTER TABLE period_totals ADD COLUMN calculated_amount_sum TEXT",
            "ALTER TABLE period_totals ADD COLUMN calculated_amount_used_sum TEXT",
            "ALTER TABLE period_totals ADD COLUMN effective_amount_sum TEXT",
            "ALTER TABLE period_totals ADD COLUMN amount_source TEXT NOT NULL DEFAULT 'missing'",
            "ALTER TABLE period_totals ADD COLUMN amount_status TEXT NOT NULL DEFAULT 'missing'",
            "ALTER TABLE period_totals ADD COLUMN ab_diff TEXT",
            "ALTER TABLE period_totals ADD COLUMN ab_status TEXT NOT NULL DEFAULT 'pending'",
            "ALTER TABLE period_totals ADD COLUMN control_diff TEXT",
            "ALTER TABLE period_totals ADD COLUMN control_status TEXT NOT NULL DEFAULT 'not_available'",
        ],
    ),
    (
        # v6: 运行级校核结果与取数范围证据。
        # 旧 period_totals 是逐清单行的累计表，无法可靠承载“本期整体校核
        # 级别/参与行数/待确认数”，因此新增一张按 period_id 唯一的结果表。
        # table_headers 的范围状态用于区分人工明确确认、程序推断和历史未知值；
        # 历史 NULL/未知值默认不允许成为绿色校核依据。
        6,
        [
            "ALTER TABLE table_headers ADD COLUMN data_range_status TEXT NOT NULL DEFAULT 'unproven'",
            "ALTER TABLE table_headers ADD COLUMN data_range_method TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE table_headers ADD COLUMN data_range_evidence_json TEXT NOT NULL DEFAULT '{}'",
            """CREATE TABLE IF NOT EXISTS crosscheck_results (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                period_id INTEGER NOT NULL REFERENCES settlement_periods(id),
                verification_level TEXT NOT NULL DEFAULT 'insufficient',
                status TEXT NOT NULL DEFAULT 'pending',
                path_a_total TEXT,
                path_b_total TEXT,
                raw_subtotal TEXT,
                diff_ab TEXT,
                control_diff TEXT,
                ab_status TEXT NOT NULL DEFAULT 'pending',
                control_status TEXT NOT NULL DEFAULT 'not_available',
                detail_rows INTEGER NOT NULL DEFAULT 0,
                excluded_subtotal_rows INTEGER NOT NULL DEFAULT 0,
                excluded_title_rows INTEGER NOT NULL DEFAULT 0,
                pending_sheets INTEGER NOT NULL DEFAULT 0,
                range_unproven_sheets INTEGER NOT NULL DEFAULT 0,
                notes_json TEXT NOT NULL DEFAULT '[]',
                evidence_id INTEGER REFERENCES evidence(id),
                checked_at TEXT NOT NULL,
                UNIQUE(period_id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_crosscheck_results_project ON crosscheck_results(project_id, period_id)",
        ],
    ),
    (
        # v7: 将校核级别直接同步到旧的 period_totals 读取面，避免下游只看
        # cross_check_status='match' 而忽略“校核不充分”的运行级结论。
        7,
        [
            "ALTER TABLE period_totals ADD COLUMN verification_level TEXT NOT NULL DEFAULT 'insufficient'",
            # v6 及更早版本没有运行级校核结果。旧项目中的 ``match`` 只表示
            # 当时的二态字段，不足以证明取数范围、C 控制值和人工门控已经闭合；
            # 升级后必须保守降级，待重新执行校核再恢复为 ``match``。
            """UPDATE period_totals
               SET verification_level='insufficient',
                   cross_check_status=CASE
                       WHEN cross_check_status='match' THEN 'insufficient'
                       ELSE cross_check_status
                   END""",
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
    ver_probe = connect(db_path)  # 必须用带 Row factory 的连接读版本
    try:
        src_ver = current_version(ver_probe)
    finally:
        ver_probe.close()
    dest = backups_dir / f"project_pre_migration_v{src_ver}_{stamp}.db"
    # WAL 可能有未 checkpoint 数据；使用 backup API 获取一致快照，并确保句柄可关闭。
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def _sync_project_schema_versions(conn: sqlite3.Connection, version: int) -> None:
    """将所有项目行的冗余结构版本标记同步到实际迁移版本。"""
    has_projects = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()
    if has_projects:
        conn.execute("UPDATE projects SET schema_version = ?", (version,))


def migrate(db_path: Path, backups_dir: Path | None = None, log: Callable[[str], None] | None = None) -> int:
    """将库迁移到最新版本。返回最终版本号。

    - 迁移前自动备份；
    - 所有待执行版本在一个事务中提交；
    - 任一脚本失败则从迁移前备份恢复，抛 MigrationError。
    """
    db_path = Path(db_path)
    conn = connect(db_path)
    backup_path: Path | None = None
    try:
        ver = current_version(conn)
        target = LATEST_SCHEMA_VERSION
        if ver >= target:
            # 版本已是最新时仍修正项目表中的冗余版本标记。
            conn.execute("BEGIN")
            try:
                _sync_project_schema_versions(conn, target)
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            return ver
        backups_dir = backups_dir or db_path.parent / "backups"
        backup_path = _backup(db_path, backups_dir)
        if log:
            log(f"migration backup -> {backup_path}")

        applied_versions: list[int] = []
        try:
            # 表重建型迁移（v3）需要在事务外关闭外键；事务内 PRAGMA
            # foreign_keys 无效。整个迁移仍在一个事务中，避免留下 v2/v3 的部分提交。
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN")
            for version, scripts in MIGRATIONS:
                if version <= ver:
                    continue
                for sql in scripts:
                    conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, datetime.now().isoformat(timespec="seconds")),
                )
                applied_versions.append(version)

            _sync_project_schema_versions(conn, target)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise MigrationError(f"foreign key violations after migration: {violations[:5]}")
            conn.execute("COMMIT")

            # COMMIT 后才能重新开启外键；开启后再确认连接没有遗留在错误状态。
            conn.execute("PRAGMA foreign_keys=ON")
            if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise MigrationError("foreign keys could not be re-enabled after migration")
        except Exception as exc:
            cleanup_errors: list[str] = []
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error as rollback_exc:
                    cleanup_errors.append(f"transaction rollback failed: {rollback_exc}")
            try:
                conn.execute("PRAGMA foreign_keys=ON")
            except sqlite3.Error as foreign_key_exc:
                cleanup_errors.append(f"foreign keys restore failed: {foreign_key_exc}")

            # 备份恢复前必须关闭迁移连接，避免自身持有 WAL/读写句柄阻塞恢复。
            try:
                conn.close()
            except sqlite3.Error as close_exc:
                cleanup_errors.append(f"migration connection close failed: {close_exc}")

            try:
                rollback_to_backup(db_path, backup_path)
            except Exception as restore_exc:
                details = [
                    f"migration failed: {exc}",
                    f"backup restore failed: {restore_exc}",
                    f"backup kept at {backup_path}",
                    *cleanup_errors,
                ]
                raise MigrationError("; ".join(details)) from exc

            details = [
                f"migration failed: {exc}",
                f"database restored from backup {backup_path}",
                *cleanup_errors,
            ]
            raise MigrationError("; ".join(details)) from exc

        for version in applied_versions:
            if log:
                log(f"applied migration v{version}")
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
    try:
        dst = connect(db_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
