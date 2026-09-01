# Changelog — Jiadun（价盾）

All notable changes. Format based on Keep a Changelog; versioning: SemVer.

> Naming note: the current product name is Jiadun（价盾）. Historical v0.x commands,
> package paths, project paths and release assets keep their original `CostGuard`
> spelling so that published facts remain traceable; they are not evidence that the
> remote assets have been renamed.

## [Unreleased]

后续变更将在下一版本单独记录。

## [0.1.14] - 2026-09-02

本轮候选改动优先收紧结算结果可信度和校核独立性，暂不宣称生产门槛已经全部闭合。

### Added

- A 路径继续从清洗后的 `line_items` 汇总，B 路径改为直接从不可变
  `raw_cells` 独立扫描；每条路径在校核 Evidence 中记录文件、Sheet、物理行号、
  数据范围和过滤条件。
- C 路径控制值继续优先使用唯一合计级行；无合计级行时才使用页级小计互斥求和，
  并保存控制值原始位置、原始值和来源 Evidence。
- 校核结果新增重复明细、负数调整、关键字段缺失、金额计算不一致、跨期单价变化和
  未解决原始行等一致性闸门；这些问题未解释前不能形成绿色“校核充分”。
- 人工确认隐藏行/列时，覆盖证明、期次校核和项目摘要复用同一范围契约；人工范围仍须
  覆盖表头后的全部非空物理行，遗漏尾部明细或控制行会统一降为“校核不充分”。
- 新增 `tests/anonymized_golden_cases/` 脱敏真实案例登记入口，以及合成/脱敏登记表
  的闭世界黄金回归比较。黄金结果为空、字段缺失或实际多出未登记指标时均报告差异，
  不自动更新基线。

### Changed

- Run Contract 当前读取闸门统一检查源文件、明细指纹、字段映射、期次、Sheet 范围和
  其它合同输入；任一组成漂移都会把旧自动成果移出当前读取面。
- 相同 SHA-256、同方向、同期次的文件重复选择改为幂等返回，不再重复写入规范明细；
  同一文件拟用于其它方向或期次时要求走明确的补充/版本入口。
- 核心金额运算入口拒绝 `float`、`int` 和非有限数值；外部单元格仍须先转换为有限
  `Decimal`，缺失金额保持待补资料，不以 0 替代。
- 工作台在显示期次结果时同时显示 A/B 独立扫描和一致性问题，并在项目级状态未开放时
  明确说明局部结果不构成项目结论；异常检测失败允许重试，不锁死界面。
- C 路径 Evidence 的选择规则按本次实际分支记录，并区分唯一合计级行、页级小计互斥
  汇总和控制值不可用，避免来源标签与实际控制值口径不一致。

### Validation boundary

- 自动化 `pytest`、Ruff 和合成黄金回归用于验证确定性代码路径；黄金回归中的合成案例
  通过，但这不等同于真实工程项目验收。
- 脱敏真实案例目前仍为“待提供”占位，真实案例回归数量为 0；合成案例不能替代
  真实项目覆盖，也不能据此宣称生产稳定性已经完成。
- WPS macOS、WPS Windows、Microsoft Excel macOS、Microsoft Excel Windows 真机
  验收，以及 1万/5万/20万行性能、取消恢复、签名和公证仍未在本轮执行。
- 因真实黄金案例和目标环境证据尚未补齐，本版只能作为预发行/预览候选；发布一致性
  和生产发布门禁会明确保留失败或条件状态，不得解释为生产就绪。

### Compatibility carry-over

- 继承 v0.1.13 的 Windows 符号链接测试守卫：无创建符号链接权限时测试跳过并保留原因。
- 继承 v0.1.13 的 demo 跨平台确定性：固定 ZIP 条目属性并对临时文件替换提供退避重试。

## [0.1.13] - 2026-09-02

Windows compatibility follow-up to the v0.1.12 preview; no parsing or
calculation behavior changes.

### Fixed
- Symlink-dependent tests (workspace dedup, stored-copy loop downgrade,
  import-path scan) now skip gracefully on Windows hosts without symlink
  privilege instead of failing with WinError 1314.
- Demo data generation is byte-stable across platforms: zip entries use
  create_system=3 (Unix), and antivirus-locked os.replace retries with backoff.

## [0.1.12] - 2026-09-01

