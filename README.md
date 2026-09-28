# MarketIQ

Real Time Financial Intelligence Platform — an incremental, evidence-backed financial research application.

**Current scope: Phase 8 deterministic financial calculations.** SEC ingestion, PostgreSQL financial facts, hybrid retrieval, bounded offline reranking, cited research traces, and versioned Decimal calculations with persisted operand lineage are implemented. Model-assisted passage selection requires a configured inference endpoint; this installation currently returns cited evidence only. Query routing, free-form synthesis, public API endpoints, and ML remain later work. The frontend is still a development entry point; use the CLI for financial data and research.

Calculate from explicitly dated stored facts:

```powershell
uv run --locked --directory backend python -m app.database migrate
uv run --locked --directory backend python -m app.database calculate NVDA net_margin --basis annual --period-end 2026-01-25
```

Supported formulas include growth, YoY, CAGR, margins, FCF and FCF growth, current ratio, component-based debt/equity, ROE/ROA, a narrowly defined EBITDA, and discrete quarters from compatible YTD facts. Missing components or incompatible periods return explicit unavailable reasons. See [the calculation contract and CLI examples](docs/phase-8-calculation-contract.md).

Enable reranking after downloading the pinned model once:

```powershell
uv run --locked --directory backend python -m app.research provision-reranker
$env:RERANKER_ENABLED="true"
uv run --locked --directory backend python -m app.research ask "What export restrictions are disclosed?" --ticker NVDA --form 10-K
```

Set `RERANKER_ENABLED=true` in `.env` to persist the setting. Requests use local files only. Reranking failures retain explicitly labelled original-order evidence and suppress answer generation. See [the reranking contract](docs/phase-7-reranking-contract.md).

See [the approved architecture](docs/architecture-proposal.md) for the design and phase gates.

## Local setup

Prerequisites: Python 3.13, uv 0.11.21, Node.js 24, and npm. Run commands at the repository root unless indicated.

```powershell
Copy-Item .env.example .env
uv sync --locked
uv run --locked --directory backend python -m app.core.check_config
npm --prefix frontend ci
npm --prefix frontend run dev
```

On macOS/Linux use `cp .env.example .env`. The Python configuration resolves the root `.env` independently of the working directory. Environment variables override that file. No external service is needed for Phase 1. Do not overwrite an existing `.env` during repeated setup.

The frontend binds to loopback; open the address printed by Vite. Configuration validation checks syntax and policy only, not provider connectivity. Keep market access disabled. Copy `frontend/.env.example` to `frontend/.env` when frontend API configuration is introduced; it must contain public values only.

## SEC ingestion

Set `SEC_USER_AGENT="MarketIQ your-real-contact-email"` in the root `.env`. Include an application name and a real email. No SEC API key is required. Use a one-command opt-in for the first run:

```powershell
uv sync --locked
uv run --locked --directory backend python -m app.ingestion run --enable-sec --tickers NVDA --limit 1
uv run --locked --directory backend python -m app.ingestion status
```

Omit `--tickers` to process all seven initial companies. `--limit` is the maximum number of pending documents per issuer per cycle, newest first. Repeated runs continue the backlog; already parsed documents are checksum-verified and skipped. `--forms 10-K` restricts processing to annual filings. `--backfill` also discovers older submissions pages and may make many requests; the document-processing limit does not limit discovery-page requests.

To poll every `SEC_POLL_SECONDS` (default 300), run in a separate terminal:

```powershell
uv run --locked --directory backend python -m app.ingestion watch --enable-sec --limit 3
```

Stop with Ctrl+C. Only one local writer may run for the configured state directory. Do not run different state directories or multiple hosts against SEC concurrently: the distributed Redis limiter is a later phase. SEC requests are limited to the configured rate (default 5/s, capped at 10/s), with exponential retry/backoff and persisted Retry-After cooldowns. No poller is installed or running automatically.

The ignored `data/sec/journal.sqlite3` is a local **ingestion journal**, separate from the PostgreSQL financial database. Source JSON/HTML and parsed JSON are saved by SHA-256 under `data/raw/<first-two-hash-characters>/<hash>`. The journal records source URLs, timestamps, versions, attempts, errors, and filing stage. Database commands normalize companyfacts into PostgreSQL; index commands publish parsed filing evidence to Qdrant.

