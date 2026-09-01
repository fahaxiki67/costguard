# ARCHITECTURE.md — Jiadun（价盾）架构设计

> 当前实现参考：v0.1.13 预览候选。架构变更须新增 ADR。

## 1. 总体形态

单机本地桌面软件，无服务器依赖。逻辑分层：

```
┌─────────────────────────────────────────────────────┐
│  ui/            PySide6 桌面界面（共享，跨平台）        │
├─────────────────────────────────────────────────────┤
│  core/          业务引擎（平台无关，禁止平台 API）       │
│   ├─ models/      领域模型 + DB ORM（sqlite3 手写层）  │
│   ├─ db/          连接、Schema 迁移、备份回滚           │
│   ├─ parsing/     xlsx/xls/csv/pdf/docx → 原始网格     │
│   ├─ cleaning/    数值清洗、文本数字、千分位、¥、负数     │
│   ├─ engine/      结算汇总、累计、双向校核（Decimal）    │
│   ├─ matching/    清单匹配、别名库、置信度分级           │
│   ├─ anomalies/   异常检测规则引擎（23+ 规则）           │
│   ├─ evidence/    证据链、统一 Finding、审计日志         │
│   ├─ contracts/   合同条款与运行契约                    │
│   └─ export/      Excel/Word/PPT 成果导出              │
├─────────────────────────────────────────────────────┤
│  platform/      平台差异隔离层（路径/字体/权限/打包）     │
├─────────────────────────────────────────────────────┤
│  用户工作空间     用户自选目录下的项目文件夹（数据与程序分离）│
└─────────────────────────────────────────────────────┘
```

## 2. 关键决策（详见 docs/adr/）

| ADR | 决策 |
|-----|------|
| ADR-001 | Python 3.12 + uv 管理；兼容 macOS arm64 与 Windows x64 |
| ADR-002 | PySide6 (LGPL) 作为共享桌面 UI |
| ADR-003 | SQLite 单文件项目库 + 用户可见的工作空间目录 |
| ADR-004 | 金额/数量/单价一律 Decimal，禁用 float |
| ADR-005 | 原始文件导入即复制为只读副本，原始路径仅记录不触碰 |
| ADR-006 | 证据链：每个结论/异常/修正生成 Evidence ID + 计算过程快照 |
| ADR-007 | platform/ 抽象层承载全部 OS 差异 |
| ADR-008 | 解析采用"原始网格保真 + 语义层重建"两段式 |
| ADR-009 | Apache-2.0（已接受；参考 AGPL 项目时只借鉴公开设计，未经许可证审查不复制代码） |
| ADR-010 | LLM 仅做语义识别/条款理解，确定性计算全部本地程序完成；LLM 适配层可插拔、可完全离线 |

## 3. 数据存储与工作空间

软件自身数据与用户工程数据严格分离：

- **软件配置**：`platform.paths.config_dir()`（macOS: `~/Library/Application Support/Jiadun`；Windows: `%APPDATA%\Jiadun`）
- **默认工作空间根**：`~/Documents/JiadunProjects/`（用户可在 UI 中更改）
- **项目目录**（每个工程项目一个文件夹，用户可见、可备份、可移动）：

```
<workspace>/<project_name>/
├── project.db          # SQLite：全部结构化数据（schema_version 管理）
├── originals/          # 导入文件的只读副本（sha256 命名，永不修改）
├── exports/            # 导出成果
└── backups/            # Migration 前自动备份
```

### 产品改名与兼容

当前产品名称为 **Jiadun（价盾）**，机器可读的命令和 Python 包名为 `jiadun`。
新建项目使用 `Jiadun` 配置目录和 `JiadunProjects` 工作空间根目录。历史版本
使用的 `CostGuard` 配置、项目目录和工作空间路径仍属于兼容资料；改名或升级不得
删除、覆盖或伪造这些旧路径中的原始文件。GitHub 仓库地址、已有 v0.x tag、Release
和远端资产仍沿用历史 `CostGuard` 名称，旧结果和历史路径必须可追溯。