Preview usability release for the Jiadun（价盾）local workbench.

### Added
- A blank-by-default project page that does not implicitly enumerate remembered
  temporary or legacy workspaces at startup; existing project files remain intact
  and can be reopened explicitly.
- Separate **打开已有项目…**, **导入资料文件…** and **导入资料文件夹…**
  actions so the project-directory picker no longer appears to be a source-file
  importer.
- Finder/Explorer drag-and-drop intake, recursive folder scanning, deterministic
  de-duplication and visible skipped-file reporting for unsupported types.
- Mixed-folder routing: XLSX/XLSM/XLS/CSV go to settlement import while DOCX/PDF/TXT
  go to contract/meeting-note extraction.
- Import feedback now separates successful, partial and failed workbooks; corrupt
  settlement files are never presented as a successful import.
- Folder intake refuses symlink roots, symlink files and broken links, with a visible
  skip reason so the imported scope stays inside the user-selected directory.
- A platform-aware application font selected once at QApplication level, with
  PingFang SC / Microsoft YaHei UI / Noto Sans CJK SC fallbacks as available.
- Persistent parse-failure batches and parse-failure Evidence for corrupt or
  unsupported settlement workbooks, keeping the project fail-closed.

### Changed
- Core project creation/opening only remembers a workspace for explicit recovery;
  it no longer silently makes an arbitrary programmatic temporary directory the
  startup workspace. Explicit UI selection still becomes the current workspace.
- The workbench keeps the existing Decimal, read-only-original and Evidence rules;
  this release changes only intake and presentation paths.

### Validation boundary
- This remains a Preview/Prerelease. WPS macOS, WPS Windows, macOS Excel,
  Windows Excel, 1万/5万/20万-row performance and cancellation, sanitized real-case
  golden regression, Developer ID signing, notarization and cross-platform install
  evidence remain open production gates.

## [0.1.11] - 2026-09-01

Preview maintenance release that closes the v0.1.10 review findings around source
copy integrity, read-only status reads and current/historical Evidence wording.

### Fixed
- Run Contract source-file components now bind the normalized `stored_path` actually
  consumed by parsers and re-hash recorded copies on current reads; overwrite,
  redirection or missing copies fail closed. Legacy contracts missing the path are
  not current and are rebound only through a new controlled run.
- Workbench status, overview, version/history and export-status refreshes use the
  read-only report model and do not persist derived line-item flags or clear a
  fail-closed sidecar as a side effect of viewing.
- Word management summaries distinguish current-range Evidence from archived
  historical Evidence instead of labelling the project-wide count as current.
- Architecture license decision now matches the accepted Apache-2.0 ADR and keeps
  the AGPL code-copy review boundary explicit.

### Validation boundary
- The release remains a Preview/Prerelease. Full target-machine performance,
  four Office environments, sanitized real-case golden regression, Developer ID
  signing and notarization remain open production gates.

## [0.1.10] - 2026-09-01

Preview release for evidence-gated settlement review and controlled local knowledge assets.

### Added
- Per-Sheet coverage proofs and immutable row classifications that bridge raw ranges,
  excluded rows, pending rows, business rows and A/B/C totals.
- Immutable run IDs and expanded Run Contracts covering source identity, sheet scope,
  mappings, periods, rules, Decimal policy, aliases, confirmations and project versions.
- Shared Sheet, period, direction and project status semantics for the UI, Excel and Word.
- BOQ difference radar, mirrored matching review, field mapping templates, versioned alias
  knowledge, configurable rule catalog and Finding lifecycle events.
- Project version chains, final-approval snapshots and traceable historical unit-price hints.
- Golden regression and release-checklist runners, plus staged 10k/50k/200k performance tooling.

### Changed
- Current-result reads fail closed when source files, raw grids, mappings, periods, rules,
  evidence or the Run Contract no longer match.
- Historical results and Evidence remain readable but are explicitly excluded from current
  project conclusions.
- Export status cards, Excel covers and Word summaries use the shared project status instead
  of recomputing a local green result.

### Fixed
- Prevented a historical Run Contract with a repeated signature from being reactivated.
- Prevented semantically identical Decimal/set rule snapshots from being reported as changed
  after canonical JSON persistence.
- Prevented registered export cards from referencing an undefined file-type variable.
- Rejected non-finite or malformed historical price values without replacing them with zero.

