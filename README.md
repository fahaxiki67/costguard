# CostGuard

**Engineering Cost & Compliance Intelligence Workbench** (工程经营合规智能工作台)

CostGuard is a local-first desktop application for construction cost engineers and
contract/compliance staff. It turns repetitive settlement reconciliation,
contract review and management reporting into a standardized, auditable pipeline:

```
import files → parse → clean → structure → compute → detect anomalies
→ link contract clauses → build evidence chain → human review → export Excel/Word
```

> **Status: pre-release, private development (Phase 0–2).** See [ROADMAP.md](ROADMAP.md).

## Why CostGuard

- **Accuracy over automation rate.** Deterministic math (quantities × unit price ×
  amounts, tax, cumulative totals) is always computed by the program with exact
  `Decimal` arithmetic — never by an LLM, never by float.
- **Your original files are never modified.** Imports are copied to a read-only
  store; all outputs go to a separate workspace.
- **Every number is traceable.** Conclusions link back to
  `file → sheet/page → cell/paragraph → original content` via Evidence IDs.
- **No silent fixes.** Missing data is marked `待补资料` (pending), incomparable
  data `不可比` (incomparable) — never auto-filled with 0, never force-balanced.
- **Human in the loop.** Uncertain matches carry confidence levels and enter a
  review queue; every manual correction is logged with a reason.

## Platform priority

1. **P0 — macOS Apple Silicon (arm64)** — current focus
2. **P1 — Windows x64** (same shared core, platform layer isolates differences)
3. P2 — Intel mac / Windows ARM64 / Linux: decided later

## Tech stack

Python 3.12 · PySide6 · SQLite · openpyxl/xlrd · pandas · rapidfuzz · pdfplumber · python-docx

## License

Not yet decided (GPL-3.0 vs Apache-2.0, see `docs/adr/ADR-009`). The repository is
private until the license decision is made.

## Documentation

- [中文说明 README_zh-CN.md](README_zh-CN.md)
- [Architecture](ARCHITECTURE.md) · [Roadmap](ROADMAP.md) · [AGENTS principles](AGENTS.md)
- ADRs: `docs/adr/`

## Reporting issues

See [CONTRIBUTING.md](CONTRIBUTING.md). Security-sensitive reports: [SECURITY.md](SECURITY.md).
