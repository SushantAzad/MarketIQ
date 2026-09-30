# Phase 9: typed query routing

`query-router-v1` is a conservative rules-based planner and internal executor. It has no LLM classifier, generated SQL, arbitrary callable names, public HTTP endpoints, or durable workflow jobs. Phase 10 adds orchestration; Phase 13 adds the API; **Phase 14 designs and builds the React terminal/dashboard, source viewer, responsive states and browser journeys**.

## Planning

`QueryRequest`, `Period`, `Issuer`, `Step`, and `QueryPlan` are Pydantic models that reject extra fields. Intents and financial metrics/formulas are allowlisted. Plans resolve imported tickers and legal names from PostgreSQL; aliases include legal names with common corporate suffixes removed. Brand aliases that cannot be derived from those names are not guessed. Multiple share classes of the same issuer require an explicit ticker. At most four issuers and 24 steps are permitted.

Recognized metric phrases include revenue, net income, operating cash flow, capex, margins, growth/YoY/CAGR, FCF, ratios and returns. Long phrases take precedence over their component words, so “net profit margin” routes to one margin calculation. A growth request must identify exactly one reported input metric and an explicit comparison date. A question can route to multiple facts/calculations and to document evidence. This finite grammar is not unrestricted natural-language understanding; unresolved or ambiguous requests return `NEEDS_CLARIFICATION` and execute no steps.

Financial reads require an exact period end and reporting basis. A single ISO date in a question explicitly saying “annual” or “quarterly” can supply that scope. A calendar year alone never identifies a fiscal period. Unknown issuers, conflicting explicit ticker scopes, ambiguous metrics, negated routing phrases, missing reporting dates, and incomplete comparisons require clarification. Explicit request fields are visible in the returned plan. Use `--as-of` with a timezone-aware timestamp for historical structured facts/calculations; this remains separate from the reporting period.

Document questions require a filing form. Without an accession, a plain current document request uses the research service's latest locally discovered filing behavior; it does not refresh SEC. Historical year/relative-period requests require an explicit accession. Combined metric explanations also require an accession, and the executor verifies that its issuer, form and report period match the financial request before reading evidence. Historical document cutoffs are not supported by the existing research path, so such steps explicitly return unavailable rather than reading current evidence as historical.

## Execution and comparisons

- `FINANCIAL_METRIC` uses the existing period/cutoff-aware SQL fact selector.
- `FINANCIAL_CALCULATION` uses Phase 8 formulas and persists their operand lineage.
- `DOCUMENT_RESEARCH` and `GENERAL_COMPANY_INFORMATION` use the cited filing research service. Company-information questions return source evidence, not invented metadata.
- `COMPARISON` aligns the selected financial results. The Python request model supports per-issuer periods through `issuer_periods`; the CLI supplies one shared period.
- `MARKET_DATA` and `RISK_ANALYSIS` return explicit unavailable reasons until their respective provider/approved model exists. No placeholder quotes or risk probabilities are generated.

Structured steps share a PostgreSQL REPEATABLE READ transaction and one cutoff. Each step has a savepoint. A document/model failure does not remove successful facts or calculations. Fact-only and calculation-only requests never load embedding/reranker/generative models.

Comparison checks require values for every requested issuer and identical units, basis, start and end dates, and comparative-period dates where applicable. Different fiscal year ends or missing observations yield `UNALIGNED`; the values and their actual dates remain visible. No spread, rank, calendar conversion, or claim of comparability is generated for mismatched periods.

The response separates `retrieved_facts`, `calculated_values`, `document_evidence`, `market_data`, `model_outputs`, `generated_explanation`, and explicit unavailable steps. Overall status is `AVAILABLE`, `PARTIAL`, `UNAVAILABLE`, or `NEEDS_CLARIFICATION`. `AVAILABLE` means requested data/evidence is present; it does not certify relevance or imply that a prose explanation was generated. Generative explanation remains null. Existing calculation/research runs retain their own IDs and source traces; routing plans are returned but are not yet persisted as workflow runs.

## CLI examples

Run from the repository root against the existing local services:

```powershell
uv run --locked --directory backend python -m app.routing plan "NVDA revenue and net margin" --basis annual --period-end 2026-01-25
uv run --locked --directory backend python -m app.routing ask "NVDA revenue and net margin" --basis annual --period-end 2026-01-25
uv run --locked --directory backend python -m app.routing ask "Compare META and TSLA revenue" --basis annual --period-end 2025-12-31
uv run --locked --directory backend python -m app.routing ask "Explain NVDA net margin" --basis annual --period-end 2026-01-25 --form 10-K --accession 0001045810-26-000021
uv run --locked --directory backend python -m app.routing plan "NVDA revenue in 2025"
```

The last example returns clarification. Source coverage depends on the imported snapshots. No migration or dependency installation is required for Phase 9.
