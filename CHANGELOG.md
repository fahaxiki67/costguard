# Changelog

All notable changes. Format based on Keep a Changelog; versioning: SemVer.

## [Unreleased]

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

### Testing
- 129 tests + 1 explicit skip: WPS has no headless xlsx recalc CLI (wpscli is
  PDF-conversion only) — real-engine recalculation is verified via LibreOffice
  (same OOXML formula spec); WPS manual-open verification remains a human step
- Second-path reconciliation: exported workbook re-read and recomputed with
  Decimal (detail sums vs cumulative sheet), independent of LibreOffice


