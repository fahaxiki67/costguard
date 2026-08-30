"""Migration 纪律测试：版本管理、自动备份、失败回滚。"""
import sqlite3
from pathlib import Path

import pytest

from costguard.core.db import migrations


def _tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "project.db"


class TestMigrate:
    def test_fresh_db_to_latest(self, tmp_path):
        db = _tmp_db(tmp_path)
        v = migrations.migrate(db, tmp_path / "backups")
        assert v == migrations.LATEST_SCHEMA_VERSION
        conn = migrations.connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for expected in [
            "projects", "source_files", "parse_batches", "raw_sheets", "raw_cells",
            "table_headers", "settlement_periods", "line_items", "item_aliases",
            "matches", "period_totals", "anomalies", "evidence", "audit_log",
            "contract_docs", "contract_facts", "crosscheck_results", "schema_migrations",
        ]:
            assert expected in tables, f"missing table {expected}"
        alias_columns = {r[1] for r in conn.execute("PRAGMA table_info(item_aliases)")}
        assert "direction" in alias_columns
        total_columns = {r[1] for r in conn.execute("PRAGMA table_info(period_totals)")}
        assert {
            "raw_amount_sum", "calculated_amount_sum", "calculated_amount_used_sum",
            "effective_amount_sum", "amount_source", "amount_status",
            "ab_diff", "ab_status", "control_diff", "control_status",
            "verification_level",
        } <= total_columns
        header_columns = {r[1] for r in conn.execute("PRAGMA table_info(table_headers)")}
        assert {"data_range_status", "data_range_method", "data_range_evidence_json"} <= header_columns
        conn.close()

    def test_idempotent(self, tmp_path):
        db = _tmp_db(tmp_path)
        migrations.migrate(db, tmp_path / "backups")
        assert migrations.migrate(db, tmp_path / "backups") == migrations.LATEST_SCHEMA_VERSION

    def test_backup_created_before_first_migration(self, tmp_path):
        db = _tmp_db(tmp_path)
        migrations.migrate(db, tmp_path / "backups")
        backups = list((tmp_path / "backups").glob("*.db"))
        assert len(backups) == 1  # 空库 v0→最新 也留档

    def test_failure_rolls_back(self, tmp_path, monkeypatch):
        """中途 SQL 失败：已建表必须回滚，且抛 MigrationError。"""
        db = _tmp_db(tmp_path)
        broken = [(1, ["CREATE TABLE a (x INTEGER)", "THIS IS NOT SQL"])]
        monkeypatch.setattr(migrations, "MIGRATIONS", broken)
        monkeypatch.setattr(migrations, "LATEST_SCHEMA_VERSION", 1)
        with pytest.raises(migrations.MigrationError):
            migrations.migrate(db, tmp_path / "backups")
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "a" not in tables  # 回滚成功

    def test_migration_error_keeps_backup(self, tmp_path, monkeypatch):
        db = _tmp_db(tmp_path)
        broken = [(1, ["NOPE"])]
        monkeypatch.setattr(migrations, "MIGRATIONS", broken)
        monkeypatch.setattr(migrations, "LATEST_SCHEMA_VERSION", 1)
        with pytest.raises(migrations.MigrationError):
            migrations.migrate(db, tmp_path / "backups")
        assert list((tmp_path / "backups").glob("*.db"))

    def test_rollback_to_backup_restores(self, tmp_path):
        db = _tmp_db(tmp_path)
        migrations.migrate(db, tmp_path / "backups")
        backup = tmp_path / "backups" / "manual_latest.db"
        src = sqlite3.connect(db)
        dst = sqlite3.connect(backup)
        with dst:
            src.backup(dst)  # 用 backup API 生成完整快照（WAL 安全）
        dst.close()
        src.close()
        conn = migrations.connect(db)
        conn.execute("DROP TABLE projects")
        conn.close()
        migrations.rollback_to_backup(db, backup)
        conn = migrations.connect(db)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='projects'").fetchone()
        conn.close()