`status` distinguishes RECENT, STALE, and UNAVAILABLE. RECENT means recently fetched, not a current financial reporting period. Source timestamp is the HTTP Last-Modified value when provided; filing acceptance and reporting dates remain in filing metadata. No SEC data is labeled LIVE. Missing timestamps stay null. A discovered filing is not a processed filing: `parsed` only confirms text extraction, not embeddings or full ingestion completion.

Parser headings are heuristic and offsets refer to normalized text. Tables are retained as text; numeric normalization, exhibits, reliable section mapping, and source anchors are later work. Corrupt stored artifacts are reported as failures rather than silently trusted; preserve and investigate the affected files before repair.

## Verification

PostgreSQL integration tests create and remove only randomly named `miq_test_*` schemas. Set `MARKETIQ_TEST_DATABASE_URL` to an accessible PostgreSQL database whose role may create schemas. Tests never drop application/public tables. Without that variable the PostgreSQL tests are explicitly skipped; a unit-only run does not verify Phase 3. CI provides a PostgreSQL 17.11 service and runs the complete suite.

For the portable database prepared on this machine, use `uv run --locked --env-file .env.postgres-local pytest --cov=app --cov-fail-under=85`.

```text
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest --cov=app --cov-fail-under=85
npm --prefix frontend run lint
npm --prefix frontend run build
```

CI runs these checks on Linux. Python dependencies are pinned by `uv.lock`; frontend dependencies by `frontend/package-lock.json`. Do not hand-edit locks. Dependency additions are introduced with their implementation phase; heavy ML packages are intentionally not installed for the foundation.

## Repository boundaries

- `backend/app`: configuration, SEC ingestion, PostgreSQL financial data, versioned filing index, and sourced queries; research/API modules come later.
- `backend/tests`: unit tests and integration tests that require PostgreSQL and Qdrant servers.
- `frontend/src`: React/TypeScript entry point; dashboard planned for Phase 14.
- `scripts`, `ml`, `evaluation`, `docker`: documented boundaries for later phases, not completed components.
- `docs`: architecture and phase verification records.

Never commit `.env`, credentials, raw filings, model weights, or generated data. Test-only credentials in automated tests are inert strings. Runtime secrets use redacted configuration types; do not log raw environment variables or validation inputs.

## PostgreSQL financial data

Configure `DATABASE_URL` in root `.env`. For Docker, also set `POSTGRES_PASSWORD` to the same password and optionally `POSTGRES_PORT` (default 5433):

```powershell
docker compose --env-file .env -f docker/postgres.compose.yml up -d --wait
uv run --locked --directory backend python -m app.database migrate
uv run --locked --directory backend python -m app.database sync --enable-sec
uv run --locked --directory backend python -m app.database query NVDA revenue --basis annual
```

The Compose service binds PostgreSQL to loopback and retains data in a named volume. It is only the database service, not the final full-stack Compose deployment. The image configuration is provided but could not be exercised locally because Docker Desktop failed at startup.

**This workspace's verification environment:** Docker Desktop failed with a `dockerInference` socket error, so an official portable PostgreSQL 17.11 runtime was provisioned under ignored `.cache/postgres/`. Its database is `data/postgres/`, bound to `127.0.0.1:55432`, with generated credentials in ignored `.env` and `.env.postgres-local`. No Windows service was installed. Control that existing cluster with:

```powershell
uv run --locked python scripts/database/local_postgres.py status
uv run --locked python scripts/database/local_postgres.py stop
uv run --locked python scripts/database/local_postgres.py start
```

Do not start both setups on the same port or assume they share data. Choose one `DATABASE_URL`; the portable cluster and Docker volume are independent. The helper script controls only the already initialized portable cluster and does not download or initialize one on a fresh checkout.

`sync --enable-sec` refreshes submissions and companyfacts before import. `import-journal` imports saved snapshots offline and says `upstream_refreshed: false`. Query commands read stored data and include its freshness; use sync first for a new upstream check. To restrict refresh scope, add `--tickers NVDA MSFT`.

