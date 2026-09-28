# Phase 2 — SEC discovery and document ingestion

Implemented and checked on 2026-09-28 local time (2026-09-27 UTC).

## Implemented behavior

- Resolve requested tickers from the SEC registry; initial seven tickers are configuration defaults, not hardcoded financial data.
- Discover 10-K, 10-Q, 8-K and amendments from current submissions; optionally follow historical submissions pages.
- Fetch primary filing documents and raw companyfacts with an identifying User-Agent, explicit SEC host/path allowlist, no redirects, decoded-body size bounds, timeouts, bounded exponential retries, and persisted server cooldowns.
- Preserve raw source bytes and parsed JSON by SHA-256; retain source URL/version/fetch history and immutable source-object provenance.
- Persist discovered/downloaded/processing/parsed/failed stages in a local SQLite ingestion journal. Unique issuer/accession keys prevent duplicate discovery. Interrupted downloaded stages reuse verified raw content; invalid document content is eligible for refetch. Corrupt artifacts are surfaced for investigation.
- Parse text and text tables while removing executable/hidden elements. Identify heuristic Item headings with normalized-text offsets. Never invent page numbers.
- Expose one-shot, polling, status, ticker, form, historical-discovery, and bounded-processing commands. Repeated bounded runs drain pending work. A local writer lock prevents concurrent ingestion commands sharing the same state directory.
- Record structured logs and source freshness without labeling SEC fundamentals LIVE. Failed fetches retain prior successful version pointers and are shown as STALE when a prior version exists.

## Live evidence

Live access used the contact from the ignored local `.env`; that value is not copied into this report. All seven companies' submissions and companyfacts endpoints returned HTTP 200. The live runs discovered **652 unique relevant filing records** and downloaded/parsed **9 primary documents**. Discovery is not full document ingestion: 643 documents remained discovered-only after the smoke checks.

Successfully parsed source accessions:

- NVIDIA: `0001045810-26-000078` (8-K), `0001045810-26-000075` (10-Q), `0001045810-26-000021` (10-K).
- Microsoft: `0001193125-26-380280` (8-K).
- Apple: `0001140361-26-035325` (8-K/A).
- Amazon: `0001104659-26-107526` (8-K).
- Alphabet: `0001193125-26-342390` (8-K).
- Meta: `0001628280-26-050705` (10-Q).
- Tesla: `0001628280-26-049270` (10-Q).

These records and exact original source URLs are retained in `data/sec/journal.sqlite3`; objects are under `data/raw/`. The second live run skipped NVIDIA's previously parsed document and processed the next pending filing, confirming real replay behavior. The live sample did not exhaustively validate every parsed table/section or backfill all older filings.

## Automated verification

The pytest suite covers configuration, URL/path rejection, malformed responses, unsupported media, response size bounds, 429/503 and transport retries, source fallback freshness, immutable source versions, duplicate discovery after journal reopen, document parsing, corrupted-object detection, downloaded-stage recovery, parse-failure refetch, historical discovery, issuer identity, form filtering, backlog progress, and CLI opt-in/status/watch behavior. Network fixtures exist only in tests; production has no mock financial responses.

Final local results: **71 tests passed**, **94.18% application statement coverage**, Ruff lint/format passed, strict mypy passed for 14 application files, and locked dependency synchronization passed. Run `uv run --locked pytest --cov=app --cov-fail-under=85`, Ruff lint/format checks, and strict mypy from the repository root. CI has not run remotely.

## Boundaries and next phase

This is a working local ingestion stage, not the complete platform. SQLite stores ingestion metadata only; PostgreSQL fact normalization and financial schemas are Phase 3. Companyfacts are raw, not normalized financial statements. No embeddings, RAG, financial calculations, ML, market quotes, or frontend integration have been added.

Only primary documents are processed in this phase; exhibits, reliable section/anchor mapping, distributed Celery scheduling and a cross-host Redis limiter remain future work. `watch` runs only while its command is running; no background service has been installed or left running. Use a single state directory/host for live SEC access until distributed coordination is implemented.

The SEC publishes access guidance and API schemas at [EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) and [Developer Resources](https://www.sec.gov/about/developer-resources). The local configuration's default limit is 5 requests/second; SEC's documented aggregate ceiling is 10 requests/second.
