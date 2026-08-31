# Changelog

All notable changes. Format based on Keep a Changelog; versioning: SemVer.

## [Unreleased]

Release gate follow-up for v0.1.8 remains in progress. The current candidate
contains the P0 evidence/range gates and the UI review workflow improvements;
WPS, macOS Excel and Windows Excel real-environment checks, large-project
performance baselines, Developer ID/notarization, migration/recovery checks and
real-case regression are still required before a production designation.

The candidate also adds a signed Run Contract for source files, sheet scope,
field mappings, calculation inputs, rule configuration, schema and code
version. Current cross-check, anomaly, matching and Excel/Word export records
are filtered by that signature; earlier records remain available as history
and are not silently reused. Findings now carry stable `finding_id` and
`fingerprint` values, raw/normalized values, confidence, detection mode,
impact, limitations, recommendation and suppression reason. The repository
also includes repeatable large-list benchmarking and release-consistency
checks; measured 1/5/20万-row baselines remain a release gate until executed
on the target environment.

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
- Manifest arrival checks now include parsed batch status, Sheet, period,
  direction and header semantics; missing `period_totals` rows and export
  registration failures are explicit failures.

### Testing
- 414 automated tests pass on the release candidate, with Ruff and compile
  checks passing. WPS, macOS Excel, Windows Excel, large-project performance,
  signing/notarization, migration recovery and real-case regression remain
  separate preview-release gates.

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
  containing `project.db`. CostGuard remembers its parent workspace thereafter.

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
  `src/costguard/platform/packaging/macos_arm64.spec`): PyInstaller onedir app
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
- Direct `costguard` startup now creates the real main window and remains in the
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