class TestFkAndWal:
    def test_foreign_keys_on(self, tmp_path):
        db = _tmp_db(tmp_path)
        migrations.migrate(db, tmp_path / "backups")
        conn = migrations.connect(db)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_files(project_id, original_path, stored_path, original_name,"
                " sha256, size_bytes, file_type, imported_at)"
                " VALUES (999,'a','b','c','d'*1||'x',1,'xlsx','2026-01-01')"
            )
        conn.close()

class TestLegacyUpgrade:
    def test_late_failure_restores_v1_database_completely(self, tmp_path, monkeypatch):
        """v1→v5 后续迁移失败时，不得留下任何部分提交。"""
        db = tmp_path / "legacy.db"
        conn = migrations.connect(db)
        try:
            for sql in migrations.MIGRATIONS[0][1]:
                conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-01-01')"
            )
            with conn:
                project_id = conn.execute(
                    "INSERT INTO projects(name, schema_version, workspace_path, created_at)"
                    " VALUES ('旧项目', 1, '/legacy', '2026')"
                ).lastrowid
                period_id = conn.execute(
                    "INSERT INTO settlement_periods(project_id, period_no, title, direction)"
                    " VALUES (?, 1, '旧第1期', 'upward')",
                    (project_id,),
                ).lastrowid
                conn.execute(
                    "INSERT INTO line_items(period_id, name, quantity, amount)"
                    " VALUES (?, '旧清单', '10', '1000')",
                    (period_id,),
                )
        finally:
            conn.close()

        broken = [*migrations.MIGRATIONS]
        latest_version, latest_scripts = broken[-1]
        broken[-1] = (latest_version, [*latest_scripts, "THIS IS NOT SQL"])
        monkeypatch.setattr(migrations, "MIGRATIONS", broken)
        monkeypatch.setattr(migrations, "LATEST_SCHEMA_VERSION", latest_version)

        with pytest.raises(migrations.MigrationError):
            migrations.migrate(db, tmp_path / "backups")

        conn = migrations.connect(db)
        try:
            assert [tuple(row) for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )] == [(1,)]
            assert tuple(conn.execute(
                "SELECT name, schema_version FROM projects WHERE id=?", (project_id,)
            ).fetchone()) == ("旧项目", 1)
            assert tuple(conn.execute(
                "SELECT name, amount FROM line_items WHERE period_id=?", (period_id,)
            ).fetchone()) == ("旧清单", "1000")

            raw_sheet_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(raw_sheets)")
            }
            assert "period_id" not in raw_sheet_columns
            header_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(table_headers)")
            }
            assert {"data_row_start", "data_row_end"}.isdisjoint(header_columns)

            settlement_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='settlement_periods'"
            ).fetchone()[0]
            assert "UNIQUE(project_id, period_no)" in settlement_sql
            assert "UNIQUE(project_id, period_no, direction)" not in settlement_sql
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO settlement_periods(project_id, period_no, title, direction)"
                    " VALUES (?, 1, '重复期次', 'downward')",
                    (project_id,),
                )
        finally:
            conn.close()

    def test_v1_database_upgrades_to_latest_with_data(self, tmp_path):
        """旧库兼容：v1 结构 + 已有数据 → migrate 到最新，数据保留。"""
        from costguard.core.db import migrations as m

        db = tmp_path / "legacy.db"
        # 手工只应用 v1
        conn = m.connect(db)
        for sql in m.MIGRATIONS[0][1]:
            conn.execute(sql)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-01-01')"
        )
        with conn:
            pid = conn.execute(
                "INSERT INTO projects(name, schema_version, workspace_path, created_at)"
                " VALUES ('旧项目', 1, '/legacy', '2026')"
            ).lastrowid
            cur = conn.execute(
                "INSERT INTO settlement_periods(project_id, period_no, title, source_file_id,"
                " direction, contract_party) VALUES (?, 1, '旧第1期', NULL, 'upward', '甲')",
                (pid,),
            )
            old_period_id = cur.lastrowid
            conn.execute(
                "INSERT INTO line_items(period_id, code, name, unit, quantity, amount)"
                " VALUES (?, 'C1', '旧清单', 'm3', '10', '1000')", (old_period_id,))
            conn.execute(
                """INSERT INTO item_aliases(project_id, canonical_key, alias_text,
                   mapping_basis, confirmed_by, confirmed_at) VALUES (?,?,?,?,?,?)""",
                (pid, "code:C1", "旧别名", "旧库人工确认", "用户", "2026"),
            )
        conn.close()

        # migrate 到最新（v3 表重建）
        assert m.migrate(db, tmp_path / "backups") == m.LATEST_SCHEMA_VERSION

        conn = m.connect(db)
        try:
            row = conn.execute(
                "SELECT title, direction FROM settlement_periods WHERE id=?", (old_period_id,)
            ).fetchone()
            assert row and row["title"] == "旧第1期" and row["direction"] == "upward"
            li = conn.execute("SELECT name, amount FROM line_items WHERE period_id=?", (old_period_id,)).fetchone()
            assert li and li["name"] == "旧清单"
            # v3 允许对上/对下同期号共存
            with conn:
                conn.execute(
                    "INSERT INTO settlement_periods(project_id, period_no, title, direction)"
                    " VALUES (?, 1, '旧第1期对下', 'downward')", (pid,))
            n1 = conn.execute(
                "SELECT COUNT(*) c FROM settlement_periods WHERE project_id=? AND period_no=1",
                (pid,),
            ).fetchone()["c"]
            assert n1 == 2
            alias = conn.execute(
                "SELECT canonical_key, alias_text, direction FROM item_aliases WHERE project_id=?",
                (pid,),
            ).fetchone()
            assert tuple(alias) == ("code:C1", "旧别名", "unknown")
            # 同文本别名可以在不同方向重新确认，同方向仍唯一。
            with conn:
                conn.execute(
                    """INSERT INTO item_aliases(project_id, direction, canonical_key,
                       alias_text, mapping_basis, confirmed_by, confirmed_at)
                       VALUES (?, 'upward', 'code:C1', '旧别名', '对上重新确认', '用户', '2026')""",
                    (pid,),
                )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO item_aliases(project_id, direction, canonical_key,
                       alias_text, mapping_basis, confirmed_by, confirmed_at)
                       VALUES (?, 'upward', 'code:C9', '旧别名', '重复', '用户', '2026')""",
                    (pid,),
                )
        finally:
            conn.close()

    def test_v6_legacy_match_is_conservatively_downgraded_on_v7(self, tmp_path):
        """v7 升级旧库时，历史二态 match 必须先降级为待重新校核。"""
        db = tmp_path / "legacy_v6.db"
        conn = migrations.connect(db)
        try:
            # 手工构造 v6 结构，避免先跑完 v7 再倒推字段，确保覆盖真实升级路径。
            conn.execute("PRAGMA foreign_keys=OFF")
            for version, scripts in migrations.MIGRATIONS:
                if version > 6:
                    break
                for sql in scripts:
                    conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, '2026-01-01')",
                    (version,),
                )
            project_id = conn.execute(
                "INSERT INTO projects(name, schema_version, workspace_path, created_at)"
                " VALUES ('v6旧项目', 6, '/legacy-v6', '2026')"
            ).lastrowid
            period_id = conn.execute(
                "INSERT INTO settlement_periods(project_id, period_no, title, direction)"
                " VALUES (?, 1, '旧第1期', 'upward')",
                (project_id,),
            ).lastrowid
            conn.execute(
                "INSERT INTO period_totals(project_id, period_id, item_key, amount_sum, cross_check_status)"
                " VALUES (?, ?, 'code:OLD', '100', 'match')",
                (project_id, period_id),
            )
        finally:
            conn.close()

        probe = migrations.connect(db)
        try:
            assert migrations.current_version(probe) == 6
        finally:
            probe.close()
        assert migrations.migrate(db, tmp_path / "backups") == migrations.LATEST_SCHEMA_VERSION

        conn = migrations.connect(db)
        try:
            row = conn.execute(
                "SELECT verification_level, cross_check_status FROM period_totals WHERE period_id=?",
                (period_id,),
            ).fetchone()
            assert row is not None
            assert row["verification_level"] == "insufficient"
            assert row["cross_check_status"] == "insufficient"
        finally:
            conn.close()
