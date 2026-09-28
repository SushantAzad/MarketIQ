# Phase 3 financial data contract

PostgreSQL is now the financial system of record. The SQLite file remains an ingestion progress journal; importing it is a bridge, not a PostgreSQL substitute.

## Storage and lineage

The initial Alembic revision defines companies, securities, source_objects, snapshot_observations, filings, financial_statements, financial_facts, fact_sources, normalization_runs, rejected_facts, financial_metrics, metric_inputs, data_fetch_logs, and data_source_state. Later-phase market, ML, and research tables are not claimed as implemented.

Every fact has an exact NUMERIC value, unit, taxonomy/concept, actual period dates, filing accession, normalization version, and one or more source observations. Each source observation has a content hash, original URL, and JSON Pointer into the original immutable companyfacts response. A primary filing link is used when discovered; otherwise the actual accession's index URL is supplied. No primary filename is invented.

Imports are transactional per source snapshot and serialized per issuer through PostgreSQL advisory locks. Stable identifiers and unique constraints make replay safe. Unchanged facts can retain multiple source pointers without being duplicated. Source-observation timestamps preserve the sequence when identical content disappears and later reappears. The original raw objects remain under `data/raw/`, outside Git.

## Periods and availability

- SEC `fy` and `fp` are stored as **filing_fiscal_year** and **filing_fiscal_period**. They are not used to infer the year of every comparative fact in that filing.
- Instant facts have no start date. Duration classes are explicitly heuristic day-count bands: quarter 70–110 days; six-month YTD 160–210; nine-month YTD 250–300; annual 330–385; otherwise `duration_other`. Original dates always remain available.
- Six/nine-month cash flow is never silently relabeled as a discrete quarter. Quarter derivation, TTM assembly, ratios, and other financial calculations are deferred to Phase 8.
- Availability uses the known SEC acceptance timestamp. When only a filing date is known, the conservative fallback is the next midnight in America/New_York, respecting daylight saving.
- `--as-of` additionally requires that the system actually observed the selected source snapshot by that cutoff. A snapshot downloaded today cannot prove what was known years ago. This is deliberately conservative; historical ML training still needs a defensible point-in-time corpus.
- Restatements and amendments remain separate accessions. For a requested period, the most recently available compatible filing is preferred. Conflicting same-period facts return UNAVAILABLE, not an arbitrary number.

## Supported reads

Named metrics resolve documented US-GAAP concepts: revenue, net income, operating income, gross profit, assets, liabilities, equity, current assets/liabilities, cash, operating cash flow, and capital expenditures. Revenue uses a declared tag preference order within the latest reporting context. Unknown/custom taxonomy facts are retained but not silently mapped into these metrics. Financial statement rows group extracted facts by statement type and context; they are not a rendered replica of the full published statements.

Queries require an explicit period basis and currency/unit (default USD). No currency conversion is performed. Empty, conflicting, or incompatible data returns an unavailable reason. Decimal values are serialized as strings to preserve precision. Returned provenance includes the selected concept, period, accession, source pointer, content hash, observation/fetch dates, and freshness. RECENT means recently checked, not live or recently ended accounting periods.

Malformed facts, unsupported dimensional contexts, and future-dated fact periods are retained in raw sources and counted in rejected_facts with source pointers. A normalization run with rejections is marked partial. Rejections are never silently converted to zero. The financial_metrics and metric_inputs tables are reserved for Phase 8 outputs and currently remain empty.

## Scope and limits

`database sync` refreshes SEC metadata and companyfacts before import; `database import-journal` explicitly operates offline. `database query` is a read of stored data with freshness, not an implicit upstream request. Use sync before querying when a fresh provider check is required. The eventual public API will orchestrate refreshes centrally.

The SEC poller and PostgreSQL sync are currently separate commands. Scheduled SQL refresh, distributed workers, public APIs, and dashboard wiring remain later work. The local securities registry reflects current SEC ticker mappings; it is not yet a historical corporate-action/share-class master. Parser/normalizer changes require a new version and a reviewed migration where appropriate.