```powershell
uv run --locked --directory backend python -m app.database status
uv run --locked --directory backend python -m app.database query MSFT assets --basis instant
uv run --locked --directory backend python -m app.database query AAPL operating_cash_flow --basis ytd_9m
```

Optional query flags: `--period-end YYYY-MM-DD`, `--unit USD`, and `--as-of 2026-09-28T12:00:00Z`. Results preserve original dates and decimal values. An as-of query requires a source snapshot actually observed by that time; it does not invent historical knowledge. See [the financial data contract](docs/phase-3-data-contract.md) for restatements, period classification, rejected observations, and limitations.

## Filing evidence index (Phase 4)

The index stores canonical filing passages in PostgreSQL and dense vectors in Qdrant. The frontend remains the development placeholder; evidence retrieval is currently a CLI operation.

On this Windows machine, the official Qdrant 1.19.1 executable was provisioned under ignored `.cache/qdrant/` with its published SHA-256 verified. It stores data under `data/qdrant/`, binds to loopback port 6333, and does not require Docker. Its log is `.cache/qdrant/server.log`. A fresh Windows checkout can use `uv run --locked python scripts/database/local_qdrant.py provision` to download that same release. Start/status commands are:

```powershell
uv run --locked python scripts/database/local_qdrant.py start
uv run --locked python scripts/database/local_qdrant.py status
```

Keep one Qdrant instance per storage directory. This is a local development runtime, not a Windows service or production deployment. The executable archive does not bundle the browser dashboard assets. On other platforms, supply a Qdrant 1.19.1 server and configure `QDRANT_URL` (and `QDRANT_API_KEY` when applicable).

The pinned model is already provisioned here. On a fresh installation, run these steps after the earlier SEC ingestion/database setup:

```powershell
uv run --locked --directory backend python -m app.database migrate
uv run --locked --directory backend python -m app.indexing provision
uv run --locked --directory backend python -m app.indexing build
uv run --locked --directory backend python -m app.indexing status
uv run --locked --directory backend python -m app.indexing search "business risks" --ticker NVDA --form 10-K --latest --limit 3
```

`provision` downloads model artifacts; build/search otherwise load them offline. Build indexes all currently parsed filings, reusing unchanged embeddings. It does not fetch newer SEC data. Results carry source URLs, acceptance times, hashes, section labels, and exact text offsets. `--latest` returns unavailable if the latest locally discovered filing is not indexed. Optional filters include `--section` and `--accepted-before 2026-09-01T00:00:00Z`; the latter filters acceptance time, not historical knowledge.

For a new validated collection, use `build --rebuild`. To recover the active corpus entirely from PostgreSQL without the journal/raw files, use `build --rebuild --from-canonical`. Both retain old generations and reuse cached embeddings. See [the index contract](docs/phase-4-index-contract.md) for activation recovery and limitations.

Run PostgreSQL **and real Qdrant server** tests locally:

```powershell
$env:MARKETIQ_TEST_QDRANT_URL = "http://127.0.0.1:6333"
uv run --locked --env-file .env.postgres-local pytest --cov=app --cov-fail-under=85
```

Without the two test service environment variables, the corresponding integration tests are explicitly skipped. Tests create isolated SQL schemas and Qdrant collections/aliases and remove only those test resources. CI supplies both services. Tests use synthetic fixture text/embeddings only; production ingestion always uses source filings and real model embeddings.

## Basic research (Phase 5)

Apply migrations, then ask a question scoped to one issuer and filing form:

```powershell
uv run --locked --directory backend python -m app.database migrate
uv run --locked --directory backend python -m app.research status
uv run --locked --directory backend python -m app.research ask "What risks does NVIDIA disclose about export restrictions?" --ticker NVDA --form 10-K
```

The default is the latest **locally discovered** filing of that exact form. If it is not indexed, research returns insufficient evidence instead of substituting an older filing. For a historical filing, provide `--accession 0001045810-26-000021`. These commands do not refresh SEC data.

The response includes a `run_id`. Inspect it with `python -m app.research show <run-id>` using the same `uv run --locked --directory backend` prefix. PostgreSQL retains the result, model/prompt metadata, timings, and citation links. It stores a hash of the question rather than the original question text. Endpoint credentials, raw model responses, and exception bodies are not saved.

