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
    (
        # v8: 运行契约与成果登记。
        # 任何源文件/工作表范围/字段映射/规则配置变化都必须产生新的运行签名；
        # 旧校核、匹配和导出只能作为历史记录，不能继续冒充当前输入下的成果。
        8,
        [
            "ALTER TABLE period_totals ADD COLUMN run_signature TEXT",
            "ALTER TABLE crosscheck_results ADD COLUMN run_signature TEXT",
            "ALTER TABLE matches ADD COLUMN run_signature TEXT",
            "ALTER TABLE anomalies ADD COLUMN run_signature TEXT",
            """CREATE TABLE IF NOT EXISTS run_contracts (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                signature TEXT NOT NULL,
                components_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                invalidated_at TEXT,
                UNIQUE(project_id, signature)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_run_contracts_current
               ON run_contracts(project_id, invalidated_at, id)""",
            """CREATE TABLE IF NOT EXISTS export_runs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                run_signature TEXT,
                file_sha256 TEXT,
                generated_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'current',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )""",
            """CREATE INDEX IF NOT EXISTS idx_export_runs_project_kind
               ON export_runs(project_id, kind, generated_at)""",
            """CREATE INDEX IF NOT EXISTS idx_period_totals_run_signature
               ON period_totals(project_id, run_signature)""",
            """CREATE INDEX IF NOT EXISTS idx_crosscheck_results_run_signature
               ON crosscheck_results(project_id, run_signature)""",
            """CREATE INDEX IF NOT EXISTS idx_matches_run_signature
               ON matches(project_id, run_signature)""",
            """CREATE INDEX IF NOT EXISTS idx_anomalies_run_signature
               ON anomalies(project_id, run_signature)""",
        ],
    ),
    (
        # v9: 统一 Finding/Evidence 保真字段。
        # 旧库中的运行结果没有可验证签名，升级后只保留为历史，不允许继续
        # 混入新的当前结果面；人工后来写入的 NULL 行仍可由兼容读取面识别。
        9,
        [
            "ALTER TABLE evidence ADD COLUMN run_signature TEXT",
            "ALTER TABLE evidence ADD COLUMN finding_id TEXT",
            "ALTER TABLE anomalies ADD COLUMN finding_id TEXT",
            "ALTER TABLE anomalies ADD COLUMN fingerprint TEXT",
            "ALTER TABLE anomalies ADD COLUMN confidence TEXT NOT NULL DEFAULT 'medium'",
            "ALTER TABLE anomalies ADD COLUMN detection_mode TEXT NOT NULL DEFAULT 'automated'",
            "ALTER TABLE anomalies ADD COLUMN raw_values_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE anomalies ADD COLUMN normalized_values_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE anomalies ADD COLUMN impact TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE anomalies ADD COLUMN limitations_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE anomalies ADD COLUMN recommendation TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE anomalies ADD COLUMN suppression_reason TEXT",
            # v1-v8 旧结果没有运行契约，显式标记为历史失效，避免与新契约
            # 的 NULL 签名混淆；不删除任何证据或原始业务记录。
            "UPDATE period_totals SET run_signature='legacy:stale' WHERE run_signature IS NULL",
            "UPDATE crosscheck_results SET run_signature='legacy:stale' WHERE run_signature IS NULL",
            "UPDATE matches SET run_signature='legacy:stale' WHERE run_signature IS NULL",
            "UPDATE anomalies SET run_signature='legacy:stale' WHERE run_signature IS NULL",
            "CREATE INDEX IF NOT EXISTS idx_anomalies_finding ON anomalies(project_id, finding_id)",
            "CREATE INDEX IF NOT EXISTS idx_evidence_run_signature ON evidence(project_id, run_signature)",
        ],
    ),
    (
        # v10: 检测覆盖率、权威批次清单与清洗事件。
        # 这些表记录“是否完整执行/应到资料是否闭合/人工如何提出与处置变更”，
        # 不把技术覆盖率或输入完整性硬塞进业务异常，也不修改原始文件和明细。
        10,
        [
            """CREATE TABLE IF NOT EXISTS detection_runs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                run_signature TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                expected_json TEXT NOT NULL DEFAULT '[]',
                executed_json TEXT NOT NULL DEFAULT '[]',
                skipped_json TEXT NOT NULL DEFAULT '{}',
                failed_json TEXT NOT NULL DEFAULT '{}',
                critical_failed_json TEXT NOT NULL DEFAULT '[]',
                error_summary TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )""",
            "CREATE INDEX IF NOT EXISTS idx_detection_runs_current "
            "ON detection_runs(project_id, run_signature, id)",
            """CREATE TABLE IF NOT EXISTS import_manifests (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                manifest_key TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                declared_by TEXT NOT NULL DEFAULT '',
                declared_at TEXT NOT NULL,
                control_hash TEXT,
                status TEXT NOT NULL DEFAULT 'not_assessed',
                note TEXT NOT NULL DEFAULT '',
                version TEXT NOT NULL DEFAULT '1'
            )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_import_manifests_key "
            "ON import_manifests(project_id, manifest_key)",
            """CREATE TABLE IF NOT EXISTS import_manifest_entries (
                id INTEGER PRIMARY KEY,
                manifest_id INTEGER NOT NULL REFERENCES import_manifests(id) ON DELETE CASCADE,
                logical_key TEXT NOT NULL,
                expected_name TEXT,
                expected_sha256 TEXT,
                expected_period_no INTEGER,
                expected_direction TEXT,
                expected_sheet_name TEXT,
                required INTEGER NOT NULL DEFAULT 1,
                received_file_id INTEGER REFERENCES source_files(id),
                state TEXT NOT NULL DEFAULT 'missing',
                note TEXT NOT NULL DEFAULT '',
                source_reference_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(manifest_id, logical_key)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_import_manifest_entries_state "
            "ON import_manifest_entries(manifest_id, state)",
            """CREATE TABLE IF NOT EXISTS cleaning_changes (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                run_signature TEXT NOT NULL,
                event_key TEXT NOT NULL,
                subject_type TEXT NOT NULL,
                subject_id INTEGER NOT NULL,
                field_name TEXT NOT NULL,
                before_json TEXT,
                proposed_json TEXT,
                status TEXT NOT NULL DEFAULT 'proposed',
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by TEXT,
                decision_note TEXT,
                evidence_id INTEGER REFERENCES evidence(id),
                audit_id INTEGER REFERENCES audit_log(id),
                UNIQUE(project_id, event_key)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_cleaning_changes_current "
            "ON cleaning_changes(project_id, run_signature, status, id)",
        ],
    ),
    (
        # v11: 覆盖率运行类型。
        # 异常规则检测与 A/B/C 聚合验证共用可审阅覆盖率结构，但必须按阶段
        # 分开读取，不能把一次异常检测误当成聚合验证已经完成。
        11,
        [
            "ALTER TABLE detection_runs ADD COLUMN run_kind TEXT NOT NULL DEFAULT 'anomaly_detection'",
            "CREATE INDEX IF NOT EXISTS idx_detection_runs_kind_current "
            "ON detection_runs(project_id, run_signature, run_kind, id)",
        ],
    ),
    (
        # v12: Run Contract 历史不可变与独立运行身份。
        # v8 的 UNIQUE(project_id, signature) 会让输入恢复到旧签名时重新
        # 激活历史合同。重建表移除该唯一约束，保留 signature 索引；历史行
        # 获得稳定迁移 run_id，新合同永远插入新行，不改写旧合同。
        12,
        [
            "ALTER TABLE run_contracts RENAME TO run_contracts_v11",
            "DROP INDEX IF EXISTS idx_run_contracts_current",
            """CREATE TABLE run_contracts (
                id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                signature TEXT NOT NULL,
                components_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                invalidated_at TEXT
            )""",
            """INSERT INTO run_contracts(
                   id, run_id, project_id, signature, components_json,
                   created_at, invalidated_at
               )
               SELECT id, 'legacy-run-' || project_id || '-' || id,
                      project_id, signature, components_json,
                      created_at, invalidated_at
               FROM run_contracts_v11""",
            "DROP TABLE run_contracts_v11",
            "CREATE INDEX IF NOT EXISTS idx_run_contracts_current "
            "ON run_contracts(project_id, invalidated_at, id)",
            "CREATE INDEX IF NOT EXISTS idx_run_contracts_signature "
            "ON run_contracts(project_id, signature, id)",
        ],
    ),
    (
        # v13: Sheet 覆盖证明与逐行分类。
        # 聚合计数只能说明结果形状，不能证明原始有效区域内每一行如何被
        # 处理；新表保留逐 Sheet、逐行的不可变事实，旧结果不被覆盖。
        13,
        [
            """CREATE TABLE IF NOT EXISTS sheet_coverage_proofs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                run_signature TEXT,
                file_id INTEGER REFERENCES source_files(id),
                batch_id INTEGER REFERENCES parse_batches(id),
                sheet_id INTEGER NOT NULL REFERENCES raw_sheets(id),
                period_id INTEGER REFERENCES settlement_periods(id),
                direction TEXT NOT NULL DEFAULT 'unknown',
                raw_row_start INTEGER NOT NULL,
                raw_row_end INTEGER NOT NULL,
                raw_col_start INTEGER NOT NULL,
                raw_col_end INTEGER NOT NULL,
                raw_data_row_count INTEGER NOT NULL,
                classified_row_count INTEGER NOT NULL,
                classified_detail_rows INTEGER NOT NULL DEFAULT 0,
                excluded_subtotal_rows INTEGER NOT NULL DEFAULT 0,
                excluded_title_rows INTEGER NOT NULL DEFAULT 0,
                excluded_note_rows INTEGER NOT NULL DEFAULT 0,
                excluded_blank_rows INTEGER NOT NULL DEFAULT 0,
                excluded_tail_note_rows INTEGER NOT NULL DEFAULT 0,
                excluded_orphan_numeric_rows INTEGER NOT NULL DEFAULT 0,
                excluded_parse_failed_rows INTEGER NOT NULL DEFAULT 0,
                pending_rows INTEGER NOT NULL DEFAULT 0,
                unrecognized_rows INTEGER NOT NULL DEFAULT 0,
                business_rows_used INTEGER NOT NULL DEFAULT 0,
                raw_amount_total TEXT,
                detail_amount_total TEXT,
                proof_status TEXT NOT NULL,
                proof_reason TEXT NOT NULL DEFAULT '[]',
                c_control_status TEXT NOT NULL DEFAULT 'not_assessed',
                c_control_value TEXT,
                c_control_source_json TEXT NOT NULL DEFAULT '{}',
                c_control_evidence_id INTEGER REFERENCES evidence(id),
                ab_row_set_status TEXT NOT NULL DEFAULT 'same_row_set',
                ab_row_set_hash TEXT,
                ab_independence_level TEXT NOT NULL DEFAULT 'shared_extractor',
                evidence_id INTEGER REFERENCES evidence(id),
                created_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_sheet_coverage_proofs_current "
            "ON sheet_coverage_proofs(project_id, run_signature, sheet_id, id)",
            """CREATE TABLE IF NOT EXISTS row_classifications (
                id INTEGER PRIMARY KEY,
                proof_id INTEGER NOT NULL REFERENCES sheet_coverage_proofs(id) ON DELETE CASCADE,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                sheet_id INTEGER NOT NULL REFERENCES raw_sheets(id),
                row_number INTEGER NOT NULL,
                class_code TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                source_range_json TEXT NOT NULL DEFAULT '{}',
                raw_values_json TEXT NOT NULL DEFAULT '{}',
                calculated_amount TEXT,
                effective_amount TEXT,
                participates_in_a INTEGER NOT NULL DEFAULT 0,
                participates_in_b INTEGER NOT NULL DEFAULT 0,
                participates_in_c INTEGER NOT NULL DEFAULT 0,
                is_pending INTEGER NOT NULL DEFAULT 0,
                is_parse_failed INTEGER NOT NULL DEFAULT 0,
                evidence_id INTEGER REFERENCES evidence(id),
                created_at TEXT NOT NULL,
                UNIQUE(proof_id, row_number)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_row_classifications_proof "
            "ON row_classifications(proof_id, row_number)",
            "CREATE INDEX IF NOT EXISTS idx_row_classifications_sheet "
            "ON row_classifications(project_id, sheet_id, row_number)",
        ],
    ),
    (
        # v14: 将 P0-01 覆盖证明摘要固定在校核结果快照中。
        # ``crosscheck_results`` 仍保留旧字段以兼容现有读取面；新增字段只
        # 保存本次结果使用的逐 Sheet 证明、A/B 行集关系和 C 控制来源摘要，
        # 不把历史结果重写成当前结果。
        14,
        [
            "ALTER TABLE crosscheck_results ADD COLUMN classified_detail_rows INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE crosscheck_results ADD COLUMN business_rows_used INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE crosscheck_results ADD COLUMN coverage_proof_status TEXT NOT NULL DEFAULT 'unproven'",
            "ALTER TABLE crosscheck_results ADD COLUMN ab_row_set_status TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE crosscheck_results ADD COLUMN ab_row_set_hash TEXT",
            "ALTER TABLE crosscheck_results ADD COLUMN ab_independence_level TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE crosscheck_results ADD COLUMN c_control_source_json TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE crosscheck_results ADD COLUMN c_control_evidence_id INTEGER REFERENCES evidence(id)",
        ],
    ),
    (
        # v15: 把不可变 Run Contract 的独立运行身份传播到成果和证据。
        # ``run_signature`` 继续作为兼容筛选键；``run_id`` 用于审计和跨表
        # 追踪同一次运行。列允许 NULL 以兼容原始来源/历史人工记录，当前
        # 自动校核写入时必须绑定活动合同的 run_id。
        15,
        [
            "ALTER TABLE evidence ADD COLUMN run_id TEXT",
            "ALTER TABLE period_totals ADD COLUMN run_id TEXT",
            "ALTER TABLE crosscheck_results ADD COLUMN run_id TEXT",
            "ALTER TABLE matches ADD COLUMN run_id TEXT",
            "ALTER TABLE anomalies ADD COLUMN run_id TEXT",
            "ALTER TABLE detection_runs ADD COLUMN run_id TEXT",
            "ALTER TABLE export_runs ADD COLUMN run_id TEXT",
            "ALTER TABLE cleaning_changes ADD COLUMN run_id TEXT",
            "ALTER TABLE audit_log ADD COLUMN run_id TEXT",
            "CREATE INDEX IF NOT EXISTS idx_evidence_run_id ON evidence(project_id, run_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_crosscheck_results_run_id ON crosscheck_results(project_id, run_id, period_id)",
            "CREATE INDEX IF NOT EXISTS idx_period_totals_run_id ON period_totals(project_id, run_id, period_id)",
            "CREATE INDEX IF NOT EXISTS idx_detection_runs_run_id ON detection_runs(project_id, run_id, id)",
        ],
    ),
    (
        # v16: Evidence/人工审计范围显式化。
        # source、current、historical、human 四类范围不能依赖摘要文字猜测；
        # 历史原因单独保存，旧 Evidence 内容仍保留并可被审计追溯。
        16,
        [
            "ALTER TABLE evidence ADD COLUMN scope TEXT NOT NULL DEFAULT 'source'",
            "ALTER TABLE evidence ADD COLUMN historical_reason TEXT",
            "ALTER TABLE audit_log ADD COLUMN run_signature TEXT",
            "CREATE INDEX IF NOT EXISTS idx_evidence_scope ON evidence(project_id, scope, id)",
            "CREATE INDEX IF NOT EXISTS idx_audit_log_run_id ON audit_log(project_id, run_id, id)",
        ],
    ),
    (
        # v17: 持久化 Sheet 四级状态，并为 Evidence 失效动作保留追加事件。
        # raw_sheets 的状态是当前事实快照；原始网格和历史 Evidence 不删除，
        # evidence_events 保存失效前快照，避免仅靠 UPDATE 后的文字无法还原
        # 原始证据语义。
        17,
        [
            "ALTER TABLE raw_sheets ADD COLUMN sheet_status TEXT NOT NULL DEFAULT 'pending'",
            "ALTER TABLE raw_sheets ADD COLUMN sheet_status_reason TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE raw_sheets ADD COLUMN sheet_status_updated_at TEXT",
            "ALTER TABLE raw_sheets ADD COLUMN sheet_status_actor TEXT",
            "UPDATE raw_sheets SET sheet_status='confirmed', sheet_status_reason='已进入结算模型' "
            "WHERE period_id IS NOT NULL",
            "UPDATE raw_sheets SET sheet_status='non_business', "
            "sheet_status_reason='历史人工确认非结算角色' "
            "WHERE period_id IS NULL AND EXISTS ("
            "SELECT 1 FROM audit_log al WHERE al.project_id=("
            "SELECT sf.project_id FROM parse_batches pb JOIN source_files sf ON sf.id=pb.file_id "
            "WHERE pb.id=raw_sheets.batch_id"
            ") AND al.target='sheet:'||raw_sheets.id "
            "AND al.action='confirm_sheet_non_settlement_role')",
            "CREATE INDEX IF NOT EXISTS idx_raw_sheets_status "
            "ON raw_sheets(sheet_status, period_id, id)",
            """CREATE TABLE IF NOT EXISTS evidence_events (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                evidence_id INTEGER NOT NULL REFERENCES evidence(id),
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'system',
                run_id TEXT,
                run_signature TEXT,
                before_scope TEXT,
                after_scope TEXT,
                before_summary TEXT,
                before_steps_json TEXT,
                before_sources_json TEXT,
                reason TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )""",
            "CREATE INDEX IF NOT EXISTS idx_evidence_events_evidence "
            "ON evidence_events(project_id, evidence_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_evidence_events_run "
            "ON evidence_events(project_id, run_id, id)",
        ],
    ),
    (
        # v18: 在数据库层锁定 Run Contract 的不可变字段。
        # 应用层只允许把当前合同从 active 单向标记为 invalidated；运行身份、
        # 项目、签名、组成快照和创建时间不得被就地修改，历史行也不得删除或
        # 重新激活。这样即使有旧脚本/插件绕过 ensure_run_contract，也不能把
        # 历史合同伪装成当前合同。
        18,
        [
            """CREATE TRIGGER IF NOT EXISTS trg_run_contracts_immutable_update
               BEFORE UPDATE ON run_contracts
               WHEN NOT (
                   NEW.id IS OLD.id
                   AND NEW.run_id IS OLD.run_id
                   AND NEW.project_id IS OLD.project_id
                   AND NEW.signature IS OLD.signature
                   AND NEW.components_json IS OLD.components_json
                   AND NEW.created_at IS OLD.created_at
                   AND (
                       NEW.invalidated_at IS OLD.invalidated_at
                       OR (OLD.invalidated_at IS NULL AND NEW.invalidated_at IS NOT NULL)
                   )
               )
               BEGIN
                   SELECT RAISE(ABORT, 'run_contract immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_run_contracts_immutable_delete
               BEFORE DELETE ON run_contracts
               BEGIN
                   SELECT RAISE(ABORT, 'run_contract immutable');
               END""",
        ],
    ),
    (
        # v19: 为按期次唯一的 A/B/C 与聚合结果保留不可变历史快照。
        # 现有结果表继续作为“当前运行读取面”兼容视图使用；重跑时由触发器
        # 先把旧行完整序列化到历史表，避免 ON CONFLICT UPDATE 覆盖原始金额、
        # 校核级别和 Evidence 关联。历史快照永不进入 current_scope。
        19,
        [
            """CREATE TABLE IF NOT EXISTS crosscheck_result_history (
                history_id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                source_id INTEGER NOT NULL,
                period_id INTEGER NOT NULL,
                run_signature TEXT,
                run_id TEXT,
                snapshot_json TEXT NOT NULL,
                archived_at TEXT NOT NULL,
                archive_reason TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_crosscheck_history_period "
            "ON crosscheck_result_history(project_id, period_id, history_id)",
            "CREATE INDEX IF NOT EXISTS idx_crosscheck_history_run "
            "ON crosscheck_result_history(project_id, run_id, history_id)",
            """CREATE TABLE IF NOT EXISTS period_total_history (
                history_id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                source_id INTEGER NOT NULL,
                period_id INTEGER NOT NULL,
                item_key TEXT NOT NULL,
                run_signature TEXT,
                run_id TEXT,
                snapshot_json TEXT NOT NULL,
                archived_at TEXT NOT NULL,
                archive_reason TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_period_total_history_period "
            "ON period_total_history(project_id, period_id, history_id)",
            "CREATE INDEX IF NOT EXISTS idx_period_total_history_run "
            "ON period_total_history(project_id, run_id, history_id)",
            """CREATE TRIGGER IF NOT EXISTS trg_crosscheck_result_archive_update
               BEFORE UPDATE ON crosscheck_results
               BEGIN
                   INSERT INTO crosscheck_result_history(
                       project_id, source_id, period_id, run_signature, run_id,
                       snapshot_json, archived_at, archive_reason
                   ) VALUES (
                       OLD.project_id, OLD.id, OLD.period_id, OLD.run_signature, OLD.run_id,
                       json_object(
                           'id', OLD.id, 'project_id', OLD.project_id, 'period_id', OLD.period_id,
                           'verification_level', OLD.verification_level, 'status', OLD.status,
                           'path_a_total', OLD.path_a_total, 'path_b_total', OLD.path_b_total,
                           'raw_subtotal', OLD.raw_subtotal, 'diff_ab', OLD.diff_ab,
                           'control_diff', OLD.control_diff, 'ab_status', OLD.ab_status,
                           'control_status', OLD.control_status, 'detail_rows', OLD.detail_rows,
                           'excluded_subtotal_rows', OLD.excluded_subtotal_rows,
                           'excluded_title_rows', OLD.excluded_title_rows,
                           'pending_sheets', OLD.pending_sheets,
                           'range_unproven_sheets', OLD.range_unproven_sheets,
                           'classified_detail_rows', OLD.classified_detail_rows,
                           'business_rows_used', OLD.business_rows_used,
                           'coverage_proof_status', OLD.coverage_proof_status,
                           'ab_row_set_status', OLD.ab_row_set_status,
                           'ab_row_set_hash', OLD.ab_row_set_hash,
                           'ab_independence_level', OLD.ab_independence_level,
                           'c_control_source_json', OLD.c_control_source_json,
                           'c_control_evidence_id', OLD.c_control_evidence_id,
                           'notes_json', OLD.notes_json, 'evidence_id', OLD.evidence_id,
                           'checked_at', OLD.checked_at, 'run_signature', OLD.run_signature,
                           'run_id', OLD.run_id
                       ), datetime('now'), 'replaced'
                   );
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_crosscheck_result_archive_delete
               BEFORE DELETE ON crosscheck_results
               BEGIN
                   INSERT INTO crosscheck_result_history(
                       project_id, source_id, period_id, run_signature, run_id,
                       snapshot_json, archived_at, archive_reason
                   ) VALUES (
                       OLD.project_id, OLD.id, OLD.period_id, OLD.run_signature, OLD.run_id,
                       json_object(
                           'id', OLD.id, 'project_id', OLD.project_id, 'period_id', OLD.period_id,
                           'verification_level', OLD.verification_level, 'status', OLD.status,
                           'path_a_total', OLD.path_a_total, 'path_b_total', OLD.path_b_total,
                           'raw_subtotal', OLD.raw_subtotal, 'diff_ab', OLD.diff_ab,
                           'control_diff', OLD.control_diff, 'ab_status', OLD.ab_status,
                           'control_status', OLD.control_status, 'detail_rows', OLD.detail_rows,
                           'excluded_subtotal_rows', OLD.excluded_subtotal_rows,
                           'excluded_title_rows', OLD.excluded_title_rows,
                           'pending_sheets', OLD.pending_sheets,
                           'range_unproven_sheets', OLD.range_unproven_sheets,
                           'classified_detail_rows', OLD.classified_detail_rows,
                           'business_rows_used', OLD.business_rows_used,
                           'coverage_proof_status', OLD.coverage_proof_status,
                           'ab_row_set_status', OLD.ab_row_set_status,
                           'ab_row_set_hash', OLD.ab_row_set_hash,
                           'ab_independence_level', OLD.ab_independence_level,
                           'c_control_source_json', OLD.c_control_source_json,
                           'c_control_evidence_id', OLD.c_control_evidence_id,
                           'notes_json', OLD.notes_json, 'evidence_id', OLD.evidence_id,
                           'checked_at', OLD.checked_at, 'run_signature', OLD.run_signature,
                           'run_id', OLD.run_id
                       ), datetime('now'), 'deleted'
                   );
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_period_total_archive_update
               BEFORE UPDATE ON period_totals
               BEGIN
                   INSERT INTO period_total_history(
                       project_id, source_id, period_id, item_key, run_signature, run_id,
                       snapshot_json, archived_at, archive_reason
                   ) VALUES (
                       OLD.project_id, OLD.id, OLD.period_id, OLD.item_key,
                       OLD.run_signature, OLD.run_id,
                       json_object(
                           'id', OLD.id, 'project_id', OLD.project_id, 'period_id', OLD.period_id,
                           'item_key', OLD.item_key, 'qty_sum', OLD.qty_sum,
                           'amount_sum', OLD.amount_sum, 'wavg_price', OLD.wavg_price,
                           'cross_check_diff', OLD.cross_check_diff,
                           'cross_check_status', OLD.cross_check_status, 'evidence_id', OLD.evidence_id,
                           'raw_amount_sum', OLD.raw_amount_sum,
                           'calculated_amount_sum', OLD.calculated_amount_sum,
                           'calculated_amount_used_sum', OLD.calculated_amount_used_sum,
                           'effective_amount_sum', OLD.effective_amount_sum,
                           'amount_source', OLD.amount_source, 'amount_status', OLD.amount_status,
                           'ab_diff', OLD.ab_diff, 'ab_status', OLD.ab_status,
                           'control_diff', OLD.control_diff, 'control_status', OLD.control_status,
                           'verification_level', OLD.verification_level,
                           'run_signature', OLD.run_signature, 'run_id', OLD.run_id
                       ), datetime('now'), 'replaced'
                   );
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_period_total_archive_delete
               BEFORE DELETE ON period_totals
               BEGIN
                   INSERT INTO period_total_history(
                       project_id, source_id, period_id, item_key, run_signature, run_id,
                       snapshot_json, archived_at, archive_reason
                   ) VALUES (
                       OLD.project_id, OLD.id, OLD.period_id, OLD.item_key,
                       OLD.run_signature, OLD.run_id,
                       json_object(
                           'id', OLD.id, 'project_id', OLD.project_id, 'period_id', OLD.period_id,
                           'item_key', OLD.item_key, 'qty_sum', OLD.qty_sum,
                           'amount_sum', OLD.amount_sum, 'wavg_price', OLD.wavg_price,
                           'cross_check_diff', OLD.cross_check_diff,
                           'cross_check_status', OLD.cross_check_status, 'evidence_id', OLD.evidence_id,
                           'raw_amount_sum', OLD.raw_amount_sum,
                           'calculated_amount_sum', OLD.calculated_amount_sum,
                           'calculated_amount_used_sum', OLD.calculated_amount_used_sum,
                           'effective_amount_sum', OLD.effective_amount_sum,
                           'amount_source', OLD.amount_source, 'amount_status', OLD.amount_status,
                           'ab_diff', OLD.ab_diff, 'ab_status', OLD.ab_status,
                           'control_diff', OLD.control_diff, 'control_status', OLD.control_status,
                           'verification_level', OLD.verification_level,
                           'run_signature', OLD.run_signature, 'run_id', OLD.run_id
                       ), datetime('now'), 'deleted'
                   );
               END""",
        ],
    ),
    (
        # v20: 覆盖证明也必须绑定独立运行身份。
        # v13 仅保存 run_signature，无法区分同一输入签名下的不同不可变运行；
        # 新列与 Run Contract 的 run_id 一起使用，历史 NULL 行在当前读取面中
        # 继续 fail-closed，不得被误当作本次运行的覆盖证明。
        20,
        [
            "ALTER TABLE sheet_coverage_proofs ADD COLUMN run_id TEXT",
            "CREATE INDEX IF NOT EXISTS idx_sheet_coverage_proofs_run "
            "ON sheet_coverage_proofs(project_id, run_id, sheet_id, id)",
        ],
    ),
    (
        # v21: 收紧 Run Contract 失效时间，并隔离迁移前的旧校核 Evidence。
        # 仅允许 NULL → 合法、非空、可由 SQLite 解析的时间戳；空字符串或
        # 任意占位文本不能作为“已失效”证据。历史 cross_check 证据没有可靠
        # 运行身份时明确标记为 historical，不能因 v16 的默认 source 范围
        # 重新进入当前证据读取面。
        21,
        [
            "DROP TRIGGER IF EXISTS trg_run_contracts_immutable_update",
            """CREATE TRIGGER IF NOT EXISTS trg_run_contracts_immutable_update
               BEFORE UPDATE ON run_contracts
               WHEN NOT (
                   NEW.id IS OLD.id
                   AND NEW.run_id IS OLD.run_id
                   AND NEW.project_id IS OLD.project_id
                   AND NEW.signature IS OLD.signature
                   AND NEW.components_json IS OLD.components_json
                   AND NEW.created_at IS OLD.created_at
                   AND (
                       NEW.invalidated_at IS OLD.invalidated_at
                       OR (
                           OLD.invalidated_at IS NULL
                           AND typeof(NEW.invalidated_at)='text'
                           AND length(trim(NEW.invalidated_at)) > 0
                           AND datetime(NEW.invalidated_at) IS NOT NULL
                       )
                   )
               )
               BEGIN
                   SELECT RAISE(ABORT, 'run_contract immutable');
               END""",
            """UPDATE evidence
               SET scope='historical',
                   historical_reason='历史结果——当前数据或运行契约已经变化，不参与当前结论'
               WHERE kind='cross_check'
                 AND (run_signature IS NULL OR run_signature IN ('legacy:stale', 'run:invalidated')
                      OR run_id IS NULL)""",
        ],
    ),
    (
        # v22: 历史结果快照只允许追加，数据库层拒绝 UPDATE/DELETE，避免
        # 任何旧脚本或人工 SQL 改写/删除已归档的 A/B/C 与聚合事实。
        22,
        [
            """CREATE TRIGGER IF NOT EXISTS trg_crosscheck_result_history_immutable_update
               BEFORE UPDATE ON crosscheck_result_history
               BEGIN
                   SELECT RAISE(ABORT, 'crosscheck result history immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_crosscheck_result_history_immutable_delete
               BEFORE DELETE ON crosscheck_result_history
               BEGIN
                   SELECT RAISE(ABORT, 'crosscheck result history immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_period_total_history_immutable_update
               BEFORE UPDATE ON period_total_history
               BEGIN
                   SELECT RAISE(ABORT, 'period total history immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_period_total_history_immutable_delete
               BEFORE DELETE ON period_total_history
               BEGIN
                   SELECT RAISE(ABORT, 'period total history immutable');
               END""",
        ],
    ),
    (
        # v23: 清单差异雷达不可变快照。
        # 比较结果按当前 Run Contract 绑定；旧运行只保留为历史，不更新或删除。
        # 明细的基准/当前值、分类、来源和确认净影响均使用 JSON/Decimal 字符串
        # 保存，避免用浮点或再次读取变化后的源数据重建历史结论。
        23,
        [
            """CREATE TABLE IF NOT EXISTS diff_runs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                run_id TEXT NOT NULL,
                run_signature TEXT NOT NULL,
                baseline_period_id INTEGER NOT NULL REFERENCES settlement_periods(id),
                current_period_id INTEGER NOT NULL REFERENCES settlement_periods(id),
                baseline_direction TEXT NOT NULL,
                current_direction TEXT NOT NULL,
                status TEXT NOT NULL,
                reason_codes_json TEXT NOT NULL DEFAULT '[]',
                confirmed_net_amount_impact TEXT,
                evidence_id INTEGER REFERENCES evidence(id),
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                CHECK (baseline_period_id <> current_period_id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_diff_runs_current "
            "ON diff_runs(project_id, run_id, run_signature, created_at, id)",
            """CREATE TABLE IF NOT EXISTS diff_items (
                id INTEGER PRIMARY KEY,
                diff_run_id INTEGER NOT NULL REFERENCES diff_runs(id) ON DELETE CASCADE,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                identity_key TEXT NOT NULL,
                primary_category TEXT NOT NULL,
                categories_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                baseline_json TEXT NOT NULL DEFAULT '{}',
                current_json TEXT NOT NULL DEFAULT '{}',
                amount_impact TEXT,
                confirmed_amount_impact TEXT,
                baseline_sources_json TEXT NOT NULL DEFAULT '[]',
                current_sources_json TEXT NOT NULL DEFAULT '[]',
                evidence_id INTEGER REFERENCES evidence(id),
                created_at TEXT NOT NULL,
                UNIQUE(diff_run_id, identity_key)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_diff_items_run ON diff_items(diff_run_id, id)",
            """CREATE TRIGGER IF NOT EXISTS trg_diff_runs_immutable_update
               BEFORE UPDATE ON diff_runs
               BEGIN
                   SELECT RAISE(ABORT, 'diff run immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_diff_runs_immutable_delete
               BEFORE DELETE ON diff_runs
               BEGIN
                   SELECT RAISE(ABORT, 'diff run immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_diff_items_immutable_update
               BEFORE UPDATE ON diff_items
               BEGIN
                   SELECT RAISE(ABORT, 'diff item immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_diff_items_immutable_delete
               BEFORE DELETE ON diff_items
               BEGIN
                   SELECT RAISE(ABORT, 'diff item immutable');
               END""",
        ],
    ),
    (
        # v24: 字段映射模板库。
        # 模板是人工确认后的可复用候选，不属于当前清单事实；每条模板保存
        # 来源 Sheet、表头快照、列映射、创建人和版本。模板只能追加，推荐时
        # 必须由人工再次检查，不能未经确认直接套用。
        24,
        [
            """CREATE TABLE IF NOT EXISTS mapping_templates (
                id INTEGER PRIMARY KEY,
                project_id INTEGER REFERENCES projects(id),
                scope TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                template_name TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                header_signature TEXT NOT NULL,
                header_labels_json TEXT NOT NULL DEFAULT '[]',
                col_map_json TEXT NOT NULL,
                header_row_lo INTEGER NOT NULL,
                header_row_hi INTEGER NOT NULL,
                data_row_start INTEGER,
                data_row_end INTEGER,
                source_file_id INTEGER REFERENCES source_files(id),
                source_sheet_id INTEGER NOT NULL REFERENCES raw_sheets(id),
                source_reference_json TEXT NOT NULL DEFAULT '{}',
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                evidence_id INTEGER REFERENCES evidence(id),
                CHECK (scope IN ('sheet', 'project', 'global')),
                CHECK (length(trim(template_name)) > 0),
                CHECK (length(trim(scope_key)) > 0),
                CHECK (header_row_lo <= header_row_hi),
                CHECK (data_row_start IS NULL OR data_row_end IS NULL OR data_row_start <= data_row_end),
                CHECK ((scope='global' AND project_id IS NULL)
                       OR (scope IN ('sheet', 'project') AND project_id IS NOT NULL))
            )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_mapping_templates_version "
            "ON mapping_templates(scope, scope_key, template_name, version)",
            "CREATE INDEX IF NOT EXISTS idx_mapping_templates_project_scope "
            "ON mapping_templates(project_id, scope, scope_key, header_signature, id)",
            "CREATE INDEX IF NOT EXISTS idx_mapping_templates_header "
            "ON mapping_templates(header_signature, id)",
            """CREATE TRIGGER IF NOT EXISTS trg_mapping_templates_immutable_update
               BEFORE UPDATE ON mapping_templates
               BEGIN
                   SELECT RAISE(ABORT, 'mapping template immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_mapping_templates_immutable_delete
               BEFORE DELETE ON mapping_templates
               BEGIN
                   SELECT RAISE(ABORT, 'mapping template immutable');
               END""",
        ],
    ),
    (
        # v25: Finding 闭环状态与重复指纹历史。
        # 保留旧 ``status`` 作为兼容字段，新增生命周期状态和追加事件；同一
        # fingerprint 再次出现时保存历史处理摘要，但新发现默认回到“新发现”，
        # 绝不因为历史上已关闭而自动关闭。
        25,
        [
            "ALTER TABLE anomalies ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'new'",
            "ALTER TABLE anomalies ADD COLUMN lifecycle_updated_at TEXT",
            "ALTER TABLE anomalies ADD COLUMN lifecycle_updated_by TEXT",
            "ALTER TABLE anomalies ADD COLUMN repeat_history_json TEXT NOT NULL DEFAULT '[]'",
            """UPDATE anomalies
               SET lifecycle_status=CASE status
                   WHEN 'open' THEN 'new'
                   WHEN 'deferred' THEN 'pending_review'
                   WHEN 'verified_no_issue' THEN 'legitimate_business'
                   WHEN 'supplemented' THEN 'pending_data'
                   WHEN 'corrected' THEN 'rectified'
                   WHEN 'resolved' THEN 'closed'
                   ELSE 'historical'
               END
               WHERE lifecycle_status='new' AND status<>'open'""",
            "CREATE INDEX IF NOT EXISTS idx_anomalies_fingerprint_history "
            "ON anomalies(project_id, fingerprint, id)",
            """CREATE TABLE IF NOT EXISTS finding_status_events (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                anomaly_id INTEGER NOT NULL REFERENCES anomalies(id),
                finding_id TEXT,
                fingerprint TEXT,
                before_status TEXT NOT NULL,
                after_status TEXT NOT NULL,
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                run_signature TEXT,
                run_id TEXT,
                evidence_id INTEGER REFERENCES evidence(id),
                audit_id INTEGER REFERENCES audit_log(id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_finding_status_events_anomaly "
            "ON finding_status_events(project_id, anomaly_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_finding_status_events_fingerprint "
            "ON finding_status_events(project_id, fingerprint, id)",
            """CREATE TRIGGER IF NOT EXISTS trg_finding_status_events_immutable_update
               BEFORE UPDATE ON finding_status_events
               BEGIN
                   SELECT RAISE(ABORT, 'finding status event immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_finding_status_events_immutable_delete
               BEFORE DELETE ON finding_status_events
               BEGIN
                   SELECT RAISE(ABORT, 'finding status event immutable');
               END""",
        ],
    ),
    (
        # v26: 项目级异常规则目录配置历史。
        # 每次启停均追加一行并由 Run Contract 读取最新配置；旧配置和操作
        # 原因保留，关闭规则不会被解释成“没有问题”。
        26,
        [
            """CREATE TABLE IF NOT EXISTS rule_configurations (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                rule_id TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                config_version INTEGER NOT NULL,
                category TEXT NOT NULL,
                name_zh TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'project',
                severity TEXT NOT NULL,
                trigger_condition TEXT NOT NULL,
                evidence_requirements TEXT NOT NULL,
                impact_algorithm TEXT NOT NULL,
                limitations TEXT NOT NULL,
                suggested_review TEXT NOT NULL,
                allow_disable INTEGER NOT NULL CHECK (allow_disable IN (0, 1)),
                version TEXT NOT NULL,
                disabled_reason TEXT,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                evidence_id INTEGER REFERENCES evidence(id),
                audit_id INTEGER REFERENCES audit_log(id),
                UNIQUE(project_id, rule_id, config_version),
                CHECK (length(trim(rule_id)) > 0),
                CHECK (length(trim(actor)) > 0)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_rule_configurations_current "
            "ON rule_configurations(project_id, rule_id, config_version DESC)",
            """CREATE TRIGGER IF NOT EXISTS trg_rule_configurations_immutable_update
               BEFORE UPDATE ON rule_configurations
               BEGIN
                   SELECT RAISE(ABORT, 'rule configuration immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_rule_configurations_immutable_delete
               BEFORE DELETE ON rule_configurations
               BEGIN
                   SELECT RAISE(ABORT, 'rule configuration immutable');
               END""",
        ],
    ),
    (
        # v27: 本地别名/匹配知识库。
        # ``item_aliases`` 保留为旧版本兼容读取；新知识库把原始名称、规范
        # 名称、适用项目/专业、方向、版本和撤销状态作为追加快照保存。撤销
        # 不 UPDATE 旧行，而是追加一条 revoked 版本并记录不可变事件，避免
        # 一次项目确认永久污染全部历史项目。
        27,
        [
            """CREATE TABLE IF NOT EXISTS alias_knowledge (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                scope TEXT NOT NULL,
                applicable_project_id INTEGER REFERENCES projects(id),
                profession TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL DEFAULT 'unknown',
                original_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                canonical_key TEXT NOT NULL,
                canonical_name TEXT NOT NULL DEFAULT '',
                mapping_basis TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                created_by TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                revoked_by TEXT,
                revoked_at TEXT,
                revoke_reason TEXT,
                supersedes_id INTEGER REFERENCES alias_knowledge(id),
                evidence_id INTEGER REFERENCES evidence(id),
                audit_id INTEGER REFERENCES audit_log(id),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                CHECK (scope IN ('project', 'global')),
                CHECK (status IN ('active', 'revoked')),
                CHECK (version >= 1),
                CHECK ((scope='global' AND applicable_project_id IS NULL)
                       OR (scope='project' AND applicable_project_id IS NOT NULL)),
                CHECK (status='active' OR (
                       revoked_by IS NOT NULL AND revoked_at IS NOT NULL
                       AND revoke_reason IS NOT NULL AND length(trim(revoke_reason)) > 0
                )),
                CHECK (length(trim(original_name)) > 0),
                CHECK (length(trim(normalized_name)) > 0),
                CHECK (length(trim(canonical_key)) > 0),
                CHECK (length(trim(mapping_basis)) > 0),
                CHECK (length(trim(created_by)) > 0)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_alias_knowledge_lookup "
            "ON alias_knowledge(scope, applicable_project_id, direction, profession, normalized_name, version DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_alias_knowledge_project "
            "ON alias_knowledge(project_id, applicable_project_id, status, id)",
            "CREATE INDEX IF NOT EXISTS idx_alias_knowledge_canonical "
            "ON alias_knowledge(canonical_key, direction, id)",
            """CREATE TABLE IF NOT EXISTS alias_knowledge_events (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                alias_id INTEGER NOT NULL REFERENCES alias_knowledge(id),
                event_type TEXT NOT NULL,
                before_status TEXT,
                after_status TEXT NOT NULL,
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                run_id TEXT,
                run_signature TEXT,
                evidence_id INTEGER REFERENCES evidence(id),
                audit_id INTEGER REFERENCES audit_log(id),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                CHECK (length(trim(reason)) > 0),
                CHECK (length(trim(actor)) > 0)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_alias_knowledge_events_alias "
            "ON alias_knowledge_events(project_id, alias_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_alias_knowledge_events_lookup "
            "ON alias_knowledge_events(project_id, occurred_at, id)",
            """CREATE TRIGGER IF NOT EXISTS trg_alias_knowledge_immutable_update
               BEFORE UPDATE ON alias_knowledge
               BEGIN
                   SELECT RAISE(ABORT, 'alias knowledge immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_alias_knowledge_immutable_delete
               BEFORE DELETE ON alias_knowledge
               BEGIN
                   SELECT RAISE(ABORT, 'alias knowledge immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_alias_knowledge_events_immutable_update
               BEFORE UPDATE ON alias_knowledge_events
               BEGIN
                   SELECT RAISE(ABORT, 'alias knowledge event immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_alias_knowledge_events_immutable_delete
               BEFORE DELETE ON alias_knowledge_events
               BEGIN
                   SELECT RAISE(ABORT, 'alias knowledge event immutable');
               END""",
            # Finding 本体是当前快照，状态可由受控 API 或系统重跑变更；但
            # 指纹、原始数值、运行身份和重复历史一旦写入不得被旧脚本覆盖。
            """CREATE TRIGGER IF NOT EXISTS trg_anomalies_snapshot_immutable_update
               BEFORE UPDATE ON anomalies
               WHEN NOT (
                   NEW.id IS OLD.id
                   AND NEW.project_id IS OLD.project_id
                   AND NEW.rule_id IS OLD.rule_id
                   AND NEW.severity IS OLD.severity
                   AND NEW.subject_type IS OLD.subject_type
                   AND NEW.subject_id IS OLD.subject_id
                   AND NEW.evidence_id IS OLD.evidence_id
                   AND NEW.message IS OLD.message
                   AND NEW.created_at IS OLD.created_at
                   AND (
                       (NEW.run_signature IS OLD.run_signature
                        AND NEW.run_id IS OLD.run_id)
                       OR (
                           OLD.run_signature IS NULL AND OLD.run_id IS NULL
                           AND NEW.run_signature IS NOT NULL AND NEW.run_id IS NOT NULL
                           AND EXISTS (
                               SELECT 1 FROM run_contracts rc
                               WHERE rc.project_id=OLD.project_id
                                 AND rc.run_id=NEW.run_id
                                 AND rc.signature=NEW.run_signature
                                 AND rc.invalidated_at IS NULL
                           )
                       )
                   )
                   AND NEW.finding_id IS OLD.finding_id
                   AND NEW.fingerprint IS OLD.fingerprint
                   AND NEW.detection_mode IS OLD.detection_mode
                   AND NEW.raw_values_json IS OLD.raw_values_json
                   AND NEW.normalized_values_json IS OLD.normalized_values_json
                   AND NEW.impact IS OLD.impact
                   AND NEW.limitations_json IS OLD.limitations_json
                   AND NEW.recommendation IS OLD.recommendation
                   AND NEW.suppression_reason IS OLD.suppression_reason
                   AND NEW.repeat_history_json IS OLD.repeat_history_json
               )
               BEGIN
                   SELECT RAISE(ABORT, 'anomaly snapshot immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_anomalies_lifecycle_guard
               BEFORE UPDATE ON anomalies
               WHEN (
                   NEW.lifecycle_status IS NOT OLD.lifecycle_status
                   OR NEW.status IS NOT OLD.status
               )
               AND (
                   NEW.lifecycle_status IS OLD.lifecycle_status
                   OR NEW.lifecycle_updated_at IS NULL
                   OR length(trim(COALESCE(NEW.lifecycle_updated_by, ''))) = 0
                   OR (
                       NEW.lifecycle_status <> 'new'
                       AND length(trim(COALESCE(NEW.resolved_note, ''))) = 0
                   )
                   OR (NEW.status='resolved' AND NEW.lifecycle_status<>'closed')
                   OR (NEW.status='corrected' AND NEW.lifecycle_status<>'rectified')
                   OR (NEW.status='verified_no_issue' AND NEW.lifecycle_status<>'legitimate_business')
                   OR (NEW.status='deferred' AND NEW.lifecycle_status NOT IN ('pending_review', 'pending_data'))
                   OR (NEW.status='stale' AND NEW.lifecycle_status<>'historical')
               )
               BEGIN
                   SELECT RAISE(ABORT, 'finding lifecycle requires actor, time and reason');
               END""",
        ],
    ),
    (
        # v28: Finding 生命周期必须由 Evidence/Audit/不可变状态事件共同证明。
        # v27 已经限制了操作人、时间、原因和旧 status/lifecycle_status 的
        # 对应关系；本迁移再加一层数据库级证据闸门，防止旧脚本直接 UPDATE
        # anomalies 绕过应用 API。系统重跑将旧快照转为 historical，属于
        # 结果保留流程，不应被当作人工处理，故明确排除 historical。
        28,
        [
            """CREATE TRIGGER IF NOT EXISTS trg_anomalies_lifecycle_evidence_guard
               BEFORE UPDATE ON anomalies
               WHEN (
                   (NEW.lifecycle_status IS NOT OLD.lifecycle_status
                    OR NEW.status IS NOT OLD.status)
                   AND NEW.lifecycle_status <> 'historical'
                   AND NOT EXISTS (
                       SELECT 1
                       FROM finding_status_events fse
                       JOIN evidence ev
                         ON ev.id=fse.evidence_id
                        AND ev.project_id=OLD.project_id
                       JOIN audit_log al
                         ON al.id=fse.audit_id
                        AND al.project_id=OLD.project_id
                       WHERE fse.project_id=OLD.project_id
                         AND fse.anomaly_id=OLD.id
                         AND fse.after_status=NEW.lifecycle_status
                         AND fse.reason=COALESCE(NEW.resolved_note, '')
                         AND fse.actor=COALESCE(NEW.lifecycle_updated_by, '')
                         AND fse.occurred_at=COALESCE(NEW.lifecycle_updated_at, '')
                         AND fse.evidence_id IS NOT NULL
                         AND fse.audit_id IS NOT NULL
                   )
               )
               BEGIN
                   SELECT RAISE(ABORT, 'finding lifecycle requires evidence, audit and event');
               END""",
        ],
    ),
    (
        # v29: 别名事件的业务内容保持不可变，但允许在同一人工操作的
        # 最终 Run Contract 确定后，仅重绑 run_id/run_signature。v27 的
        # 全字段禁止 UPDATE 会把“别名写入先切换、后续确认审计再切换”的
        # 原子操作卡在历史运行；本迁移把可变范围严格收窄为当前合同身份。
        29,
        [
            "DROP TRIGGER IF EXISTS trg_alias_knowledge_events_immutable_update",
            """CREATE TRIGGER IF NOT EXISTS trg_alias_knowledge_events_immutable_update
               BEFORE UPDATE ON alias_knowledge_events
               WHEN NOT (
                   NEW.id IS OLD.id
                   AND NEW.project_id IS OLD.project_id
                   AND NEW.alias_id IS OLD.alias_id
                   AND NEW.event_type IS OLD.event_type
                   AND NEW.before_status IS OLD.before_status
                   AND NEW.after_status IS OLD.after_status
                   AND NEW.reason IS OLD.reason
                   AND NEW.actor IS OLD.actor
                   AND NEW.occurred_at IS OLD.occurred_at
                   AND NEW.evidence_id IS OLD.evidence_id
                   AND NEW.audit_id IS OLD.audit_id
                   AND NEW.metadata_json IS OLD.metadata_json
                   AND (
                       (NEW.run_id IS OLD.run_id
                        AND NEW.run_signature IS OLD.run_signature)
                       OR (
                           NEW.run_id IS NOT NULL
                           AND NEW.run_signature IS NOT NULL
                           AND EXISTS (
                               SELECT 1 FROM run_contracts rc
                               WHERE rc.project_id=OLD.project_id
                                 AND rc.run_id=NEW.run_id
                                 AND rc.signature=NEW.run_signature
                                 AND rc.invalidated_at IS NULL
                           )
                           AND EXISTS (
                               SELECT 1
                               FROM alias_knowledge ak
                               JOIN audit_log al ON al.id=ak.audit_id
                               WHERE ak.id=OLD.alias_id
                                 AND al.project_id=OLD.project_id
                                 AND al.run_id=NEW.run_id
                                 AND al.run_signature=NEW.run_signature
                           )
                       )
                   )
               )
               BEGIN
                   SELECT RAISE(ABORT, 'alias knowledge event immutable');
               END""",
        ],
    ),
    (
        # v30: 收紧 Finding 闭环的关联证明。v28 只检查了“有一条事件、
        # 有一条同项目 Evidence/Audit”，仍可能用无关 source 记录拼接出
        # 伪事件；现在必须同时命中 Finding 身份、旧/新状态、运行身份、
        # Evidence 类型/归属和 Audit action/target。
        30,
        [
            "DROP TRIGGER IF EXISTS trg_anomalies_lifecycle_evidence_guard",
            """CREATE TRIGGER IF NOT EXISTS trg_anomalies_lifecycle_evidence_guard
               BEFORE UPDATE ON anomalies
               WHEN (
                   (NEW.lifecycle_status IS NOT OLD.lifecycle_status
                    OR NEW.status IS NOT OLD.status)
                   AND NEW.lifecycle_status <> 'historical'
                   AND NOT EXISTS (
                       SELECT 1
                       FROM finding_status_events fse
                       JOIN evidence ev
                         ON ev.id=fse.evidence_id
                        AND ev.project_id=OLD.project_id
                       JOIN audit_log al
                         ON al.id=fse.audit_id
                        AND al.project_id=OLD.project_id
                       WHERE fse.project_id=OLD.project_id
                         AND fse.anomaly_id=OLD.id
                         AND fse.finding_id IS OLD.finding_id
                         AND fse.fingerprint IS OLD.fingerprint
                         AND fse.before_status=COALESCE(OLD.lifecycle_status, 'new')
                         AND fse.after_status=NEW.lifecycle_status
                         AND fse.reason=COALESCE(NEW.resolved_note, '')
                         AND fse.actor=COALESCE(NEW.lifecycle_updated_by, '')
                         AND fse.occurred_at=COALESCE(NEW.lifecycle_updated_at, '')
                         AND fse.run_id IS OLD.run_id
                         AND fse.run_signature IS OLD.run_signature
                         AND fse.evidence_id IS NOT NULL
                         AND fse.audit_id IS NOT NULL
                         AND ev.kind='finding_status_change'
                         AND ev.scope='human'
                         AND ev.finding_id IS OLD.finding_id
                         AND ev.run_id IS fse.run_id
                         AND ev.run_signature IS fse.run_signature
                         AND al.action='update_finding_status'
                         AND al.target='anomaly:' || CAST(OLD.id AS TEXT)
                         AND al.run_id IS fse.run_id
                         AND al.run_signature IS fse.run_signature
                   )
               )
               BEGIN
                   SELECT RAISE(ABORT, 'finding lifecycle requires evidence, audit and linked event');
               END""",
        ],
    ),
    (
        # v31: 项目级别名的拥有项目与适用项目必须相同。应用 API 已拒绝
        # 跨项目写入，但旧脚本仍可能直接 INSERT 一条 ``project_id=P1,
        # applicable_project_id=P2`` 的行；读取面也再次要求两者相等，确保
        # 迁移前异常数据不能让 P2 获得未经授权的项目知识。
        31,
        [
            """CREATE TRIGGER IF NOT EXISTS trg_alias_knowledge_project_scope_guard
               BEFORE INSERT ON alias_knowledge
               WHEN NEW.scope='project'
                AND NEW.project_id IS NOT NEW.applicable_project_id
               BEGIN
                   SELECT RAISE(ABORT, 'project alias owner mismatch');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_alias_knowledge_event_project_scope_guard
               BEFORE INSERT ON alias_knowledge_events
               WHEN EXISTS (
                   SELECT 1 FROM alias_knowledge ak
                    WHERE ak.id=NEW.alias_id
                      AND ak.project_id IS NOT NEW.project_id
               )
               BEGIN
                   SELECT RAISE(ABORT, 'alias knowledge event project mismatch');
               END""",
        ],
    ),
    (
        # v32: 项目送审/补充/复审/审定版本链。版本与清单快照只允许追加，
        # 运行身份和 Evidence/Audit 绑定可以在同一人工事务的最终合同确定
        # 后收口；旧版本不得被覆盖或删除。
        32,
        [
            """CREATE TABLE IF NOT EXISTS project_versions (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                version_no INTEGER NOT NULL,
                version_kind TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                source_manifest_id INTEGER REFERENCES import_manifests(id),
                snapshot_sha256 TEXT NOT NULL,
                item_count INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                run_id TEXT,
                run_signature TEXT,
                evidence_id INTEGER REFERENCES evidence(id),
                audit_id INTEGER REFERENCES audit_log(id),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(project_id, version_no),
                CHECK (version_no >= 1),
                CHECK (length(trim(version_kind)) > 0),
                CHECK (length(trim(title)) > 0),
                CHECK (length(trim(created_by)) > 0),
                CHECK (length(trim(reason)) > 0)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_project_versions_project "
            "ON project_versions(project_id, version_no DESC, id DESC)",
            """CREATE TABLE IF NOT EXISTS project_version_items (
                id INTEGER PRIMARY KEY,
                version_id INTEGER NOT NULL REFERENCES project_versions(id),
                project_id INTEGER NOT NULL REFERENCES projects(id),
                identity_key TEXT NOT NULL,
                occurrence INTEGER NOT NULL,
                period_id INTEGER,
                period_no INTEGER,
                direction TEXT NOT NULL DEFAULT 'unknown',
                line_item_id INTEGER,
                code TEXT,
                name TEXT,
                feature TEXT,
                unit TEXT,
                quantity TEXT,
                unit_price TEXT,
                amount TEXT,
                flags_json TEXT NOT NULL DEFAULT '{}',
                source_file_id INTEGER,
                source_sheet_id INTEGER,
                source_row INTEGER,
                source_evidence_id INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(version_id, identity_key, occurrence)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_project_version_items_version "
            "ON project_version_items(version_id, identity_key, occurrence, id)",
            "CREATE INDEX IF NOT EXISTS idx_project_version_items_project "
            "ON project_version_items(project_id, period_id, id)",
            """CREATE TRIGGER IF NOT EXISTS trg_project_version_items_immutable_update
               BEFORE UPDATE ON project_version_items
               BEGIN
                   SELECT RAISE(ABORT, 'project version item immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_project_version_items_immutable_delete
               BEFORE DELETE ON project_version_items
               BEGIN
                   SELECT RAISE(ABORT, 'project version item immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_project_versions_immutable_delete
               BEFORE DELETE ON project_versions
               BEGIN
                   SELECT RAISE(ABORT, 'project version immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_project_versions_immutable_update
               BEFORE UPDATE ON project_versions
               WHEN NOT (
                   NEW.id IS OLD.id
                   AND NEW.project_id IS OLD.project_id
                   AND NEW.version_no IS OLD.version_no
                   AND NEW.version_kind IS OLD.version_kind
                   AND NEW.title IS OLD.title
                   AND NEW.description IS OLD.description
                   AND NEW.source_manifest_id IS OLD.source_manifest_id
                   AND NEW.snapshot_sha256 IS OLD.snapshot_sha256
                   AND NEW.item_count IS OLD.item_count
                   AND NEW.created_by IS OLD.created_by
                   AND NEW.created_at IS OLD.created_at
                   AND NEW.reason IS OLD.reason
                   AND NEW.metadata_json IS OLD.metadata_json
                   AND (
                       (NEW.run_id IS OLD.run_id AND NEW.run_signature IS OLD.run_signature)
                       OR (
                           NEW.run_id IS NOT NULL AND NEW.run_signature IS NOT NULL
                           AND EXISTS (
                               SELECT 1 FROM run_contracts rc
                                WHERE rc.project_id=OLD.project_id
                                  AND rc.run_id=NEW.run_id
                                  AND rc.signature=NEW.run_signature
                                  AND rc.invalidated_at IS NULL
                           )
                       )
                   )
                   AND (
                       NEW.evidence_id IS OLD.evidence_id
                       OR (
                           OLD.evidence_id IS NULL AND NEW.evidence_id IS NOT NULL
                           AND EXISTS (
                               SELECT 1 FROM evidence ev
                                WHERE ev.id=NEW.evidence_id
                                  AND ev.project_id=OLD.project_id
                           )
                       )
                   )
                   AND (
                       NEW.audit_id IS OLD.audit_id
                       OR (
                           OLD.audit_id IS NULL AND NEW.audit_id IS NOT NULL
                           AND EXISTS (
                               SELECT 1 FROM audit_log al
                                WHERE al.id=NEW.audit_id
                                  AND al.project_id=OLD.project_id
                           )
                       )
                   )
               )
               BEGIN
                   SELECT RAISE(ABORT, 'project version immutable');
               END""",
        ],
    ),
    (
        # v33: 历史综合单价资产与项目关闭闸门。
        # 历史价格只能从显式关闭、绑定最终审定版本且已有完整 Evidence/Audit
        # 的项目中追加；价格观察本身只作复核提示，不参与当前项目结论。所有
        # 来源、维度和 Decimal 文本均保留为不可变快照，避免项目后续重跑覆盖
        # 或把缺失资料静默补成零。
        33,
        [
            """CREATE TABLE IF NOT EXISTS project_closures (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                final_version_id INTEGER NOT NULL REFERENCES project_versions(id),
                status TEXT NOT NULL DEFAULT 'closed',
                region TEXT NOT NULL DEFAULT '',
                project_type TEXT NOT NULL DEFAULT '',
                observed_at TEXT,
                closed_by TEXT NOT NULL,
                closed_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                run_id TEXT NOT NULL,
                run_signature TEXT NOT NULL,
                evidence_id INTEGER REFERENCES evidence(id),
                audit_id INTEGER REFERENCES audit_log(id),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(project_id),
                CHECK (status='closed'),
                CHECK (length(trim(closed_by)) > 0),
                CHECK (length(trim(reason)) > 0)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_project_closures_version "
            "ON project_closures(project_id, final_version_id, id)",
            """CREATE TRIGGER IF NOT EXISTS trg_project_closures_insert_guard
               BEFORE INSERT ON project_closures
               WHEN NEW.status <> 'closed'
                 OR NEW.run_id IS NULL OR NEW.run_signature IS NULL
                 OR NOT EXISTS (
                       SELECT 1 FROM project_versions pv
                        WHERE pv.id=NEW.final_version_id
                          AND pv.project_id=NEW.project_id
                          AND pv.version_kind='final_approval'
                   )
                 OR NOT EXISTS (
                       SELECT 1 FROM run_contracts rc
                        WHERE rc.project_id=NEW.project_id
                          AND rc.run_id=NEW.run_id
                          AND rc.signature=NEW.run_signature
                          AND rc.invalidated_at IS NULL
                   )
               BEGIN
                   SELECT RAISE(ABORT, 'project closure requires final approval and current run');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_project_closures_immutable_update
               BEFORE UPDATE ON project_closures
               WHEN NOT (
                   NEW.id IS OLD.id
                   AND NEW.project_id IS OLD.project_id
                   AND NEW.final_version_id IS OLD.final_version_id
                   AND NEW.status IS OLD.status
                   AND NEW.region IS OLD.region
                   AND NEW.project_type IS OLD.project_type
                   AND NEW.observed_at IS OLD.observed_at
                   AND NEW.closed_by IS OLD.closed_by
                   AND NEW.closed_at IS OLD.closed_at
                   AND NEW.reason IS OLD.reason
                   AND NEW.run_id IS OLD.run_id
                   AND NEW.run_signature IS OLD.run_signature
                   AND NEW.metadata_json IS OLD.metadata_json
                   AND (
                       NEW.evidence_id IS OLD.evidence_id
                       OR (
                           OLD.evidence_id IS NULL AND NEW.evidence_id IS NOT NULL
                           AND EXISTS (
                               SELECT 1 FROM evidence ev
                                WHERE ev.id=NEW.evidence_id
                                  AND ev.project_id=OLD.project_id
                           )
                       )
                   )
                   AND (
                       NEW.audit_id IS OLD.audit_id
                       OR (
                           OLD.audit_id IS NULL AND NEW.audit_id IS NOT NULL
                           AND EXISTS (
                               SELECT 1 FROM audit_log al
                                WHERE al.id=NEW.audit_id
                                  AND al.project_id=OLD.project_id
                           )
                       )
                   )
               )
               BEGIN
                   SELECT RAISE(ABORT, 'project closure immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_project_closures_immutable_delete
               BEFORE DELETE ON project_closures
               BEGIN
                   SELECT RAISE(ABORT, 'project closure immutable');
               END""",
            """CREATE TABLE IF NOT EXISTS historical_unit_prices (
                id INTEGER PRIMARY KEY,
                source_project_id INTEGER NOT NULL REFERENCES projects(id),
                closure_id INTEGER NOT NULL REFERENCES project_closures(id),
                source_version_id INTEGER NOT NULL REFERENCES project_versions(id),
                source_version_item_id INTEGER NOT NULL REFERENCES project_version_items(id),
                raw_project_name TEXT NOT NULL,
                normalized_project_name TEXT NOT NULL,
                raw_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                feature TEXT NOT NULL DEFAULT '',
                normalized_feature TEXT NOT NULL DEFAULT '',
                unit TEXT NOT NULL DEFAULT '',
                normalized_unit TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '',
                observed_at TEXT,
                project_type TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL DEFAULT 'unknown',
                unit_price TEXT NOT NULL,
                quantity TEXT,
                amount TEXT,
                source_file_id INTEGER,
                source_sheet_id INTEGER,
                source_row INTEGER,
                source_evidence_id INTEGER,
                evidence_id INTEGER NOT NULL REFERENCES evidence(id),
                audit_id INTEGER NOT NULL REFERENCES audit_log(id),
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(closure_id, source_version_item_id),
                CHECK (length(trim(raw_project_name)) > 0),
                CHECK (length(trim(normalized_project_name)) > 0),
                CHECK (length(trim(raw_name)) > 0),
                CHECK (length(trim(normalized_name)) > 0),
                CHECK (length(trim(unit_price)) > 0),
                CHECK (direction IN ('upward', 'downward', 'unknown')),
                CHECK (status IN ('active', 'revoked')),
                CHECK (length(trim(created_by)) > 0)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_historical_unit_prices_lookup "
            "ON historical_unit_prices(normalized_name, normalized_feature, normalized_unit, "
            "direction, status, id)",
            "CREATE INDEX IF NOT EXISTS idx_historical_unit_prices_project "
            "ON historical_unit_prices(source_project_id, source_version_id, id)",
            """CREATE TRIGGER IF NOT EXISTS trg_historical_unit_prices_insert_guard
               BEFORE INSERT ON historical_unit_prices
               WHEN NOT EXISTS (
                       SELECT 1
                         FROM project_closures pc
                         JOIN project_versions pv
                           ON pv.id=pc.final_version_id
                          AND pv.project_id=pc.project_id
                          AND pv.version_kind='final_approval'
                         JOIN project_version_items pvi
                           ON pvi.id=NEW.source_version_item_id
                          AND pvi.version_id=pv.id
                          AND pvi.project_id=pc.project_id
                        WHERE pc.id=NEW.closure_id
                          AND pc.project_id=NEW.source_project_id
                          AND pc.final_version_id=NEW.source_version_id
                          AND pc.status='closed'
                          AND pc.evidence_id IS NOT NULL
                          AND pc.audit_id IS NOT NULL
                   )
                 OR NEW.evidence_id IS NULL
                 OR NEW.audit_id IS NULL
                 OR NOT EXISTS (
                       SELECT 1 FROM evidence ev
                        WHERE ev.id=NEW.evidence_id
                          AND ev.project_id=NEW.source_project_id
                          AND ev.kind='historical_unit_price'
                          AND ev.scope='historical'
                   )
                 OR NOT EXISTS (
                       SELECT 1 FROM audit_log al
                        WHERE al.id=NEW.audit_id
                          AND al.project_id=NEW.source_project_id
                   )
               BEGIN
                   SELECT RAISE(ABORT, 'historical unit price requires closed project evidence');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_historical_unit_prices_immutable_update
               BEFORE UPDATE ON historical_unit_prices
               BEGIN
                   SELECT RAISE(ABORT, 'historical unit price immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_historical_unit_prices_immutable_delete
               BEFORE DELETE ON historical_unit_prices
               BEGIN
                   SELECT RAISE(ABORT, 'historical unit price immutable');
               END""",
        ],
    ),
    (
        # v34: 版本快照和历史证据的数据库级归属边界。
        # 应用 API 已经执行同样的校验；这里再挡住旧脚本/手工 SQL 的
        # 跨项目写入，避免一条错误快照把别的项目清单或来源 Evidence
        # 带进当前版本。历史 Evidence 一旦退出 current 只能继续留在
        # historical，不允许通过 UPDATE 复活成当前证据。
        34,
        [
            """CREATE TRIGGER IF NOT EXISTS trg_project_versions_insert_guard
               BEFORE INSERT ON project_versions
               WHEN NEW.version_kind NOT IN (
                        'initial_submission', 'supplement',
                        'resubmission', 'final_approval'
                    )
                 OR (
                       NEW.source_manifest_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM import_manifests im
                            WHERE im.id=NEW.source_manifest_id
                              AND im.project_id=NEW.project_id
                       )
                    )
                 OR (
                       (NEW.run_id IS NOT NULL OR NEW.run_signature IS NOT NULL)
                       AND NOT EXISTS (
                           SELECT 1 FROM run_contracts rc
                            WHERE rc.project_id=NEW.project_id
                              AND rc.run_id=NEW.run_id
                              AND rc.signature=NEW.run_signature
                              AND rc.invalidated_at IS NULL
                       )
                    )
               BEGIN
                   SELECT RAISE(ABORT, 'project version source or run mismatch');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_project_version_items_insert_guard
               BEFORE INSERT ON project_version_items
               WHEN NEW.direction NOT IN ('upward', 'downward', 'unknown')
                 OR NOT EXISTS (
                       SELECT 1 FROM project_versions pv
                        WHERE pv.id=NEW.version_id
                          AND pv.project_id=NEW.project_id
                   )
                 OR (
                       NEW.line_item_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1
                             FROM line_items li
                             JOIN settlement_periods sp ON sp.id=li.period_id
                            WHERE li.id=NEW.line_item_id
                              AND sp.project_id=NEW.project_id
                              AND (NEW.period_id IS NULL OR NEW.period_id=li.period_id)
                       )
                    )
                 OR (
                       NEW.source_file_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM source_files sf
                            WHERE sf.id=NEW.source_file_id
                              AND sf.project_id=NEW.project_id
                       )
                    )
                 OR (
                       NEW.source_sheet_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1
                             FROM raw_sheets rs
                             JOIN parse_batches pb ON pb.id=rs.batch_id
                             JOIN source_files sf ON sf.id=pb.file_id
                            WHERE rs.id=NEW.source_sheet_id
                              AND sf.project_id=NEW.project_id
                              AND (NEW.source_file_id IS NULL OR sf.id=NEW.source_file_id)
                       )
                    )
                 OR (
                       NEW.source_evidence_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM evidence ev
                            WHERE ev.id=NEW.source_evidence_id
                              AND ev.project_id=NEW.project_id
                       )
                    )
               BEGIN
                   SELECT RAISE(ABORT, 'project version item source mismatch');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_evidence_historical_immutable_scope
               BEFORE UPDATE ON evidence
               WHEN OLD.scope='historical' AND NEW.scope<>'historical'
               BEGIN
                   SELECT RAISE(ABORT, 'historical evidence cannot be revived');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_evidence_historical_immutable_delete
               BEFORE DELETE ON evidence
               WHEN OLD.scope='historical'
               BEGIN
                   SELECT RAISE(ABORT, 'historical evidence cannot be deleted');
               END""",
        ],
    ),
    (
        # v35: 收口“版本—Evidence—Audit—Run Contract”同一责任链，并校验
        # 快照期次直接归属当前项目。v34 已挡跨项目来源；本版继续拒绝
        # 同项目但拿另一运行的 Evidence/Audit 拼接到新版本的情况。
        35,
        [
            """CREATE TRIGGER IF NOT EXISTS trg_project_versions_evidence_chain_guard
               BEFORE INSERT ON project_versions
               WHEN (
                       NEW.evidence_id IS NOT NULL
                       OR NEW.audit_id IS NOT NULL
                     )
                 AND (
                       NEW.evidence_id IS NULL
                       OR NEW.audit_id IS NULL
                       OR NOT EXISTS (
                           SELECT 1 FROM evidence ev
                            WHERE ev.id=NEW.evidence_id
                              AND ev.project_id=NEW.project_id
                              AND ev.kind='project_version'
                       )
                       OR NOT EXISTS (
                           SELECT 1 FROM audit_log al
                            WHERE al.id=NEW.audit_id
                              AND al.project_id=NEW.project_id
                       )
                       OR NEW.run_id IS NULL
                       OR NEW.run_signature IS NULL
                       OR NOT EXISTS (
                           SELECT 1 FROM evidence ev
                            WHERE ev.id=NEW.evidence_id
                              AND ev.run_id=NEW.run_id
                              AND ev.run_signature=NEW.run_signature
                       )
                       OR NOT EXISTS (
                           SELECT 1 FROM audit_log al
                            WHERE al.id=NEW.audit_id
                              AND al.run_id=NEW.run_id
                              AND al.run_signature=NEW.run_signature
                       )
                     )
               BEGIN
                   SELECT RAISE(ABORT, 'project version evidence chain mismatch');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_project_versions_evidence_chain_update_guard
               BEFORE UPDATE ON project_versions
               WHEN (
                       NEW.evidence_id IS NOT NULL
                       OR NEW.audit_id IS NOT NULL
                     )
                 AND (
                       NEW.evidence_id IS NULL
                       OR NEW.audit_id IS NULL
                       OR NOT EXISTS (
                           SELECT 1 FROM evidence ev
                            WHERE ev.id=NEW.evidence_id
                              AND ev.project_id=NEW.project_id
                              AND ev.kind='project_version'
                              AND ev.run_id=NEW.run_id
                              AND ev.run_signature=NEW.run_signature
                       )
                       OR NOT EXISTS (
                           SELECT 1 FROM audit_log al
                            WHERE al.id=NEW.audit_id
                              AND al.project_id=NEW.project_id
                              AND al.run_id=NEW.run_id
                              AND al.run_signature=NEW.run_signature
                       )
                     )
               BEGIN
                   SELECT RAISE(ABORT, 'project version evidence chain mismatch');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_project_version_items_period_scope_guard
               BEFORE INSERT ON project_version_items
               WHEN NEW.period_id IS NOT NULL
                AND NOT EXISTS (
                       SELECT 1 FROM settlement_periods sp
                        WHERE sp.id=NEW.period_id
                          AND sp.project_id=NEW.project_id
                          AND COALESCE(sp.period_no, -1)=COALESCE(NEW.period_no, -1)
                          AND COALESCE(sp.direction, 'unknown')=COALESCE(NEW.direction, 'unknown')
                   )
               BEGIN
                   SELECT RAISE(ABORT, 'project version item period mismatch');
               END""",
        ],
    ),
    (
        # v36: Sheet 覆盖证明和逐行分类是 P0-01 的不可变事实快照。旧版只在
        # API 层约定“追加不更新”，手工 SQL 仍可改状态、计数、方向或删行，
        # 进而让摘要看到被篡改的 complete 证明。本版补数据库级归属、证据
        # 和不可变触发器；修正后的证明只能追加新快照。
        36,
        [
            """CREATE TRIGGER IF NOT EXISTS trg_sheet_coverage_proofs_insert_guard
               BEFORE INSERT ON sheet_coverage_proofs
               WHEN NOT EXISTS (
                       SELECT 1
                         FROM raw_sheets rs
                         JOIN parse_batches pb ON pb.id=rs.batch_id
                         JOIN source_files sf ON sf.id=pb.file_id
                        WHERE rs.id=NEW.sheet_id
                          AND sf.project_id=NEW.project_id
                          AND (NEW.file_id IS NULL OR NEW.file_id=sf.id)
                          AND (NEW.batch_id IS NULL OR NEW.batch_id=pb.id)
                          AND (NEW.period_id IS NULL OR NEW.period_id=rs.period_id)
                   )
                 OR (
                       NEW.period_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM settlement_periods sp
                            WHERE sp.id=NEW.period_id
                              AND sp.project_id=NEW.project_id
                       )
                    )
                 OR (
                       NEW.evidence_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM evidence ev
                            WHERE ev.id=NEW.evidence_id
                              AND ev.project_id=NEW.project_id
                              AND ev.kind='sheet_coverage_proof'
                       )
                    )
                 OR (
                       NEW.c_control_evidence_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM evidence ev
                            WHERE ev.id=NEW.c_control_evidence_id
                              AND ev.project_id=NEW.project_id
                       )
                    )
               BEGIN
                   SELECT RAISE(ABORT, 'sheet coverage proof source mismatch');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_sheet_coverage_proofs_immutable_update
               BEFORE UPDATE ON sheet_coverage_proofs
               BEGIN
                   SELECT RAISE(ABORT, 'sheet coverage proof immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_sheet_coverage_proofs_immutable_delete
               BEFORE DELETE ON sheet_coverage_proofs
               BEGIN
                   SELECT RAISE(ABORT, 'sheet coverage proof immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_row_classifications_insert_guard
               BEFORE INSERT ON row_classifications
               WHEN NOT EXISTS (
                       SELECT 1 FROM sheet_coverage_proofs p
                        WHERE p.id=NEW.proof_id
                          AND p.project_id=NEW.project_id
                          AND p.sheet_id=NEW.sheet_id
                   )
                 OR (
                       NEW.evidence_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1 FROM evidence ev
                            WHERE ev.id=NEW.evidence_id
                              AND ev.project_id=NEW.project_id
                       )
                    )
               BEGIN
                   SELECT RAISE(ABORT, 'row classification source mismatch');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_row_classifications_immutable_update
               BEFORE UPDATE ON row_classifications
               BEGIN
                   SELECT RAISE(ABORT, 'row classification immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_row_classifications_immutable_delete
               BEFORE DELETE ON row_classifications
               BEGIN
                   SELECT RAISE(ABORT, 'row classification immutable');
               END""",
        ],
    ),
    (
        # v37: 版本快照必须保留业务范围和责任链定位。v35 只校验了项目/期次
        # 归属，仍允许 period_id、方向和全部来源字段同时为空；同一运行下的
        # 另一版本 Evidence/Audit 也可能被直接 SQL 拼到 final_approval。该版
        # 在数据库层拒绝无期次/无方向/无来源的快照，并要求版本 Evidence 的
        # steps/sources 与 Audit 的 target/after_json 指向同一个 version_id。
        37,
        [
            """CREATE TRIGGER IF NOT EXISTS trg_project_version_items_scope_guard
               BEFORE INSERT ON project_version_items
               WHEN NEW.period_id IS NULL
                 OR NEW.period_no IS NULL
                 OR NEW.direction NOT IN ('upward', 'downward')
                 OR (
                       NEW.line_item_id IS NULL
                       AND NOT (
                           NEW.source_file_id IS NOT NULL
                           AND NEW.source_sheet_id IS NOT NULL
                           AND NEW.source_row IS NOT NULL
                           AND NEW.source_evidence_id IS NOT NULL
                       )
                    )
               BEGIN
                   SELECT RAISE(ABORT, 'project version item scope incomplete');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_project_versions_evidence_target_guard
               BEFORE INSERT ON project_versions
               WHEN (NEW.evidence_id IS NOT NULL OR NEW.audit_id IS NOT NULL)
                AND (
                       NEW.evidence_id IS NULL
                       OR NEW.audit_id IS NULL
                       OR NOT EXISTS (
                           SELECT 1 FROM evidence ev
                            WHERE ev.id=NEW.evidence_id
                              AND ev.project_id=NEW.project_id
                              AND ev.kind='project_version'
                              AND json_valid(ev.sources_json)=1
                              AND EXISTS (
                                  SELECT 1
                                    FROM json_each(
                                        CASE WHEN json_valid(ev.sources_json)=1
                                             THEN ev.sources_json ELSE '[]' END
                                    ) AS source_entry
                                   WHERE json_extract(source_entry.value, '$.version_id')=NEW.id
                              )
                              AND json_valid(ev.steps_json)=1
                              AND EXISTS (
                                  SELECT 1
                                    FROM json_each(
                                        CASE WHEN json_valid(ev.steps_json)=1
                                             THEN ev.steps_json ELSE '[]' END
                                    ) AS step_entry
                                   WHERE json_extract(step_entry.value, '$.version_id')=NEW.id
                              )
                       )
                       OR NOT EXISTS (
                           SELECT 1 FROM audit_log al
                            WHERE al.id=NEW.audit_id
                              AND al.project_id=NEW.project_id
                              AND al.action='create_project_version'
                              AND al.target='project_version:' || NEW.id
                              AND json_extract(
                                  CASE WHEN json_valid(al.after_json)=1
                                       THEN al.after_json ELSE '{}' END,
                                  '$.version_id'
                              )=NEW.id
                       )
                    )
               BEGIN
                   SELECT RAISE(ABORT, 'project version evidence target mismatch');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_project_versions_evidence_target_update_guard
               BEFORE UPDATE ON project_versions
               WHEN (NEW.evidence_id IS NOT NULL OR NEW.audit_id IS NOT NULL)
                AND (
                       NEW.evidence_id IS NULL
                       OR NEW.audit_id IS NULL
                       OR NOT EXISTS (
                           SELECT 1 FROM evidence ev
                            WHERE ev.id=NEW.evidence_id
                              AND ev.project_id=NEW.project_id
                              AND ev.kind='project_version'
                              AND json_valid(ev.sources_json)=1
                              AND EXISTS (
                                  SELECT 1
                                    FROM json_each(
                                        CASE WHEN json_valid(ev.sources_json)=1
                                             THEN ev.sources_json ELSE '[]' END
                                    ) AS source_entry
                                   WHERE json_extract(source_entry.value, '$.version_id')=NEW.id
                              )
                              AND json_valid(ev.steps_json)=1
                              AND EXISTS (
                                  SELECT 1
                                    FROM json_each(
                                        CASE WHEN json_valid(ev.steps_json)=1
                                             THEN ev.steps_json ELSE '[]' END
                                    ) AS step_entry
                                   WHERE json_extract(step_entry.value, '$.version_id')=NEW.id
                              )
                       )
                       OR NOT EXISTS (
                           SELECT 1 FROM audit_log al
                            WHERE al.id=NEW.audit_id
                              AND al.project_id=NEW.project_id
                              AND al.action='create_project_version'
                              AND al.target='project_version:' || NEW.id
                              AND json_extract(
                                  CASE WHEN json_valid(al.after_json)=1
                                       THEN al.after_json ELSE '{}' END,
                                  '$.version_id'
                              )=NEW.id
                       )
                    )
               BEGIN
                   SELECT RAISE(ABORT, 'project version evidence target mismatch');
               END""",
        ],
    ),
    (
        # v38: 覆盖证明的数据库入口还必须绑定真实文件/批次并落在原始
        # Sheet 的尺寸内；逐行分类不能脱离证明范围或使用未知分类码。行数
        # 与金额的完整勾稽仍在只读摘要执行，因为证明 INSERT 后才会追加其
        # row_classifications，不能用即时 AFTER trigger 在中间事务误拒绝合法
        # 的“先证明、后逐行分类”原子写入。
        38,
        [
            "DROP TRIGGER IF EXISTS trg_project_version_items_scope_guard",
            """CREATE TRIGGER IF NOT EXISTS trg_project_version_items_scope_guard
               BEFORE INSERT ON project_version_items
               WHEN NEW.period_id IS NULL
                 OR NEW.period_no IS NULL
                 OR NEW.direction NOT IN ('upward', 'downward')
                 OR (
                       NEW.line_item_id IS NULL
                       AND NOT (
                           NEW.source_file_id IS NOT NULL
                           AND NEW.source_sheet_id IS NOT NULL
                           AND NEW.source_row IS NOT NULL
                           AND NEW.source_evidence_id IS NOT NULL
                       )
                    )
               BEGIN
                   SELECT RAISE(ABORT, 'project version item scope incomplete');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_project_closures_evidence_target_guard
               BEFORE INSERT ON project_closures
               WHEN (NEW.evidence_id IS NOT NULL OR NEW.audit_id IS NOT NULL)
                AND (
                       NEW.evidence_id IS NULL
                       OR NEW.audit_id IS NULL
                       OR NOT EXISTS (
                           SELECT 1 FROM evidence ev
                            WHERE ev.id=NEW.evidence_id
                              AND ev.project_id=NEW.project_id
                              AND ev.kind='project_closure'
                              AND json_valid(ev.sources_json)=1
                              AND EXISTS (
                                  SELECT 1 FROM json_each(
                                      CASE WHEN json_valid(ev.sources_json)=1
                                           THEN ev.sources_json ELSE '[]' END
                                  ) AS source_entry
                                  WHERE json_extract(source_entry.value, '$.closure_id')=NEW.id
                                    AND json_extract(source_entry.value, '$.final_version_id')=NEW.final_version_id
                              )
                              AND json_valid(ev.steps_json)=1
                              AND EXISTS (
                                  SELECT 1 FROM json_each(
                                      CASE WHEN json_valid(ev.steps_json)=1
                                           THEN ev.steps_json ELSE '[]' END
                                  ) AS step_entry
                                  WHERE json_extract(step_entry.value, '$.closure_id')=NEW.id
                                    AND json_extract(step_entry.value, '$.final_version_id')=NEW.final_version_id
                              )
                       )
                       OR NOT EXISTS (
                           SELECT 1 FROM audit_log al
                            WHERE al.id=NEW.audit_id
                              AND al.project_id=NEW.project_id
                              AND al.action='close_project_for_history'
                              AND al.target='project_closure:' || NEW.id
                              AND json_extract(
                                  CASE WHEN json_valid(al.after_json)=1
                                       THEN al.after_json ELSE '{}' END,
                                  '$.closure_id'
                              )=NEW.id
                              AND json_extract(
                                  CASE WHEN json_valid(al.after_json)=1
                                       THEN al.after_json ELSE '{}' END,
                                  '$.final_version_id'
                              )=NEW.final_version_id
                       )
                    )
               BEGIN
                   SELECT RAISE(ABORT, 'project closure evidence target mismatch');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_project_closures_evidence_target_update_guard
               BEFORE UPDATE ON project_closures
               WHEN (NEW.evidence_id IS NOT NULL OR NEW.audit_id IS NOT NULL)
                AND (
                       NEW.evidence_id IS NULL
                       OR NEW.audit_id IS NULL
                       OR NOT EXISTS (
                           SELECT 1 FROM evidence ev
                            WHERE ev.id=NEW.evidence_id
                              AND ev.project_id=NEW.project_id
                              AND ev.kind='project_closure'
                              AND json_valid(ev.sources_json)=1
                              AND EXISTS (
                                  SELECT 1 FROM json_each(
                                      CASE WHEN json_valid(ev.sources_json)=1
                                           THEN ev.sources_json ELSE '[]' END
                                  ) AS source_entry
                                  WHERE json_extract(source_entry.value, '$.closure_id')=NEW.id
                                    AND json_extract(source_entry.value, '$.final_version_id')=NEW.final_version_id
                              )
                              AND json_valid(ev.steps_json)=1
                              AND EXISTS (
                                  SELECT 1 FROM json_each(
                                      CASE WHEN json_valid(ev.steps_json)=1
                                           THEN ev.steps_json ELSE '[]' END
                                  ) AS step_entry
                                  WHERE json_extract(step_entry.value, '$.closure_id')=NEW.id
                                    AND json_extract(step_entry.value, '$.final_version_id')=NEW.final_version_id
                              )
                       )
                       OR NOT EXISTS (
                           SELECT 1 FROM audit_log al
                            WHERE al.id=NEW.audit_id
                              AND al.project_id=NEW.project_id
                              AND al.action='close_project_for_history'
                              AND al.target='project_closure:' || NEW.id
                              AND json_extract(
                                  CASE WHEN json_valid(al.after_json)=1
                                       THEN al.after_json ELSE '{}' END,
                                  '$.closure_id'
                              )=NEW.id
                              AND json_extract(
                                  CASE WHEN json_valid(al.after_json)=1
                                       THEN al.after_json ELSE '{}' END,
                                  '$.final_version_id'
                              )=NEW.final_version_id
                       )
                    )
               BEGIN
                   SELECT RAISE(ABORT, 'project closure evidence target mismatch');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_sheet_coverage_proofs_scope_guard
               BEFORE INSERT ON sheet_coverage_proofs
               WHEN NEW.file_id IS NULL
                 OR NEW.batch_id IS NULL
                 OR NEW.raw_row_start < 1
                 OR NEW.raw_row_end < NEW.raw_row_start
                 OR NEW.raw_col_start < 1
                 OR NEW.raw_col_end < NEW.raw_col_start
                 OR NEW.raw_data_row_count <> NEW.raw_row_end - NEW.raw_row_start + 1
                 OR NEW.classified_row_count < 0
                 OR NEW.business_rows_used < 0
                 OR (NEW.raw_row_end > (
                         SELECT COALESCE(rs.n_rows, 0) FROM raw_sheets rs WHERE rs.id=NEW.sheet_id
                     ) AND (
                         SELECT COALESCE(rs.n_rows, 0) FROM raw_sheets rs WHERE rs.id=NEW.sheet_id
                     ) > 0)
                 OR (NEW.raw_col_end > (
                         SELECT COALESCE(rs.n_cols, 0) FROM raw_sheets rs WHERE rs.id=NEW.sheet_id
                     ) AND (
                         SELECT COALESCE(rs.n_cols, 0) FROM raw_sheets rs WHERE rs.id=NEW.sheet_id
                     ) > 0)
               BEGIN
                   SELECT RAISE(ABORT, 'sheet coverage proof scope incomplete');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_row_classifications_scope_guard
               BEFORE INSERT ON row_classifications
               WHEN NOT EXISTS (
                       SELECT 1 FROM sheet_coverage_proofs p
                        WHERE p.id=NEW.proof_id
                          AND p.project_id=NEW.project_id
                          AND p.sheet_id=NEW.sheet_id
                          AND NEW.row_number BETWEEN p.raw_row_start AND p.raw_row_end
                   )
                 OR NEW.class_code NOT IN (
                       'blank', 'detail', 'subtotal', 'grand_total', 'title', 'note',
                       'tail_note', 'orphan_numeric', 'parse_failed',
                       'pending_review', 'unrecognized'
                   )
               BEGIN
                   SELECT RAISE(ABORT, 'row classification scope incomplete');
               END""",
        ],
    ),
    (
        # v39: 原始网格是所有范围证明的证据根。解析完成后禁止通过直接 SQL
        # 改写/删除 raw_cells，使“同时伪造原始值、覆盖证明和 A/B 结果”不再
        # 能绕过只读摘要的来源一致性检查；合法导入仍只执行 INSERT。
        39,
        [
            """CREATE TRIGGER IF NOT EXISTS trg_raw_cells_immutable_update
               BEFORE UPDATE ON raw_cells
               BEGIN
                   SELECT RAISE(ABORT, 'raw cell immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_raw_cells_immutable_delete
               BEFORE DELETE ON raw_cells
               BEGIN
                   SELECT RAISE(ABORT, 'raw cell immutable');
               END""",
        ],
    ),
    (
        # v40: 版本/历史价格的不可变快照不仅要“指向同一个 ID”，还必须
        # 与该 ID 在写入时的核心字段和来源定位一致。否则旧脚本可以挂接
        # 一个真实 line_item/source_version_item_id，却把名称、数量、单价、
        # 合价或文件行列改成伪造值，再由摘要/导出原样传播。快照写入后仍
        # 允许当前 line_items 变化，历史版本不会随当前清单回写；本闸门只
        # 约束 INSERT 时的来源一致性。
        40,
        [
            """CREATE TRIGGER IF NOT EXISTS trg_project_version_items_snapshot_source_guard
               BEFORE INSERT ON project_version_items
               WHEN NEW.line_item_id IS NOT NULL
                AND NOT EXISTS (
                       SELECT 1
                         FROM line_items li
                         JOIN settlement_periods sp ON sp.id=li.period_id
                        WHERE li.id=NEW.line_item_id
                          AND li.period_id=NEW.period_id
                          AND sp.project_id=NEW.project_id
                          AND COALESCE(sp.period_no, -1)=COALESCE(NEW.period_no, -1)
                          AND COALESCE(sp.direction, 'unknown')=COALESCE(NEW.direction, 'unknown')
                          AND COALESCE(li.code, '') IS COALESCE(NEW.code, '')
                          AND li.name IS NEW.name
                          AND COALESCE(li.feature, '') IS COALESCE(NEW.feature, '')
                          AND COALESCE(li.unit, '') IS COALESCE(NEW.unit, '')
                          AND li.quantity IS NEW.quantity
                          AND li.unit_price IS NEW.unit_price
                          AND li.amount IS NEW.amount
                    )
               BEGIN
                   SELECT RAISE(ABORT, 'project version item snapshot source mismatch');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_historical_unit_prices_snapshot_source_guard
               BEFORE INSERT ON historical_unit_prices
               WHEN NOT EXISTS (
                       SELECT 1
                         FROM project_version_items pvi
                         JOIN project_versions pv
                           ON pv.id=pvi.version_id
                          AND pv.project_id=pvi.project_id
                         JOIN projects p ON p.id=pvi.project_id
                        WHERE pvi.id=NEW.source_version_item_id
                          AND pvi.version_id=NEW.source_version_id
                          AND pvi.project_id=NEW.source_project_id
                          AND NEW.raw_project_name IS p.name
                          AND NEW.raw_name IS pvi.name
                          AND NEW.feature IS pvi.feature
                          AND NEW.unit IS pvi.unit
                          AND NEW.direction IS pvi.direction
                          AND CAST(NEW.unit_price AS NUMERIC)=CAST(pvi.unit_price AS NUMERIC)
                          AND (
                              (NEW.quantity IS NULL AND pvi.quantity IS NULL)
                              OR CAST(NEW.quantity AS NUMERIC)=CAST(pvi.quantity AS NUMERIC)
                          )
                          AND (
                              (NEW.amount IS NULL AND pvi.amount IS NULL)
                              OR CAST(NEW.amount AS NUMERIC)=CAST(pvi.amount AS NUMERIC)
                          )
                          AND NEW.source_file_id IS pvi.source_file_id
                          AND NEW.source_sheet_id IS pvi.source_sheet_id
                          AND NEW.source_row IS pvi.source_row
                          AND NEW.source_evidence_id IS pvi.source_evidence_id
                    )
               BEGIN
                   SELECT RAISE(ABORT, 'historical unit price snapshot source mismatch');
               END""",
        ],
    ),
    (
        # v41: 版本项的 source_sheet 必须绑定到同一项目、同一期次、同一
        # 方向和同一源文件；如果同时挂有 line_item_id，还必须与该清单行
        # 的实际来源 Sheet/文件一致。v40 只校验了 line_item 的核心字段，
        # 新闸门阻止把其他期次的原始单元格包装成当前期版本项。
        41,
        [
            """CREATE TRIGGER IF NOT EXISTS trg_project_version_items_explicit_source_scope_guard
               BEFORE INSERT ON project_version_items
               WHEN (
                       (
                              NEW.line_item_id IS NULL
                              AND (
                                     NEW.source_file_id IS NULL
                                  OR NEW.source_sheet_id IS NULL
                                  OR NEW.source_row IS NULL
                                  OR NEW.source_evidence_id IS NULL
                                  OR NOT EXISTS (
                                      SELECT 1
                                        FROM raw_sheets rs
                                        JOIN parse_batches pb ON pb.id=rs.batch_id
                                        JOIN settlement_periods sp ON sp.id=rs.period_id
                                       WHERE rs.id=NEW.source_sheet_id
                                         AND pb.file_id=NEW.source_file_id
                                         AND rs.period_id=NEW.period_id
                                         AND sp.project_id=NEW.project_id
                                         AND COALESCE(sp.period_no, -1)=COALESCE(NEW.period_no, -1)
                                         AND COALESCE(sp.direction, 'unknown')=COALESCE(NEW.direction, 'unknown')
                                  )
                              )
                       )
                    OR (
                           NEW.line_item_id IS NOT NULL
                           AND (
                                  NEW.source_file_id IS NOT NULL
                               OR NEW.source_sheet_id IS NOT NULL
                           )
                           AND (
                                  NEW.source_file_id IS NULL
                               OR NEW.source_sheet_id IS NULL
                               OR NOT EXISTS (
                            SELECT 1
                              FROM raw_sheets rs
                              JOIN parse_batches pb ON pb.id=rs.batch_id
                             JOIN settlement_periods sp ON sp.id=rs.period_id
                            WHERE rs.id=NEW.source_sheet_id
                              AND pb.file_id=NEW.source_file_id
                              AND rs.period_id=NEW.period_id
                              AND sp.project_id=NEW.project_id
                              AND COALESCE(sp.period_no, -1)=COALESCE(NEW.period_no, -1)
                              AND COALESCE(sp.direction, 'unknown')=COALESCE(NEW.direction, 'unknown')
                               )
                           )
                       )
                    OR (
                           NEW.line_item_id IS NOT NULL
                           AND (
                                  NEW.source_file_id IS NOT NULL
                               OR NEW.source_sheet_id IS NOT NULL
                           )
                           AND NOT EXISTS (
                           SELECT 1
                             FROM line_items li
                             LEFT JOIN raw_sheets rs ON rs.id=li.sheet_id
                             LEFT JOIN parse_batches pb ON pb.id=rs.batch_id
                            WHERE li.id=NEW.line_item_id
                              AND (
                                     (NEW.source_sheet_id IS NULL AND li.sheet_id IS NULL)
                                  OR (NEW.source_sheet_id IS NOT NULL AND li.sheet_id=NEW.source_sheet_id)
                              )
                              AND (
                                     (NEW.source_file_id IS NULL AND pb.file_id IS NULL)
                                  OR (NEW.source_file_id IS NOT NULL AND pb.file_id=NEW.source_file_id)
                              )
                       )
                   )
               )
               BEGIN
                   SELECT RAISE(ABORT, 'project version item scope incomplete (explicit source scope mismatch)');
               END""",
        ],
    ),
    (
        # v42: 原始网格尺寸/布局和已确认表头属于证据范围事实，不能通过
        # UPDATE 直接把新增原始行吞进表头或缩窄 Sheet。业务状态（period_id、
        # sheet_status 等）仍由受控 API 更新；网格本身只能由导入时 INSERT。
        42,
        [
            """CREATE TRIGGER IF NOT EXISTS trg_raw_sheets_grid_immutable_update
               BEFORE UPDATE ON raw_sheets
               WHEN NEW.n_rows IS NOT OLD.n_rows
                 OR NEW.n_cols IS NOT OLD.n_cols
                 OR NEW.merged_ranges_json IS NOT OLD.merged_ranges_json
                 OR NEW.hidden_rows_json IS NOT OLD.hidden_rows_json
                 OR NEW.hidden_cols_json IS NOT OLD.hidden_cols_json
               BEGIN
                   SELECT RAISE(ABORT, 'raw sheet grid immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_table_headers_immutable_update
               BEFORE UPDATE ON table_headers
               BEGIN
                   SELECT RAISE(ABORT, 'table header immutable');
               END""",
        ],
    ),
    (
        # v43: 原始文件身份和原始网格坐标是 Run Contract 的输入根，不能
        # 通过直接 SQL 改写后继续复用旧运行。source_files 允许受控更新
        # stored_path（例如恢复副本位置），但文件身份、项目归属和导入
        # 时间一经登记即不可变；raw_cells 只能写入导入时声明的 Sheet 网格。
        43,
        [
            """CREATE TRIGGER IF NOT EXISTS trg_source_files_identity_immutable_update
               BEFORE UPDATE ON source_files
               WHEN NEW.project_id IS NOT OLD.project_id
                 OR NEW.original_path IS NOT OLD.original_path
                 OR NEW.original_name IS NOT OLD.original_name
                 OR NEW.sha256 IS NOT OLD.sha256
                 OR NEW.size_bytes IS NOT OLD.size_bytes
                 OR NEW.file_type IS NOT OLD.file_type
                 OR NEW.imported_at IS NOT OLD.imported_at
               BEGIN
                   SELECT RAISE(ABORT, 'source file identity immutable');
               END""",
            """CREATE TRIGGER IF NOT EXISTS trg_raw_cells_grid_scope_guard
               BEFORE INSERT ON raw_cells
               WHEN NOT EXISTS (
                       SELECT 1
                         FROM raw_sheets rs
                        WHERE rs.id=NEW.sheet_id
                          AND NEW.row BETWEEN 1 AND rs.n_rows
                          AND NEW.col BETWEEN 1 AND rs.n_cols
                   )
               BEGIN
                   SELECT RAISE(ABORT, 'raw cell outside sheet grid');
               END""",
        ],
    ),
    (
        # v44: 解析层保留公式缓存、工作表/表格筛选和结构化表范围。
        # openpyxl 不能证明筛选后的实际可见行，也不能在未缓存或动态公式时
        # 证明金额值已确定性重算；这些字段作为不可变输入元数据供导入/异常
        # 层 fail-closed。原始网格仍只允许在解析批次中 INSERT。
        44,
        [
            "ALTER TABLE raw_cells ADD COLUMN formula_cache_status TEXT NOT NULL DEFAULT 'not_formula'",
            "ALTER TABLE raw_sheets ADD COLUMN auto_filter_ref TEXT",
            "ALTER TABLE raw_sheets ADD COLUMN filter_conditions_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE raw_sheets ADD COLUMN table_ranges_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE raw_sheets ADD COLUMN filter_state TEXT NOT NULL DEFAULT 'none'",
            "ALTER TABLE raw_sheets ADD COLUMN formula_metadata_json TEXT NOT NULL DEFAULT '{}'",
            "DROP TRIGGER IF EXISTS trg_raw_sheets_grid_immutable_update",
            """CREATE TRIGGER IF NOT EXISTS trg_raw_sheets_grid_immutable_update
               BEFORE UPDATE ON raw_sheets
               WHEN NEW.n_rows IS NOT OLD.n_rows
                 OR NEW.n_cols IS NOT OLD.n_cols
                 OR NEW.merged_ranges_json IS NOT OLD.merged_ranges_json
                 OR NEW.hidden_rows_json IS NOT OLD.hidden_rows_json
                 OR NEW.hidden_cols_json IS NOT OLD.hidden_cols_json
                 OR NEW.auto_filter_ref IS NOT OLD.auto_filter_ref
                 OR NEW.filter_conditions_json IS NOT OLD.filter_conditions_json
                 OR NEW.table_ranges_json IS NOT OLD.table_ranges_json
                 OR NEW.filter_state IS NOT OLD.filter_state
                 OR NEW.formula_metadata_json IS NOT OLD.formula_metadata_json
               BEGIN
                   SELECT RAISE(ABORT, 'raw sheet grid immutable');
               END""",
            "CREATE INDEX IF NOT EXISTS idx_raw_sheets_filter_state ON raw_sheets(filter_state, period_id, id)",
        ],
    ),
    (
        # v45: 历史单价快照的维度列以空字符串表示缺失，而版本快照仍可
        # 保留 NULL。来源校验必须按同一“缺失”语义比较，否则真实项目中
        # 缺少单位/特征的有效单价会在历史资产写入时被错误拒绝。
        45,
        [
            "DROP TRIGGER IF EXISTS trg_historical_unit_prices_snapshot_source_guard",
            """CREATE TRIGGER IF NOT EXISTS trg_historical_unit_prices_snapshot_source_guard
               BEFORE INSERT ON historical_unit_prices
               WHEN NOT EXISTS (
                       SELECT 1
                         FROM project_version_items pvi
                         JOIN project_versions pv
                           ON pv.id=pvi.version_id
                          AND pv.project_id=pvi.project_id
                         JOIN projects p ON p.id=pvi.project_id
                        WHERE pvi.id=NEW.source_version_item_id
                          AND pvi.version_id=NEW.source_version_id
                          AND pvi.project_id=NEW.source_project_id
                          AND NEW.raw_project_name IS p.name
                          AND NEW.raw_name IS pvi.name
                          AND COALESCE(NEW.feature, '') IS COALESCE(pvi.feature, '')
                          AND COALESCE(NEW.unit, '') IS COALESCE(pvi.unit, '')
                          AND NEW.direction IS pvi.direction
                          AND CAST(NEW.unit_price AS NUMERIC)=CAST(pvi.unit_price AS NUMERIC)
                          AND (
                              (NEW.quantity IS NULL AND pvi.quantity IS NULL)
                              OR CAST(NEW.quantity AS NUMERIC)=CAST(pvi.quantity AS NUMERIC)
                          )
                          AND (
                              (NEW.amount IS NULL AND pvi.amount IS NULL)
                              OR CAST(NEW.amount AS NUMERIC)=CAST(pvi.amount AS NUMERIC)
                          )
                          AND NEW.source_file_id IS pvi.source_file_id
                          AND NEW.source_sheet_id IS pvi.source_sheet_id
                          AND NEW.source_row IS pvi.source_row
                          AND NEW.source_evidence_id IS pvi.source_evidence_id
                    )
               BEGIN
                   SELECT RAISE(ABORT, 'historical unit price snapshot source mismatch');
               END""",
        ],
    ),
    # v46（v0.1.18）：经营合规问题台账字段。技术异常（anomalies）与人工台账
    # 评估严格分离：台账值只能由人工经 ledger API 写入并留审计事件，
    # 规则引擎永远不写这些列。金额影响保存为 TEXT 由上层 Decimal 解析，
    # 避免引入浮点。台账按 (project_id, fingerprint) 定位——anomalies.id
    # 每次分析运行都会重建，fingerprint 才是跨运行稳定的问题身份。
    (
        46,
        [
            """CREATE TABLE IF NOT EXISTS finding_ledger (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                fingerprint TEXT NOT NULL,
                amount_impact TEXT,
                responsible_unit TEXT,
                responsible_matter TEXT,
                handling_opinion TEXT,
                ledger_status TEXT NOT NULL DEFAULT 'pending_confirmation',
                updated_by TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, fingerprint)
            )""",
            """CREATE TRIGGER IF NOT EXISTS finding_ledger_status_guard
               BEFORE UPDATE ON finding_ledger
               FOR EACH ROW
               WHEN NEW.ledger_status NOT IN (
                   'pending_confirmation','confirmed','resolved','not_applicable')
               BEGIN
                   SELECT RAISE(ABORT, 'invalid finding_ledger status');
               END""",
        ],
    ),
    # v47（开发中）：资料接收台账与可复核分类。类别只表达用户确认的资料用途，
    # 绝不从文件名推断对上/对下或业务金额；未分类文件保留原件且不进入计算模型。
    (
        47,
        [
            """CREATE TABLE IF NOT EXISTS document_intake (
                file_id INTEGER PRIMARY KEY REFERENCES source_files(id),
                project_id INTEGER NOT NULL REFERENCES projects(id),
                category TEXT NOT NULL DEFAULT 'unclassified',
                classification_status TEXT NOT NULL DEFAULT 'unclassified',
                direction TEXT NOT NULL DEFAULT 'unknown',
                parse_status TEXT NOT NULL DEFAULT 'registered',
                detail TEXT NOT NULL DEFAULT '',
                parser TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_document_intake_project_status ON document_intake(project_id, parse_status, category)",
            """CREATE TRIGGER IF NOT EXISTS trg_document_intake_project_guard_insert
               BEFORE INSERT ON document_intake
               WHEN NOT EXISTS (
                   SELECT 1 FROM source_files sf
                    WHERE sf.id=NEW.file_id AND sf.project_id=NEW.project_id
               )
               BEGIN SELECT RAISE(ABORT, 'document intake source project mismatch'); END""",
            """CREATE TRIGGER IF NOT EXISTS trg_document_intake_project_guard_update
               BEFORE UPDATE OF file_id, project_id ON document_intake
               WHEN NOT EXISTS (
                   SELECT 1 FROM source_files sf
                    WHERE sf.id=NEW.file_id AND sf.project_id=NEW.project_id
               )
               BEGIN SELECT RAISE(ABORT, 'document intake source project mismatch'); END""",
        ],
    ),
    # v48：合同事实确认生命周期。抽取产出一律是 candidate；只有人工确认
    # （review_status='confirmed'）的事实才可视为已确认合同事实；历史事实
    # 无法证明经过人工确认，回填为 candidate 而不是 confirmed。
    (
        48,
        [
            "ALTER TABLE contract_facts ADD COLUMN review_status TEXT NOT NULL DEFAULT 'candidate'",
            "ALTER TABLE contract_facts ADD COLUMN reviewed_at TEXT",
            "ALTER TABLE contract_facts ADD COLUMN reviewed_by TEXT",
            "ALTER TABLE contract_facts ADD COLUMN review_reason TEXT NOT NULL DEFAULT ''",
        ],
    ),
    # v49：PDF 逐页人工对照复核。含 OCR 页的合同停在 needs_review；用户对照
    # 只读原件逐页核实后，全部应复核页 verified 的文档才允许转 parsed。
    # 表保存每页当前决定，历史流转留在 contract_fact_review 同级审计 Evidence。
    (
        49,
        [
            """CREATE TABLE IF NOT EXISTS pdf_page_reviews (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                file_id INTEGER NOT NULL REFERENCES source_files(id),
                page_number INTEGER NOT NULL,
                decision TEXT NOT NULL,
                reviewed_by TEXT NOT NULL DEFAULT 'user',
                reviewed_at TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                evidence_id INTEGER,
                UNIQUE(project_id, file_id, page_number)
            )""",
        ],
    ),
    # v50：对上控制基准候选（终审/审计报告等作为对比上限候选）。候选只能来自
    # 已确认合同事实或人工显式登记；只有人工确认的基准才参与上限比较；
    # supersedes 必须显式声明，两个并存有效基准比较时返回 CONTROL_CONFLICT。
    (
        50,
        [
            """CREATE TABLE IF NOT EXISTS control_baselines (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                doc_id INTEGER REFERENCES contract_docs(id),
                fact_id INTEGER REFERENCES contract_facts(id),
                amount TEXT NOT NULL,
                tax_basis TEXT NOT NULL DEFAULT 'unknown',
                scope_note TEXT NOT NULL DEFAULT '',
                source_note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'candidate',
                supersedes_id INTEGER REFERENCES control_baselines(id),
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                confirmed_by TEXT,
                confirmed_reason TEXT NOT NULL DEFAULT '',
                evidence_id INTEGER
            )""",
        ],
    ),
    # v51：框架/管理性协议费率规则（宪章 §五：费率不是一个百分比）。抽取只产
    # 候选；确认时必须人工设定 base_type/base_definition（"按结算价3%" 与
    # "按不含甲供材税前建安费3%" 不是同一条规则）；试算用 Decimal 确定性计算。
    (
        51,
        [
            """CREATE TABLE IF NOT EXISTS rate_rules (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                doc_id INTEGER REFERENCES contract_docs(id),
                file_id INTEGER REFERENCES source_files(id),
                rate_percent TEXT,
                base_type TEXT NOT NULL DEFAULT 'unset',
                base_definition TEXT NOT NULL DEFAULT '',
                tax_basis TEXT NOT NULL DEFAULT 'unknown',
                cap TEXT,
                floor TEXT,
                effective_scope TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 0,
                quote_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'candidate',
                reviewed_at TEXT,
                reviewed_by TEXT,
                review_reason TEXT NOT NULL DEFAULT '',
                evidence_id INTEGER,
                created_at TEXT NOT NULL
            )""",
        ],
    ),
    # v52：Sheet 清单类型标注（任务 B）。unknown=未识别；boq_detail=分部分项；
    # measure_unit=单价措施（量×价，与分部分项同构）；measure_total=总价措施
    # （费率计取，无工程量，GB50500）；summary=汇总/控制页；non_business=非业务。
    # 仅是内容类型标注，不改变 sheet_status 角色/门控语义。
    (
        52,
        [
            "ALTER TABLE raw_sheets ADD COLUMN list_kind TEXT NOT NULL DEFAULT 'unknown'",
        ],
    ),
    # v53：Sheet 单元格摘要缓存（F-3 性能修复）。raw_cells 按 sheet_id 插入后
    # 不可变（重解析产生新 sheet_id），摘要算一次即可复用；表可随时清空重建，
    # 不影响摘要语义。
    (
        53,
        [
            """CREATE TABLE IF NOT EXISTS sheet_cell_digests (
                sheet_id INTEGER PRIMARY KEY REFERENCES raw_sheets(id),
                digest TEXT NOT NULL,
                cell_count INTEGER NOT NULL,
                computed_at TEXT NOT NULL
            )""",
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
        # 旧程序不得打开或降写未来数据库。除了 schema_migrations 主版本外，
        # projects.schema_version 也是冗余的结构标记；任一处超过当前支持
        # 版本都说明本程序无法证明字段/约束语义，必须在任何写入前失败关闭。
        project_version_row = conn.execute(
            "SELECT MAX(schema_version) AS v FROM projects"
            " WHERE EXISTS (SELECT 1 FROM sqlite_master"
            "               WHERE type='table' AND name='projects')"
        ).fetchone() if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'"
        ).fetchone() else None
        project_version = int(project_version_row["v"] or 0) if project_version_row else 0
        if ver > target or project_version > target:
            future = max(ver, project_version)
            raise MigrationError(
                f"检测到未来 schema {future}（当前程序最多支持 {target}），"
                "拒绝打开或降写该数据库"
            )
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
                    try:
                        conn.execute(sql)
                    except sqlite3.OperationalError as oe:
                        if "duplicate column name" in str(oe):
                            continue
                        raise
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
