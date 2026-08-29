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

### Testing
- 121 tests: unit + integration + UI (offscreen); hypothesis property tests for
  money paths; synthetic defect corpus end-to-end