### Release boundary
- This remains a Preview/Prerelease. Sanitized real-case golden regression, four Office
  environments, complete target-machine performance evidence, Developer ID signing and
  notarization remain open production gates.

## [0.1.9] - 2026-08-31

Preview repackaging release for the Jiadun（价盾） product rename.

### Changed
- The distribution, Python package, CLI and macOS application are now named
  `jiadun` / `Jiadun` / `价盾`; the legacy `costguard` GUI command remains as a
  migration alias.
- New projects and exports use the Jiadun naming and paths while legacy
  CostGuard settings, workspaces, sidecars and historical exports remain
  readable and are never silently deleted or rewritten.
- Existing database schema, historical tags, the GitHub repository URL,
  `costguard-canonical-json-v1` and the `v0.1.8` release assets remain historical
  compatibility facts.
- The version is intentionally a preview release. WPS, macOS Excel, Windows
  Excel, 1万/5万/20万-row performance, migration/recovery, signing and notarization,
  and real-case regression remain open gates.

### Validation
- The rename baseline passed the repository's automated test, lint, compile,
  demo-determinism and release-asset checks before this version bump.
- This release does not claim formal production readiness or approval of any
  real project settlement.

## [0.1.8] - 2026-08-31

Preview release focused on review-gate integrity and safe recovery boundaries.

### Fixed
- Future database schema versions now fail closed instead of being opened and
  down-written by an older program.
- Direction changes, alias-backed match confirmation and batch confirmation use
  atomic core transactions with current-run rechecks.
- Failed or partial cross-check runs invalidate stale current results and cannot
  clear the project fail-closed boundary without a complete current A/B/C
  coverage proof.
- Project-level cross-checks coordinate periods across both directions,
  including identical period numbers; a partial direction rerun invalidates
  the project-wide current view until the complete project run is repeated.
- Aggregate coverage proofs are bound to the actual project periods,
  cross-check evidence and persisted period totals, and ordinary public
  detection runs cannot self-declare the aggregate validation kind.
- Batch match confirmation checks the live row again in the final conditional
  update, so a concurrent or trigger-side state change rolls back the batch.
- Manifest arrival checks now include parsed batch status, Sheet, period,
  direction and header semantics; missing `period_totals` rows and export
  registration failures are explicit failures.

### Testing
- 427 automated tests pass on the release candidate, with Ruff and compile
  checks passing. The independent code review conditionally passed for the
  addressed P0-04, P1-01 and P1-02 scope, with the partial-summary semantic
  observation documented in the release notes. WPS, macOS Excel, Windows
  Excel, large-project performance, signing/notarization, migration recovery
  and real-case regression remain separate preview-release gates.

## [0.1.7] - 2026-08-30

Productization pass (P0): no silent truncation, honest verification levels,
dual-value audit worksheet, business-language UI.

### Added
- 项目首页卡片、工作台审核总览、下一步建议、异常问题中心、左右对照匹配
  复核、工作表样例映射和导出完成度提示，统一按证据门控展示。
- Excel 审核底稿新增《封面与说明》页、冻结表头、筛选、打印重复表头、差异
  条件提示和 Evidence ID 工作簿内跳转；Word 摘要新增首屏项目状态、关键指标、
  Top 风险、待决策/待补资料和范围限制。
- 清单明细分页：500/页 + 首页/上一页/下一页/末页 + "共 N 条 / 当前显示
  X-Y 条"；>2500 行回归证明全部数据可翻页到达，导出继续读取完整数据集。
- 三档校核级别（逐期）：校核充分（A=B 且 C 控制可用且一致 且无待人工
  工作表）/ 校核有发现（A/B 或 C 差异）/ 校核不充分（C 不可用、数据不
  完整、有待人工工作表）——纯绿色"一致"措辞废除；每期展示参与累计明细
  行数、排除小计行数、排除标题说明行数、待人工确认工作表数、C 控制状态。
- Excel 审核底稿双值列：合价(底稿公式) 保留 Excel 复核值；程序计算合价
  列写入引擎 Decimal 精确值——主结论不依赖 Office 重算。
- 业务语言：方向显示 对上结算/对下结算/未标记；确定/取消 中文按钮；
  schema 版本移入 Tooltip；规则中文名完备性守卫测试。

