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
        conn.close()
        for expected in [
            "projects", "source_files", "parse_batches", "raw_sheets", "raw_cells",
            "table_headers", "settlement_periods", "line_items", "item_aliases",
            "matches", "period_totals", "anomalies", "evidence", "audit_log",
            "contract_docs", "contract_facts", "schema_migrations",
        ]:
            assert expected in tables, f"missing table {expected}"

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
