# Changelog

All notable changes. Format based on Keep a Changelog; versioning: SemVer.

## [Unreleased]

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
