"""Migration 纪律测试：版本管理、自动备份、失败回滚。"""
import sqlite3
from pathlib import Path

import pytest

from jiadun.core.db import migrations


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
            "contract_docs", "contract_facts", "crosscheck_results", "run_contracts",
            "export_runs", "detection_runs", "import_manifests", "import_manifest_entries",
            "cleaning_changes", "sheet_coverage_proofs", "row_classifications",
            "evidence_events", "crosscheck_result_history", "period_total_history",
            "diff_runs", "diff_items", "mapping_templates",
            "finding_status_events",
            "rule_configurations", "alias_knowledge", "alias_knowledge_events",
            "project_versions", "project_version_items", "project_closures",
            "historical_unit_prices",
            "schema_migrations",
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
        assert {"run_signature", "run_id"} <= total_columns
        result_columns = {r[1] for r in conn.execute("PRAGMA table_info(crosscheck_results)")}
        assert "run_signature" in result_columns
        assert {
            "classified_detail_rows", "business_rows_used", "coverage_proof_status",
            "ab_row_set_status", "ab_independence_level", "c_control_evidence_id",
            "run_id",
        } <= result_columns
        match_columns = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
        assert {"run_signature", "run_id"} <= match_columns
        anomaly_columns = {r[1] for r in conn.execute("PRAGMA table_info(anomalies)")}
        assert {"run_signature", "run_id", "lifecycle_status", "repeat_history_json"} <= anomaly_columns
        detection_columns = {r[1] for r in conn.execute("PRAGMA table_info(detection_runs)")}
        assert {
            "expected_json", "executed_json", "skipped_json", "failed_json",
            "status", "run_kind",
        } <= detection_columns
        assert "run_id" in detection_columns
        evidence_columns = {r[1] for r in conn.execute("PRAGMA table_info(evidence)")}
        assert {"run_id", "scope", "historical_reason"} <= evidence_columns
        audit_columns = {r[1] for r in conn.execute("PRAGMA table_info(audit_log)")}
        assert {"run_id", "run_signature"} <= audit_columns
        manifest_columns = {r[1] for r in conn.execute("PRAGMA table_info(import_manifests)")}
        assert {"manifest_key", "control_hash", "status", "version"} <= manifest_columns
        cleaning_columns = {r[1] for r in conn.execute("PRAGMA table_info(cleaning_changes)")}
        assert {"run_signature", "event_key", "status", "evidence_id", "audit_id", "run_id"} <= cleaning_columns
        contract_columns = {r[1] for r in conn.execute("PRAGMA table_info(run_contracts)")}
        assert "run_id" in contract_columns
        proof_columns = {r[1] for r in conn.execute("PRAGMA table_info(sheet_coverage_proofs)")}
        assert {
            "raw_data_row_count", "classified_row_count", "business_rows_used",
            "proof_status", "c_control_source_json", "ab_independence_level",
            "run_signature", "run_id",
        } <= proof_columns
        row_columns = {r[1] for r in conn.execute("PRAGMA table_info(row_classifications)")}
        assert {"class_code", "raw_values_json", "effective_amount", "participates_in_a"} <= row_columns
        header_columns = {r[1] for r in conn.execute("PRAGMA table_info(table_headers)")}
        assert {"data_range_status", "data_range_method", "data_range_evidence_json"} <= header_columns
        sheet_columns = {r[1] for r in conn.execute("PRAGMA table_info(raw_sheets)")}
        assert {"sheet_status", "sheet_status_reason", "sheet_status_updated_at", "sheet_status_actor"} <= sheet_columns
        triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='run_contracts'"
        )}
        assert {"trg_run_contracts_immutable_update", "trg_run_contracts_immutable_delete"} <= triggers
        raw_cell_triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='raw_cells'"
        )}
        assert {
            "trg_raw_cells_immutable_update",
            "trg_raw_cells_immutable_delete",
            "trg_raw_cells_grid_scope_guard",
        } <= raw_cell_triggers
        raw_sheet_triggers = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='raw_sheets'"
            )
        }
        assert "trg_raw_sheets_grid_immutable_update" in raw_sheet_triggers
        table_header_triggers = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='table_headers'"
            )
        }
        assert "trg_table_headers_immutable_update" in table_header_triggers
        source_file_triggers = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='source_files'"
            )
        }
        assert "trg_source_files_identity_immutable_update" in source_file_triggers
        version_source_triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_project_version_items_snapshot_%'"
        )}
        assert "trg_project_version_items_snapshot_source_guard" in version_source_triggers
        assert "trg_project_version_items_explicit_source_scope_guard" in {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_project_version_items_%source_scope_guard'"
            )
        }
        history_triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE '%history_immutable_%'"
        )}
        assert {
            "trg_crosscheck_result_history_immutable_update",
            "trg_crosscheck_result_history_immutable_delete",
            "trg_period_total_history_immutable_update",
            "trg_period_total_history_immutable_delete",
        } <= history_triggers
        diff_triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_diff_%_immutable_%'"
        )}
        assert {
            "trg_diff_runs_immutable_update",
            "trg_diff_runs_immutable_delete",
            "trg_diff_items_immutable_update",
            "trg_diff_items_immutable_delete",
        } <= diff_triggers
        template_triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_mapping_templates_immutable_%'"
        )}
        assert {
            "trg_mapping_templates_immutable_update",
            "trg_mapping_templates_immutable_delete",
        } <= template_triggers
        finding_event_triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_finding_status_events_immutable_%'"
        )}
        assert {
            "trg_finding_status_events_immutable_update",
            "trg_finding_status_events_immutable_delete",
        } <= finding_event_triggers
        alias_knowledge_triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_alias_knowledge%immutable_%'"
        )}
        assert {
            "trg_alias_knowledge_immutable_update",
            "trg_alias_knowledge_immutable_delete",
            "trg_alias_knowledge_events_immutable_update",
            "trg_alias_knowledge_events_immutable_delete",
        } <= alias_knowledge_triggers
        assert {
            "trg_anomalies_snapshot_immutable_update",
            "trg_anomalies_lifecycle_guard",
        } <= {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_anomalies_%'"
            )
        }
        rule_config_triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_rule_configurations_immutable_%'"
        )}
        assert {
            "trg_rule_configurations_immutable_update",
            "trg_rule_configurations_immutable_delete",
        } <= rule_config_triggers
        history_price_triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND (name LIKE 'trg_project_closures_%' OR name LIKE 'trg_historical_unit_prices_%')"
        )}
        assert {
            "trg_project_closures_insert_guard",
            "trg_project_closures_immutable_update",
            "trg_project_closures_immutable_delete",
            "trg_historical_unit_prices_insert_guard",
            "trg_historical_unit_prices_immutable_update",
            "trg_historical_unit_prices_immutable_delete",
            "trg_historical_unit_prices_snapshot_source_guard",
        } <= history_price_triggers
        version_scope_triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('trg_project_versions_insert_guard', "
            "'trg_project_version_items_insert_guard', "
            "'trg_evidence_historical_immutable_scope', "
            "'trg_evidence_historical_immutable_delete')"
        )}
        assert {
            "trg_project_versions_insert_guard",
            "trg_project_version_items_insert_guard",
            "trg_evidence_historical_immutable_scope",
            "trg_evidence_historical_immutable_delete",
        } <= version_scope_triggers
        version_chain_triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_project_versions_evidence_chain_%'"
        )}
        assert {
            "trg_project_versions_evidence_chain_guard",
            "trg_project_versions_evidence_chain_update_guard",
        } <= version_chain_triggers
        version_target_triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_project_versions_evidence_target_%'"
        )}
        assert {
            "trg_project_versions_evidence_target_guard",
            "trg_project_versions_evidence_target_update_guard",
        } <= version_target_triggers
        closure_target_triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_project_closures_evidence_target_%'"
        )}
        assert {
            "trg_project_closures_evidence_target_guard",
            "trg_project_closures_evidence_target_update_guard",
        } <= closure_target_triggers
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_project_version_items_period_scope_guard'"
        ).fetchone()
        assert {
            "trg_sheet_coverage_proofs_scope_guard",
            "trg_row_classifications_scope_guard",
        } <= {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_%_scope_guard'"
            )
        }
        conn.close()

    def test_v43_protects_source_file_identity_and_raw_cell_grid(self, tmp_path):
        """源文件身份不可改写，原始单元格不得写出 Sheet 声明网格。"""
        db = _tmp_db(tmp_path)
        migrations.migrate(db, tmp_path / "backups")
        conn = migrations.connect(db)
        try:
            with conn:
                project_id = conn.execute(
                    "INSERT INTO projects(name, schema_version, workspace_path, created_at) "
                    "VALUES ('v43', ?, '/v43', '2026')",
                    (migrations.LATEST_SCHEMA_VERSION,),
                ).lastrowid
                file_id = conn.execute(
                    """INSERT INTO source_files(
                           project_id, original_path, stored_path, original_name,
                           sha256, size_bytes, file_type, imported_at)
                       VALUES (?, '/in.xlsx', '/stored.xlsx', 'in.xlsx', 'sha', 3, 'xlsx', '2026')""",
                    (project_id,),
                ).lastrowid
                batch_id = conn.execute(
                    "INSERT INTO parse_batches(file_id, parser, parsed_at, status) "
                    "VALUES (?, 'test', '2026', 'ok')",
                    (file_id,),
                ).lastrowid
                sheet_id = conn.execute(
                    """INSERT INTO raw_sheets(
                           batch_id, sheet_index, sheet_name, n_rows, n_cols)
                       VALUES (?, 0, 'Sheet1', 2, 2)""",
                    (batch_id,),
                ).lastrowid
            conn.execute(
                "UPDATE source_files SET stored_path='/restored.xlsx' WHERE id=?",
                (file_id,),
            )
            with pytest.raises(sqlite3.IntegrityError, match="source file identity immutable"):
                conn.execute(
                    "UPDATE source_files SET original_name='forged.xlsx' WHERE id=?",
                    (file_id,),
                )
            conn.execute(
                "INSERT INTO raw_cells(sheet_id, row, col, raw_value) VALUES (?, 2, 2, 'ok')",
                (sheet_id,),
            )
            with pytest.raises(sqlite3.IntegrityError, match="raw cell outside sheet grid"):
                conn.execute(
                    "INSERT INTO raw_cells(sheet_id, row, col, raw_value) VALUES (?, 3, 1, 'out')",
                    (sheet_id,),
                )
            with pytest.raises(sqlite3.IntegrityError, match="raw cell outside sheet grid"):
                conn.execute(
                    "INSERT INTO raw_cells(sheet_id, row, col, raw_value) VALUES (?, 1, 3, 'out')",
                    (sheet_id,),
                )
        finally:
            conn.close()

    def test_v34_blocks_cross_project_version_items_and_historical_resurrection(self, tmp_path):
        """v34 不能让旧脚本跨项目写快照，也不能复活或删除历史 Evidence。"""
        db = _tmp_db(tmp_path)
        migrations.migrate(db, tmp_path / "backups")
        conn = migrations.connect(db)
        try:
            with conn:
                p1 = conn.execute(
                    "INSERT INTO projects(name, schema_version, workspace_path, created_at) "
                    "VALUES ('P1', ?, '/p1', '2026')",
                    (migrations.LATEST_SCHEMA_VERSION,),
                ).lastrowid
                p2 = conn.execute(
                    "INSERT INTO projects(name, schema_version, workspace_path, created_at) "
                    "VALUES ('P2', ?, '/p2', '2026')",
                    (migrations.LATEST_SCHEMA_VERSION,),
                ).lastrowid
                v1 = conn.execute(
                    """INSERT INTO project_versions(
                           project_id, version_no, version_kind, title,
                           snapshot_sha256, created_by, created_at, reason)
                       VALUES (?, 1, 'initial_submission', 'P1 v1', 'sha', 'u', '2026', 'r')""",
                    (p1,),
                ).lastrowid
                v2 = conn.execute(
                    """INSERT INTO project_versions(
                           project_id, version_no, version_kind, title,
                           snapshot_sha256, created_by, created_at, reason)
                       VALUES (?, 1, 'initial_submission', 'P2 v1', 'sha', 'u', '2026', 'r')""",
                    (p2,),
                ).lastrowid
                with pytest.raises(
                    sqlite3.IntegrityError,
                    match="project version item (source|scope) mismatch|project version item scope incomplete",
                ):
                    conn.execute(
                        """INSERT INTO project_version_items(
                               version_id, project_id, identity_key, occurrence,
                               direction, created_at)
                           VALUES (?, ?, 'cross', 1, 'unknown', '2026')""",
                        (v1, p2),
                    )
                ev = conn.execute(
                    """INSERT INTO evidence(project_id, kind, summary, created_at)
                       VALUES (?, 'test', '历史证据', '2026')""",
                    (p1,),
                ).lastrowid
                conn.execute(
                    "UPDATE evidence SET scope='historical', historical_reason='测试' WHERE id=?",
                    (ev,),
                )
                with pytest.raises(sqlite3.IntegrityError, match="historical evidence cannot be revived"):
                    conn.execute("UPDATE evidence SET scope='current' WHERE id=?", (ev,))
                with pytest.raises(sqlite3.IntegrityError, match="historical evidence cannot be deleted"):
                    conn.execute("DELETE FROM evidence WHERE id=?", (ev,))
                period_id = conn.execute(
                    """INSERT INTO settlement_periods(project_id, period_no, title, direction)
                       VALUES (?, 1, 'P1 第1期', 'upward') RETURNING id""",
                    (p1,),
                ).fetchone()[0]
                line_item_id = conn.execute(
                    """INSERT INTO line_items(
                           period_id, code, name, unit, quantity, unit_price, amount, flags_json)
                       VALUES (?, 'L', '本地项', '项', '1', '1', '1', '{}') RETURNING id""",
                    (period_id,),
                ).fetchone()[0]
                # 合法的同项目版本快照仍可写入，避免闸门把 API 的最小快照路径挡住。
                conn.execute(
                    """INSERT INTO project_version_items(
                           version_id, project_id, identity_key, occurrence,
                           period_id, period_no, direction, line_item_id,
                           code, name, unit, quantity, unit_price, amount,
                           source_row, created_at)
                       VALUES (?, ?, 'local', 1, ?, 1, 'upward', ?,
                               'L', '本地项', '项', '1', '1', '1', 1, '2026')""",
                    (v1, p1, period_id, line_item_id),
                )
                assert conn.execute(
                    "SELECT COUNT(*) FROM project_version_items WHERE version_id=?", (v1,)
                ).fetchone()[0] == 1
                assert v2
        finally:
            conn.close()

    def test_idempotent(self, tmp_path):
        db = _tmp_db(tmp_path)
        migrations.migrate(db, tmp_path / "backups")
        assert migrations.migrate(db, tmp_path / "backups") == migrations.LATEST_SCHEMA_VERSION

    def test_future_schema_is_rejected_without_downgrading_project_marker(self, tmp_path):
        """当前程序不得打开未来库，也不得把项目版本标记降写。"""
        db = _tmp_db(tmp_path)
        migrations.migrate(db, tmp_path / "backups")
        conn = migrations.connect(db)
        try:
            with conn:
                project_id = conn.execute(
                    "INSERT INTO projects(name, schema_version, workspace_path, created_at) "
                    "VALUES ('未来库', ?, '/future', '2026')",
                    (migrations.LATEST_SCHEMA_VERSION,),
                ).lastrowid
                future_version = migrations.LATEST_SCHEMA_VERSION + 1
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, '2026')",
                    (future_version,),
                )
        finally:
            conn.close()

        with pytest.raises(migrations.MigrationError, match="未来 schema"):
            migrations.migrate(db, tmp_path / "backups")

        check = migrations.connect(db)
        try:
            assert migrations.current_version(check) == future_version
            assert check.execute(
                "SELECT schema_version FROM projects WHERE id=?", (project_id,)
            ).fetchone()[0] == migrations.LATEST_SCHEMA_VERSION
        finally:
            check.close()

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
        from jiadun.core.db import migrations as m

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
