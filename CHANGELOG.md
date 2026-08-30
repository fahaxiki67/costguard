# Changelog

All notable changes. Format based on Keep a Changelog; versioning: SemVer.

## [Unreleased]

## [0.1.5] - 2026-08-30

First Windows x64 installer. Same shared core engine, data model, tests and UI
as macOS — platform differences stay inside `platform/` and packaging
(AGENTS.md: not two separate applications).

### Added
- **Windows x64 packaging** (`scripts/build_windows_x64.ps1` +
  `platform/packaging/windows_x64.spec` + Inno Setup `windows_x64.iss`):
  PyInstaller onedir app (x64, `icon.ico` from the same master as the macOS
  icon), portable zip, and a setup installer that installs per-user or to
  Program Files, creates Start-menu/desktop shortcuts, and never touches user
  data under `Documents\CostGuardProjects` on uninstall. No background
  services, no auto-update. Unsigned — SmartScreen may prompt on first run;
  documented honestly, no "no prompt" claims.
- **Manual packaging workflow** `.github/workflows/package-windows-x64.yml`
  (workflow_dispatch, real `windows-latest` runner): lint → full pytest →
  demo determinism → build → PE x64 check → privacy audit → anonymous-demo
  end-to-end (import → standardize → Decimal checks → matching → direction
  isolation → anomalies → Excel/Word export) → silent install / launch / quit /
  uninstall smoke → sensitive-data scan → SHA256SUMS → artifact upload.
- **Windows test job** in `ci.yml`: every PR now runs the full suite on a real
  Windows runner alongside macOS.
- **Sheet confirmation GUI** (`SheetConfirmDialog`, ported from the Windows
  collaborator patch, issue #5): gated sheets (header ambiguity / no header /
  form-like) can be confirmed in the app — pending list with states, candidate
  column-map prefill, direction/period and 8-field column mapping, header-row
  range, mandatory reason with audit; "extract as settlement list" or
  "evidence-only (non-settlement role)". Import reports pending count and
  offers the dialog.
- `SUBTOTAL_WORDS` adds 本页小计/本页合计 (per-page subtotal rows in payment
  reports).
- `_normalize_zip` pins zip entry `create_system=3` and retries `os.replace`
  with backoff on `PermissionError` (antivirus file lock) — demo data
  regenerates byte-identical on Windows and macOS (issue #9 scenario).

### Provenance
- The three ported changes come from the Windows collaborator delivery package
  (SHA-256 768e8b9f…32c), whose source snapshot matches upstream v0.1.3
  (6a3da73) exactly outside the patch; provenance and forensic review recorded
  in the PR. The package's own documents carried inconsistent v0.1.2/v0.1.3
  labels — the correct lineage is: v0.1.3 baseline + the three enhancements
  above.

### Changed
- No discipline rule changed: missing values stay unfilled (never 0), amounts
  stay Decimal, upward/downward stay isolated, originals stay read-only, and
  the evidence chain / human-review gates are unchanged (locked by the shared
  test suite, now green on both macOS arm64 and Windows x64 runners).

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