This is an explicitly temporary **extractive** baseline, now using hybrid retrieval by default. A configured model selects at most three supporting passages. The application quotes the complete selected chunks and attaches canonical URLs, hashes, generation/chunk IDs, filing dates, and normalized-text offsets. It does not display model-written paraphrases, invented links, calculations, or predictions. Exact source matching does not certify semantic relevance; `relevance_verified` remains false pending evaluation.

Statuses:

- `ANSWERED`: a model selected valid sources; the answer consists of exact filing quotations.
- `EVIDENCE_ONLY`: source passages are available, but the model is disabled or failed. `answer` stays null.
- `INSUFFICIENT_EVIDENCE`: no matching passages, an unindexed latest filing, model abstention, or invalid citation selection. `answer` stays null.
- `UNAVAILABLE`: a configuration, infrastructure, or integrity error prevented the request.

To enable an endpoint **you have chosen**, edit only your local `.env`:

```dotenv
LLM_PROVIDER=compatible
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL=your-installed-model
# LLM_API_KEY=your-key-if-required
```

That URL is an example for an existing local compatible server; this project does not install or download a generative model automatically. Hosted endpoints require HTTPS and may incur provider charges. The adapter posts to `<LLM_BASE_URL>/chat/completions`, requests JSON-object output, and expects standard `choices/message/content` responses with `finish_reason=stop`. Endpoint support must be verified with the selected model. Status reports configuration only, not successful connectivity. Secrets belong in `.env`, not Git or chat.

`RAG_MIN_SCORE=0.3` is an uncalibrated cosine cutoff for dense candidates, not a confidence probability or a cutoff for BM25/RRF. Context is bounded by characters without truncating individual passages; the selected endpoint must support the supplied context. See [the Phase 5 contract](docs/phase-5-rag-contract.md) and [verification record](docs/phase-5-verification.md) for the original baseline.

## Hybrid retrieval (Phase 6)

Search combines up to 40 dense and 40 BM25 candidates using reciprocal rank fusion (constant 60). Both retrievers use one SQL-resolved company/form/accession/section/date scope and one generation. Every generation must have validated dense vectors and lexical records before activation.

The current local corpus has already been upgraded. On an existing Phase 5 installation, run:

```powershell
uv run --locked --directory backend python -m app.database migrate
uv run --locked --directory backend python -m app.indexing build --rebuild --from-canonical
```

This rebuilds from PostgreSQL and reuses embeddings. Old dense-only generations remain available for explicit dense searches; hybrid/BM25 require the lexical generation. Subsequent ordinary `build` commands publish both indexes.

```powershell
uv run --locked --directory backend python -m app.indexing search "export restrictions" --ticker NVDA --form 10-K --latest --mode hybrid
uv run --locked --directory backend python -m app.indexing search CUDA --ticker NVDA --form 10-K --latest --mode bm25
uv run --locked --directory backend python -m app.indexing search CUDA --ticker NVDA --form 10-K --latest --mode dense
```

`--mode` defaults to `hybrid` in the CLI. BM25 mode skips loading the embedding model but still checks Qdrant alias consistency. Research defaults to `RAG_RETRIEVAL_MODE=hybrid`; set it to `dense` or `bm25` in your local environment to compare. Existing LLM settings are unchanged.

Results identify `score_kind` (`cosine`, `bm25`, or `rrf`) and retain separate dense/BM25 scores and ranks. A missing component score means that retriever did not contribute the chunk within its candidate window. Research applies its cosine cutoff before fusion and allows positive BM25 candidates; it never treats a small RRF score as low cosine similarity.

Recorded comparisons for ten queries across seven issuers are in [the Phase 6 baseline JSON](evaluation/phase-6-baseline.json). They are unjudged rank comparisons, not evidence of improved answer accuracy. Regenerate with `$env:PYTHONPATH="backend"` followed by `uv run --locked python scripts/compare_retrieval.py`. See [the hybrid contract](docs/phase-6-hybrid-contract.md) for scoring, versioning, and scale limits, and [the verification record](docs/phase-6-verification.md) for measured checks.