### Changed
- 验收报告逐文件逐表新增 校核级别/参与明细/排除小计/排除标题/待人工表 列。

### Pending environments（如实）
- WPS Office、macOS Excel、Windows Excel 三环境真机重算验证待补；
  当前以结构/公式/数值可复现检查 + LibreOffice 重算（不宣称等同 WPS）兜底。

## [0.1.6] - 2026-08-30

Windows-compatibility and correctness release for macOS, driven by the
independent review of v0.1.5 and a real settlement book (229 sheets). No
business-rule changes; all discipline intact.

### Fixed
- **C-path control double-count (v0.1.5 known limitation)**: the control
  value now uses the unique grand-total row (合计/总计/累计). Without a grand
  row, page-level subtotals sum to the control (pages partition the table);
  multiple grand rows → not_available with a note; shortfalls report an
  honest diff, never balanced.
- **E.6 单位工程汇总表 misrouted as key-value form (26 real sheets)**: a
  money-header row with numeric rows in the money column now suppresses
  weak AND strong form routing — all 26 sheets reach role review with
  evidence candidates for the confirmation dialog. Genuine 封面/扉页 stay
  forms; the synthetic kv-content page still routes form.
- **Money-less sheets cannot auto-parse** (表-09-style analysis pages):
  mappings without amount AND unit_price stay at human confirmation —
  money-less half-blind rows can no longer enter the settlement model.
- **表-08 pages with colon-rich feature text** ("1.名称：…2.型号：…" in every
  row) no longer trigger strong-form blocking on small pages.
- Subtotal-label matching normalizes internal whitespace and 第N页 prefixes
  ("小     计" previously escaped detection).
- Section-title rows (name only, no numbers) are skipped as non-items with
  the skip reason recorded in the sheet report; tail-note skip reasons
  likewise.

### Changed
- Real-data acceptance (private corpus T14–T18 added; originals hash-verified
  untouched): the standard 229-sheet settlement book now auto-parses 26
  tables / 1,221 detail rows (1,096 with amounts); the remainder stays
  honestly gated with per-sheet reasons. Parsed sheet count ≠ settlement
  period count — periods are assigned per sheet at human confirmation.
- GUI crosscheck message shows the C control value/status per period and
  warns prominently when a control difference exists while A/B match.
- Local acceptance report lists per-file per-sheet status, unique-provenance
  detail counts, per-period A/B/C control values, gating counts, and states
  explicitly that A/B share the same extractor (issue #4 tracks true
  row-set independence).
- UI: unified professional visual system (theme tokens, button hierarchy,
  business-language labels, table column strategies, soft severity badges,
  grouped sheet-confirmation dialog with column hints and auto-detected
  candidate markers) — display layer only.

### Testing
- 284 tests green on macOS arm64; final private-corpus acceptance on this
  exact candidate: 18/18 imported, hashes untouched, E.6 misroutes 0,
  表-08 form misroutes 0, skip reasons recorded per sheet.

## [0.1.5] - 2026-08-30

macOS usability release: real settlement books finally import. Same shared
core engine, data model, tests and UI across platforms; no discipline rule
changed (missing values stay unfilled, amounts stay Decimal, upward/downward
isolated, originals read-only, evidence chain and human-review gates intact).

