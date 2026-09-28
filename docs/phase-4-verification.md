# Phase 4 verification — 2026-09-28

Implemented and verified filing ingestion into an actual Qdrant server, with canonical PostgreSQL evidence, pinned Sentence Transformer embeddings, payload filters, and recoverable generation activation. This phase does not implement generated research answers or the frontend dashboard.

## Environment and source data

- Windows, Python 3.13.9, locked `qdrant-client` 1.19.1 and `sentence-transformers` 5.7.0.
- Official Qdrant 1.19.1 Windows executable, verified against the release asset's published SHA-256; metadata retained in ignored `.cache/qdrant/release.json`.
- PostgreSQL 17.11, using the existing local financial database. Alembic revision `9fc509aa38a2` adds five tables and preserves Phase 3 data.
- MiniLM model `sentence-transformers/all-MiniLM-L6-v2`, pinned revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, CPU, 384 dimensions, cosine vectors.
- Nine previously downloaded real primary SEC documents across NVDA, MSFT, AAPL, AMZN, GOOGL, META, and TSLA. Forms include 10-K, 10-Q, 8-K, and 8-K/A.
- **1,211 canonical chunks and vectors.** No invented production filings, values, or embeddings. This run did not refresh upstream SEC data.

## Real-data verification

`scripts/verify_filing_index.py` completed against the real server and model. The machine-readable report is ignored `data/index-verification.json`, recorded at `2026-09-28T11:33:18.805395+00:00`.

- Initial build created 1,211 embeddings. Subsequent unchanged replay created **zero** embeddings and retained generation identity.
- A fresh generation rebuilt from canonical PostgreSQL evidence reused all 1,211 cached embeddings.
- All collection point IDs, counts, complete payloads, vector dimensions, cosine configuration, and float32 vectors were checked before activation.
- Eight ticker/form queries across seven issuers returned 16 passages with verified canonical text/hash/payload and SEC source links. NVDA was checked separately for 10-K and 10-Q.
- Nonexistent-ticker and pre-2000 acceptance filters correctly returned no results.
- Active generation: `eda4080d-8a91-4bb2-b1f5-aa4c89c89897`. SQL and Qdrant alias agree. Previous real-data generations remain retained.
- Qdrant readiness endpoint returned HTTP 200. The start helper recognizes an existing server and avoids a duplicate launch.

This verifies evidence integrity and filtering, not a benchmark of semantic relevance or answer accuracy.

## Automated checks

Final command:

```powershell
$env:MARKETIQ_TEST_QDRANT_URL = "http://127.0.0.1:6333"
uv run --locked --cache-dir .cache/uv --env-file .env.postgres-local pytest --cov=app --cov-fail-under=85
```

Result: **132 passed in 53.28 seconds; 94.51% application coverage**, with no skips or cleanup errors. This includes 10 real Qdrant/PostgreSQL index integration tests and the 13 pre-existing PostgreSQL integration tests. Synthetic test embeddings/text are confined to automated fixtures, isolated SQL schemas, and temporary vector collections.

Coverage includes exact chunk offsets and token limits, deterministic replay, source corruption rejection, failed upserts preserving the active index, interruption after alias publication, reconciliation, payload/vector/count corruption detection, canonical rebuild without raw/journal access, deleted-collection recovery, latest-discovered-but-unindexed behavior, filters, CLI flows, and offline/pinned model loading.

Additional checks passed:

- Ruff lint and format check: all Python files passed.
- Strict mypy: 30 application source files.
- `uv sync --locked`: dependency lock is consistent.
- Migration/schema parity and upgrade/downgrade/re-upgrade remain covered by PostgreSQL tests.

CI now supplies PostgreSQL 17.11 and Qdrant 1.19.1 services, waits for Qdrant readiness, and runs the full suite. Remote CI execution has not been observed. Frontend code was not changed in this phase.

## Windows observations and limitations

Docker Desktop remains unavailable; verification used the official standalone Windows Qdrant server, not an in-memory replacement. No Docker reset or configuration change was performed.

`localhost` caused approximately two seconds of connection delay on this host versus approximately 22 ms for `127.0.0.1`. The local `.env` Qdrant URL and defaults now use the explicit IPv4 loopback address.

One earlier run passed all 131 test assertions but encountered a Qdrant Windows file-lock/rename error while deleting a temporary test collection. Qdrant had unregistered it but left its directory behind. After confirming the collection was no longer registered, that exact test directory was moved to ignored `.cache/qdrant-test-orphans/` to prevent reload on restart; no files were deleted. The subsequent expanded 132-test run cleaned up successfully. This transient Windows server cleanup behavior is not claimed to be fixed upstream. Application builds retain old generations and do not perform collection deletion.

The Windows release does not bundle browser dashboard assets, and its filesystem-type check reports unsupported on this platform. API operations, persistence, retrieval, and rebuilding were exercised successfully. This remains a local development runtime, not a production readiness certification.

See [the index contract](phase-4-index-contract.md) for heuristic sections, flattened-table limitations, exact-form latest filtering, source freshness, and cross-store activation semantics. Next is Phase 5: basic RAG with grounded answers and citations.
