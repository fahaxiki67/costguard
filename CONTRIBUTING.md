# Contributing to Jiadun（价盾）

## Ground rules (enforced)
1. Read `AGENTS.md` first — its principles override any code convenience.
2. Original user files are read-only, always.
3. Money/quantity math: `Decimal` only. `float` in amount paths fails review.
4. Real project data never enters the repo. Synthetic data only (`synthetic_test_data/`).
5. Follow the roadmap phases; no unrelated refactors.

## Dev setup (macOS)
```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
.venv/bin/pytest            # unit + integration
.venv/bin/ruff check src tests
```

## Before every PR
- `pytest` green; new logic covered by tests (property-based for money paths)
- `ruff check` clean
- No new dependency without an ADR (license, maintenance, macOS arm64 + Win x64 wheels)
- Docs updated (`ARCHITECTURE.md`, ADR, `CHANGELOG.md`)

## Reporting bugs
Open an issue with: platform, app version, steps, expected vs actual,
**anonymized/synthetic** sample files only. Never attach real contract or
settlement data.
