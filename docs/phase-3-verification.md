# Phase 3 — PostgreSQL financial persistence and normalization

Implemented and verified on 2026-09-28. This report supersedes the earlier phase reports for financial persistence only; the frontend, public API, RAG, calculations, and ML remain future phases.

## Delivered

- PostgreSQL 17 financial schema with 14 tables, exact NUMERIC amounts, relational keys, period constraints, lookup indexes, and a frozen Alembic migration.
- Transactional, replay-safe normalization of SEC companyfacts, with raw-object checksums and JSON Pointer lineage for every accepted fact.
- Filing metadata provenance from saved submissions where available; older filings without discovered primary metadata retain the companyfacts accession and filing-index source.
- Explicit annual/quarter/YTD/instant periods, filing fiscal labels separate from fact dates, conservative availability timestamps, preserved amendments/restatements, and conflict detection.
- Snapshot-observation history and point-in-time query restrictions, including tests for values removed from a later snapshot and versions that reappear.
- PostgreSQL copies of ingestion fetch history and freshness state; source files remain immutable local objects.
- `database migrate`, `status`, `sync`, `import-journal`, and `query` commands. Sync performs a real upstream refresh; import-journal is explicitly offline. No generated financial numbers or production mock responses were introduced.
- A database-only Docker Compose configuration and PostgreSQL service in CI. Full-stack Compose remains a later phase.

## Measured local validation

- **109 tests passed**, including **13 real PostgreSQL integration tests**; none skipped in the full verification run.
- **94.18% application statement coverage**.
- Strict mypy, Ruff lint/format, and locked dependency synchronization passed.
- Alembic upgrade → downgrade → upgrade passed in an isolated test schema. Reflected PostgreSQL schema matched SQLAlchemy metadata.
- Tests used generated `miq_test_*` schemas and removed only those schemas after execution. Application tables and user data were not dropped.
- A fresh SEC metadata/companyfacts sync succeeded for all seven initial companies.
- Live import persisted **177,791 financial fact rows**, **467 filing records**, and **17,048 statement/context groups**, across seven issuers. Statement/context groups are not counts of complete published reports.
- **148 observations were rejected** and retained with source pointers; affected normalization runs are marked partial. These are not silently filled, rounded, or discarded from the raw sources.
- **21 queries** (annual revenue, instant assets, and annual operating cash flow for each initial company) matched the exact values and accession numbers at their original SEC JSON pointers.
- Repeating the import reported all seven snapshots unchanged. No duplicate financial facts were introduced.

## Database runtime and lifecycle

Docker Desktop could not initialize: its startup log reported a `dockerInference` socket/file-access failure. No factory reset, settings edit, or existing container deletion was attempted.

Verification therefore used the official portable PostgreSQL 17.11 Windows binaries downloaded from the EDB distribution linked by the PostgreSQL project. The runtime is under ignored `.cache/postgres/`, the cluster under `data/postgres/`, and the server binds only to `127.0.0.1:55432`. Generated credentials are in ignored local environment files. No Windows system service was installed.

The lifecycle helper at `scripts/database/local_postgres.py` controls this existing cluster. A clean restart was verified. Its log is outside the data directory to avoid a Windows file-sharing conflict during recovery. The database is left available for local queries; stop it with the documented helper when no longer needed.

Docker Compose execution and remote GitHub Actions execution remain **unverified**. The database engine, migration, imports, and queries were tested against real PostgreSQL, not SQLite or mocked SQL.

## Reproduce

```powershell
uv sync --locked
uv run --locked python scripts/database/local_postgres.py status
uv run --locked --directory backend python -m app.database status
uv run --locked --env-file .env.postgres-local pytest --cov=app --cov-fail-under=85
uv run --locked --directory backend python -m app.database sync --enable-sec
uv run --locked --directory backend python -m app.database query NVDA revenue --basis annual
```

On a fresh checkout, provision PostgreSQL using the README's Compose instructions or your own instance and configure `DATABASE_URL` plus `MARKETIQ_TEST_DATABASE_URL`. The portable runtime and local credentials are intentionally not committed.

## Remaining boundaries

Read [the data contract](phase-3-data-contract.md) before treating results as comparable financial periods. Ratios, quarter subtraction, TTM calculations, custom taxonomy mappings, and historical securities master data are not implemented. Current as-of queries use actually observed snapshots; they do not establish a retrospectively complete historical training dataset. The existing SEC poller does not automatically run PostgreSQL sync. The next planned phase is Qdrant ingestion.
