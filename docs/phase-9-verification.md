# Phase 9 verification — 2026-09-30

Implemented typed, allowlisted intent planning; issuer resolution from imported metadata; bounded internal dispatch; multi-intent result separation; and explicit financial comparison alignment. The CLI exposes `python -m app.routing plan` and `ask`. No dependencies or schema migration were added.

Validation:

- Full regression suite: **271 passed**, **94.64% application coverage**, 103.28 seconds, with real PostgreSQL and Qdrant.
- After adding final currency/date/cutoff conflict checks, the complete routing unit/integration subset passed: **34 tests**.
- Ruff lint/format and strict mypy passed for **45 application modules**.
- Tests cover representative fact, calculation, market, risk, document, company-information, comparison and combined intents; ambiguous issuers/years/metrics; strict model allowlists; conflicting scopes; unavailable services; preservation of financial results after document failure; accession/report-period checks; and explicit refusal of historical document cutoffs.

Eight real-data cases passed using the existing stored source snapshots:

1. NVIDIA annual revenue: available sourced fact.
2. NVIDIA net margin: available persisted calculation.
3. META versus TSLA annual revenue ending 2025-12-31: aligned actual periods.
4. NVIDIA ending 2026-01-25 versus Microsoft ending 2026-06-30 net margin: partial response with unaligned fiscal periods, preserving both values and dates.
5. NVIDIA revenue “in 2025”: clarification; no invented fiscal endpoint.
6. NVIDIA revenue plus stock price: fact retained, unavailable market step.
7. NVIDIA risk score: unavailable approved risk model; no synthetic probability.
8. Explain NVIDIA net margin, scoped to accession `0001045810-26-000021`: calculation and real cited filing evidence returned separately.

The checks used local PostgreSQL and Qdrant, with no SEC refresh. The generative model remains disabled. Evidence presence is not a relevance-quality evaluation, and the response does not claim a generated causal explanation.

Full local output is in ignored `data/routing-verification.json`. Reproduce from the repository root:

```powershell
$env:PYTHONPATH="backend"
uv run --locked python scripts/verify_routing.py
```

Remaining planned work: Phase 10 durable LangGraph orchestration; Phase 13 public API; **Phase 14 React dashboard design and implementation**. The current frontend placeholder is unchanged.