## 4. 数据模型概要（project.db）

```
projects(id, name, schema_version, created_at, workspace_path)
source_files(id, project_id, original_path, stored_path, sha256, size_bytes,
             file_type, imported_at, note)                    -- 只读副本登记
parse_batches(id, file_id, parser, parsed_at, status, stats_json)
raw_sheets(id, batch_id, sheet_index, sheet_name, n_rows, n_cols,
           merged_ranges_json, hidden_rows_json, hidden_cols_json)
raw_cells(sheet_id, row, col, raw_value, cached_value, is_formula,
          is_number_stored_as_text, num_fmt)                   -- 网格保真层
table_headers(sheet_id, header_row_lo, header_row_hi, col_map_json, confidence)
settlement_periods(id, project_id, period_no, title, source_file_id,
                   direction, -- 'upward'对上 / 'downward'对下 / 'unknown'
                   contract_party, tax_mode, note)
line_items(id, period_id, sheet_id, code, name, feature, unit,
           quantity, unit_price, amount, tax_rate,
           qty_evid, price_evid, amount_evid,                  -- 证据外键
           flags_json)                                         -- 清洗标记
item_aliases(id, project_id, canonical_key, alias_text, mapping_basis,
             confirmed_by, confirmed_at)                       -- 别名/映射库
matches(id, project_id, group_key, item_ids, level, -- 5档置信度
        method, score, status, reviewed_by, review_note, run_signature)
period_totals(period_id, item_key, qty_sum, amount_sum, wavg_price,
              cross_check_diff, cross_check_status, run_signature)
anomalies(id, project_id, rule_id, severity, subject_type, subject_id,
          evidence_id, message, status, resolved_note, run_signature,
          finding_id, fingerprint, confidence, detection_mode,
          raw_values_json, normalized_values_json, impact, limitations_json,
          recommendation, suppression_reason)
evidence(id, project_id, kind, summary, steps_json, sources_json,
          created_at, run_signature, finding_id)                  -- 证据链
audit_log(id, project_id, ts, actor, action, target, before_json,
          after_json, reason)                                   -- 人工修改日志
contract_docs / contract_facts(...)                             -- Phase 6
run_contracts(project_id, signature, components_json, created_at,
              invalidated_at)                                   -- 运行契约
export_runs(project_id, kind, path, run_signature, file_sha256,
            generated_at, status, metadata_json)                 -- 成果登记
detection_runs(project_id, run_signature, run_kind, status,
               expected_json, executed_json, skipped_json,
               failed_json, critical_failed_json, metadata_json)  -- 检测/聚合覆盖率
import_manifests(project_id, manifest_key, control_hash, status)  -- 权威应到清单
import_manifest_entries(manifest_id, logical_key, expected_sha256,
                         expected_period_no, expected_direction,
                         received_file_id, state)                -- 到件状态
cleaning_changes(project_id, run_signature, event_key, subject_id,
                 before_json, proposed_json, status, reason,
                 evidence_id, audit_id)                           -- 清洗事件
schema_migrations(version, applied_at)
```

### 证据链（ADR-006）

任何差异、异常、风险、结论都持有 `evidence_id`。证据记录：
`结论 → 计算过程(steps_json) → 使用的数据 → 数据来源(文件+Sheet+单元格) → 原始文件(stored_path) → 原始位置`。
UI 中点击 Evidence ID 可展开全链路。人工修改写入 `audit_log`，同样生成证据。

### 运行契约（Run Contract）

一次校核、异常检测、匹配或成果导出必须明确它使用的输入和配置。运行契约把
源文件 SHA-256、工作表及取数范围、字段映射、期次方向、合同事实、明细输入、
规则配置、Schema 版本和代码版本规范化后生成运行签名。签名变化时，旧结果和
导出登记保留为历史并标记失效，当前读取面只使用新签名；导出文件本身不因失效
而被删除。