### Added
- **Human-confirmation GUI for gated sheets** (`SheetConfirmDialog`, issue #5):
  real files with header ambiguity / no header / form-like sheets used to stop
  at "needs role review" with no GUI path at all — the period overview stayed
  empty and regular users were stuck. The workbench now lists pending sheets
  (distinguishing ambiguous-header vs no-header states), prefills the detected
  candidate column mapping, lets the user set direction/period, map all 8
  fields and the header-row range, then "extract as settlement list" or keep
  it "evidence-only" (5 non-settlement roles) — reason mandatory, every action
  audited. Imports report pending counts and offer the dialog.
- **GB-template recognition** for standard settlement books (表-08
  分部分项清单 / 表-12-3): block headers of 2-3 rows resolve leaf fields per
  column (group labels like 金额（元） spanning 综合单价+合价 no longer occupy
  mappings), table-title rows cannot masquerade as leaves, and the best
  candidate across all start rows wins. On a real 229-sheet settlement book
  (private corpus) auto-parsing went from 0 pages to 25 pages / 1,211 detail
  rows; section-title rows are no longer absorbed as items.
- SUBTOTAL_WORDS adds 本页小计/本页合计; subtotal matching normalizes internal
  whitespace (排版空格 previously escaped detection).
- Acceptance corpus may now grow beyond the original 13 rows (unique ids,
  growth ≥13 guard) — five new real-copy test IDs recorded locally.

### Fixed
- E.6-style 单位工程汇总表 no longer misroutes as a key-value form when a
  money-header row exists; it reaches the semantic gate (needs role review)
  with an evidence candidate instead of a dead end.
- Sheets whose mapping lacks any money column (表-09-style analysis pages)
  no longer auto-parse — money-less half-blind rows cannot enter the
  settlement model; they stay at human confirmation.
- form misjudgments on the real book dropped 45 → 29 (remaining ones are
  genuine cover/title pages).

### Known limitation
- The C-path control sums ALL subtotal rows: real books with both 本页小计 and
  合计 double-count the control. Distinguishing page subtotals from grand
  totals is planned together with issue #4 (A/B same-source) work.

### Testing
- 262 tests passed on macOS arm64 (3 red-first GB-template tests, 5 dialog
  tests); real-corpus before/after comparison recorded in the PR; DMG rebuilt
  through all gates (privacy audit PASS, mount self-check); install → launch
  → quit → relaunch regression and anonymous-demo E2E pass.

## [0.1.4] - 2026-08-30

### Fixed
- Custom workspaces selected while creating or manually opening a project are
  now persisted. Restarting the app no longer makes those projects disappear
  from the project list.
- The project list scans both the default workspace and all remembered
  workspaces, deduplicates project databases, and reads project metadata without
  triggering a database migration.
- A moved project opens from the directory the user selected or the app found,
  instead of trusting a stale `workspace_path` stored inside `project.db`.
- Global settings use atomic replacement so an interrupted write cannot leave a
  partially written `settings.json`.

### Recovery
- This defect hid projects from the list; it did not delete `project.db` or the
  imported read-only copies. If a workspace was created with v0.1.2 and is not
  listed after upgrading, use **打开项目目录…** once and select the directory
  containing `project.db`. The app remembers its parent workspace thereafter.

### Testing
- 254 tests passed, including restart simulation, multi-workspace discovery,
  idempotent registration, moved-project reopening, a schema v3 → latest copy
  migration that preserved all record counts and imported-file hashes, and
  listing guarantees: no WAL/SHM sidecars created on scan, committed data in an
  active WAL still discovered (via temp-copy read) with source bytes and mtimes
  untouched, read-only directories still discoverable, corrupted settings.json
  archived and recovered, symlinked workspace roots deduplicated, and
  originals paths repaired after a project directory move.

## [0.1.3] - 2026-08-30
Bugfix release driven by real-world Windows + municipal payment-report feedback
(issues #1, #2, #3, #6). No engine-semantics changes beyond the fixes below.

### Fixed
- **#1 Windows crash on startup/test collection**: `paths.py` used the
  nonexistent `pathlib.Path.expandvars`; `APPDATA`/`USERPROFILE` are now read
  from the environment with sane fallbacks (no literal `%APPDATA%` junk paths).
- **#3 grand-total/footnote rows absorbed into detail (7.8× amount inflation)**:
  "分部分项合计" is now matched atomically as a subtotal label (plus "第…"-
  prefixed section totals); the "总计" label in the index column is detected by
  scanning every leading column; consecutive trailing orphan-numeric footnote
  rows are excluded from detail (originals retained in the fidelity layer).
- **#6 scanned PDFs failed silently**: a zero-text-layer PDF now raises
  `NotImplementedError` (surfaced by the runner's expected-limit channel and
  the GUI) instead of returning ok/0 paragraphs/0 facts.
- **#6 text-number alert flood**: `text_number_in_value_col` findings now
  aggregate one per sheet×field with full row lists in details (a real
  workbook went from 344 findings to 3), raw-value samples retained.
- **#2 (short-term) dictionary gap**: added 子目号/细目号/子目编码/细目编码 code
  aliases and 子目名称/细目名称 name aliases; contains-terms deliberately
  exclude bare 子目/细目 so the 子目名称 column cannot be misclassified.

## [0.1.2] - 2026-08-30

First runnable DMG preview for macOS Apple Silicon regular users. No Python/uv
required; ships with a fully synthetic anonymous demo corpus.

### Added
- **macOS arm64 DMG build** (`scripts/build_macos_arm64.sh` +
  historical path `src/costguard/platform/packaging/macos_arm64.spec`): PyInstaller onedir app
  (arm64-native, ad-hoc signed, NOT notarized), DMG with Applications symlink,
  built-in 三分钟上手 RTF and 匿名演示数据 folder, mount smoke test, SHA-256
  output; every gate exits non-zero on failure. Minimum macOS 15.0 (measured
  from bundled PySide6 binaries).
- **Privacy gate** (`scripts/audit_bundle_privacy.py`): scans the uncompressed
  bundle and the decompressed PYZ bytecode for the build machine's username,
  HOME, repo path, LAN prefixes and `local_private_data`; the build fails on
  any hit. Verified PASS on the built app.
- **Anonymous demo data** (`examples/demo`, generated by
  `scripts/generate_demo_data.py`): fixed-seed, byte-reproducible synthetic
  workbooks (upward 3 periods / downward 3 periods / appendix with a
  deliberately role-gated 人材机汇总 sheet) + synthetic contract docx;
  manifest.json with SHA-256, directions, expected rows/anomalies/matching,
  known limitations and a 28-scenario coverage matrix; SHA256SUMS.
- **One-click demo** in the project list (体验匿名演示): creates a demo project
  with auto-suffix (never overwrites), imports bundled demo files per manifest
  direction, keeps read-only copies, sanity-checks upward/downward periods.
- **3-minute onboarding** (docs/QUICKSTART_zh-CN.md, also converted into the
  DMG welcome RTF) + bilingual README entries + real-app screenshots
  (`scripts/generate_screenshots.py`, WA_DontShowOnScreen renders of the
  running app; exported files rendered via macOS QuickLook).
- Manual packaging workflow `.github/workflows/package-macos-arm64.yml`
  (workflow_dispatch): lint → full tests → demo determinism → build → CI-level
  sensitive scan → artifact upload. It never creates Releases or tags.
- Table polish in the workbench (numeric right-alignment, bold headers,
  content-sized columns) and an explicit 「在 Finder 中显示导出目录」 action.

### Changed
- The project list shows project names only; absolute paths moved to tooltips
  (no machine paths visible on screen or screenshots).
- Main-window geometry persists across launches (QSettings).

### Testing
- 226 tests passed (14 demo-data acceptance tests lock byte determinism,
  manifest consistency, direction isolation, NULL-vs-zero semantics, role
  gating, anomaly-rule sets, matching levels, contract quotes, exports; 5
  demo-entry tests cover provisioning and privacy-preserving list rendering).

## [0.1.1] - 2026-08-30

### Fixed
- The README-documented `uv run pytest` now works on a fresh clone: added
  `pythonpath = ["."]` to the pytest configuration. `tests/` has no
  `__init__.py`, so tests that import `scripts.*` / `tests.*` modules need the
  repo root on `sys.path`. CI was green because it runs `python -m pytest`
  (which puts the cwd on `sys.path` implicitly), while a fresh clone running
  `uv run pytest` got 2 failures + 9 `ModuleNotFoundError` errors. No test
  assertions were changed.

## [0.1.0] - 2026-08-30

First open-source developer preview. It provides the seven-step settlement workflow,
macOS Apple Silicon GUI, source distribution and Python wheel. A signed/notarized DMG
is not included in this release.

### Added
- **Phase 1** framework: platform abstraction (macOS arm64 first), SQLite with
  versioned migrations + auto-backup + rollback, Decimal money engine, project
  lifecycle, read-only source imports (SHA256 dedup), PySide6 shell
- **Phase 2** Excel structuring: fidelity layer (formulas, cached values, merged
  cells, hidden rows/cols, text-numbers), header detection (multi-row, scored
  dictionary, confidence), item extraction with cell-level provenance, synthetic
  defect generator (22 scenarios)
- **Phase 3** settlement engine: Decimal cumulative aggregation, weighted avg
  price, missing-period detection (incomplete — never zero-filled), A/B dual-path
  cross-check + raw subtotal check, per-sheet period assignment (migration v2)
- **Phase 4** anomaly engine: 21 rules with severity grading (qty×price mismatch
  vs rounding difference, duplicates, suspected duplicate settlement, cross-period
  price/tax/unit changes, qty spikes, hidden cells, formula errors/missing cache,
  text numbers in value columns, merged cells in data, missing key fields)
- **Phase 5** evidence chain + audit log (reason mandatory) + item matching with
  5-level confidence (confirmed/probable/suspected/incomparable/pending_data),
  alias library, override API
- **Phase 6** contract analysis: docx/pdf/txt parsing, deterministic clause
  extraction with mandatory quotes/locations, contract risk checklist
- **Phase 7** export: 12 report sheets (audit worksheet preserves formulas with
  row-ID provenance; LibreOffice real-recalc verified; OOXML/WPS structure checks)
- **Phase 8** Mac UI: project page + 5-tab workbench (periods, items with cell
  provenance, anomalies, match review with reason dialogs, export)

### Changed (supervision gates for Phase 7)
- Migration v3: settlement period uniqueness relaxed to
  `(project_id, period_no, direction)` — upward/downward keep independent period
  number sequences (table rebuild per SQLite ALTER limitations)
- Export serialization boundary `_num`: Decimal is written natively (exact
  decimal XML string, zero binary error); float conversion only via explicit
  constant branch (money pre-rounded with round2 if ever required); missing
  values strictly `is None` — Decimal("0") preserved
- Up/down cumulative reports aggregate per direction and key by period_id
  (prevents cross-direction merging at equal period numbers)
- Management summary (xlsx + docx) declares scope: source-file count, period
  count with direction split, tax mode status, evidence record count; docx no
  longer claims full traceability without an evidence entry (points to the
  Excel evidence index instead); synthetic-data disclaimer added
- Migration v5 scopes confirmed item aliases by settlement direction. Legacy
  aliases migrate to `unknown` and must be reconfirmed before use in an upward
  or downward workflow; the same alias text may be confirmed independently per
  direction.
- `period_totals` now persists raw amount, calculated candidate, calculated
  fallback actually used, effective amount, amount source/status, A/B status and
  difference, and C/control status and difference. Compatibility fields remain
  populated but cannot hide an open control difference.
- Excel anomaly and pending-review sheets now carry an explicit direction column
  for line-item, period, and sheet findings; project-level findings are labeled
  accordingly.

### Fixed
- Migration failure recovery now restores the pre-migration database and records
  the schema version without leaving a half-migrated database.
- Re-aggregating a period now invalidates stale A/B/C differences, statuses, and
  evidence links before a new cross-check, preventing old results from being read
  as current validation.
- Sheet-level anomalies now retain their settlement direction in the desktop UI,
  matching the Excel anomaly export.
- Direct historical `costguard` startup now creates the real main window and remains in the
  Qt event loop on macOS arm64.
- The WPS acceptance generator now writes to the repository-local ignored workspace
  instead of a developer-specific `.zcode` path.
- LibreOffice headless conversion remains environment-dependent. In the current
  controlled run it returned rc=1 without output, so that test is reported as
  skipped and is not used as evidence that WPS passed.

### Real-data acceptance (formal round)
- 13/13 private copies imported, hashes verified before/after (zero modification);
  private corpus and outputs remain excluded by `.gitignore`.
- R05–R07 completed the settlement execution pipeline. A/B independent Decimal
  recomputation matches; R05 retains a 1,680.00 tax bridge to C, while R06/R07
  retain source-cache/control differences as open findings without balancing.
- R04, R11 and R12 completed text extraction but require page/paragraph source
  review. R01, R02 and R08–R10 remain at explicit spreadsheet role-review gates;
  legacy `.doc` and image parsing remain unsupported with recoverable messages.
- The synthetic minimum WPS sample passed manual verification on 2026-08-29.
  The latest R05–R07 private real-data exports remain a separate conditional WPS
  open/recalculate/save/reopen gate and are not marked as accepted.

### Testing
- Latest controlled run: 207 passed. WPS remains an explicit manual compatibility
  and visual gate rather than being inferred from unit tests.
- Targeted regressions prove that an upward alias is never applied to downward
  items, A/B and control states survive persistence, anomaly exports retain
  direction, and Decimal warning groups force `with_findings` rather than an
  unconditional pass.
- Second-path reconciliation: exported workbook re-read and recomputed with
  Decimal (detail sums vs cumulative sheet), independent of LibreOffice
