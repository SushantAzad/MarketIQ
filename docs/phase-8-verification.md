# Phase 8 verification — 2026-09-28

Implemented versioned Decimal formulas, period/cutoff validation, explicit unavailable reasons, CLI calculation/readback commands, and PostgreSQL dependency tracing. Applied additive migration `599d1b9a7129` to the existing local database. Prior source facts and retrieval generations were preserved.

Validation results:

- Full regression suite: **239 passed**, **94.63% application coverage**, 98.06 seconds, including real PostgreSQL and Qdrant integration tests.
- Final targeted calculation suite: **39 passed**, including explicit period selection for YoY and derived quarters, after isolating the Decimal context from ambient settings.
- Ruff lint/format and strict mypy passed for 40 application modules. Migration round-trip/schema checks passed on isolated test schemas. Downgrade refuses when derived-metric dependencies exist, preserving their traces.
- Tests cover exact amounts above the binary-float safe integer range, growth/CAGR baseline and endpoint edges, annual spacing, non-overlapping compatible durations, margins, FCF payment signs, missing debt components, current ratio, average opening/closing balances, explicit EBITDA operands, incompatible issuers/currencies/cutoffs, NaN/float rejection, and quarter/YTD differentiation.
- PostgreSQL tests verify fact and intermediate-metric foreign-key lineage, deterministic replay, saved response round-trips, unavailable results, and earlier results remaining unchanged after a later restatement.

The real-data script checked six calculations across all seven stored issuers (NVDA, MSFT, AAPL, AMZN, GOOGL, META, TSLA): **42 saved/read-back records**, **27 available values**, **15 explicit missing-input results**. Net margins and available FCF values were independently recomputed with Decimal from the selected operands. All available inputs retained SEC source hashes and fact IDs. No upstream refresh occurred.

The missing-input results are substantive: NVIDIA FCF lacks the selected capex concept for the tested period, and the strict EBITDA and debt-component definitions lack required inputs across this seven-issuer check. These values were not substituted, estimated, or zero-filled. Supporting the formulas does not imply source coverage for every issuer.

Reproduce the real-data check from the repository root:

```powershell
$env:PYTHONPATH="backend"
uv run --locked python scripts/verify_calculations.py
```

Full local records are in ignored `data/calculation-verification.json`, and every known-issuer result is persisted in PostgreSQL. This verifies deterministic arithmetic and trace preservation over stored facts; it does not independently audit issuer statements. The frontend remains a placeholder. Next: Phase 9 query routing.