统一 Finding 保留稳定的 `finding_id` 和 `fingerprint`，同时记录置信度、检测
方式、原始值、标准化值、影响、限制、建议和抑制原因。重复运行可以据此识别同
一问题，人工处理仍需通过 `audit_log` 和 Evidence ID 回查。

检测规则和 A/B/C 聚合验证共用 `detection_runs` 表，但由 `run_kind` 分开读取。
覆盖率明确列出预期、执行、跳过和失败项；技术失败不写成低级业务异常，覆盖不
完整时所有成果通道保持待复核门控。权威批次清单不从已收到文件反向生成，复合
键重复先于匹配被阻断，清洗建议也只进入 Evidence/Audit 事件，不直接改写原始
明细。

## 5. 解析两段式（ADR-008）

1. **保真层**：openpyxl `data_only=False+True` 双读，逐格落库 `raw_cells`，
   保留公式缓存值、文本数字标记、合并单元格拓扑、隐藏行列位图。原始网格永不解释。
2. **语义层**：表头识别（多行表头扫描、字段词典打分）→ 列映射 → 逐行抽取
   `line_items`，同时记录每个字段来自哪个单元格。语义层可随时在保真层上重放。

## 6. 计算纪律（ADR-004）

- `Decimal` + `ROUND_HALF_UP`；聚合后再比较，容差仅用于"报告差异"，不用于"调平"。
- 双向校核：A = 各期金额直接累计；B = 明细重新清洗、重新汇总。差异必须落库并生成证据。
- 交叉校验：每个涉及金额的最终结果至少一条独立路径复算。

## 7. 匹配与置信度（五档）

`确认匹配`(编码相同且名称一致) / `高概率匹配`(编码或强特征一致) /
`疑似匹配`(仅语义相似，rapidfuzz 得分区间+人工确认) / `不可比` / `待补资料`。
任何合并动作必须经过用户确认并写入 `item_aliases` + `audit_log`。

## 8. 平台抽象层（ADR-007）

`platform/paths.py`（config_dir / default_workspace / 文件管理器 reveal）、
`platform/system.py`（字体、单实例、打开方式）、`platform/packaging/`（PyInstaller spec、DMG/NSIS 脚本）。
core 代码出现 `import platform模块` 以外的平台判断即视为架构违规（CI 检查）。

## 9. 测试体系

- `tests/unit/`：清洗、金额、表头识别、匹配、异常规则（hypothesis property-based）。
- `tests/integration/`：导入→解析→核算→异常→证据→导出全链路。
- `synthetic_test_data/`：生成器脚本 + 固定异常样本（多层表头、合并单元格、隐藏行列、
  文本数字、千分位、¥、负数、公式错误、重复、漏项、单位转换、名称陷阱、税率变化、舍入差异）。
- CI（GitHub Actions）：lint + unit + integration + security scan；macOS arm64 优先。

## 10. 已识别的扩展性风险与对策（Phase 0 自检）

| 风险 | 对策 |
|------|------|
| SQLite 单文件在超大项目（>100万行明细）下性能 | 明细分表 + 索引设计（period_id+code）；解析批处理写入；DuckDB 仅作只读分析缓存，不替代主库 |
| PySide6 打包体积大、启动慢 | 打包阶段裁剪 Qt 模块（PyInstaller exclude），启动懒加载重视图 |
| 表头识别误判导致静默错数据 | 所有列映射带 confidence，低于阈值强制进入人工复核；映射结果可回放复核 |
| 证据链数据膨胀 | 证据 JSON 压缩存储；原始网格按 Sheet 惰性加载 |
| Windows 路径/编码差异早期渗入 core | Phase 1 即建立 platform 层 + CI 平台违规静态检查 |
| LLM 依赖破坏离线性 | LLM 适配层默认关闭，全部功能可离线运行 |
