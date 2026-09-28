# Phase 5 verification — 2026-09-28

Implemented the temporary dense-only extractive RAG baseline: scoped source retrieval, a configurable model adapter, exact passage citations, abstention, and persistent research traces. **Live model inference is not verified or enabled:** no model endpoint has been selected. The installed system returns cited evidence with a null answer rather than inventing a generated response.

## Implemented behavior

- `app.research ask`, `show`, and configuration-only `status` commands.
- Latest locally discovered filing by default, with explicit historical accession support and no older-filing substitution.
- Whole-passage context budget, score threshold, deduplication, and issuer/form/accession/generation/hash checks.
- Optional compatible chat-completions selector with strict JSON output, no tools, no redirects, bounded response size/deadline, and safe failure codes.
- Model output limited to source IDs; quoted answer text and all citations come from canonical data.
- PostgreSQL research runs and citation ledger, with exact quotes linked to canonical generation/chunk membership. Questions are hashed, and secrets/raw model responses are excluded.
- `ANSWERED`, `EVIDENCE_ONLY`, and `INSUFFICIENT_EVIDENCE` business outcomes; infrastructure/integrity errors return `UNAVAILABLE` through the CLI.

Migration `1486d73c77a0` was applied to the local PostgreSQL database. No dependencies were added and no frontend changes were made.

## Automated checks

Final full command:

```powershell
$env:MARKETIQ_TEST_QDRANT_URL = "http://127.0.0.1:6333"
uv run --locked --cache-dir .cache/uv --env-file .env.postgres-local pytest --cov=app --cov-fail-under=85
```

Result: **173 passed in 62.84 seconds, 95.14% application coverage**, no skipped tests. The suite includes 27 PostgreSQL and/or actual Qdrant server integration tests; four are new research integration tests. Model responses in automated tests are explicitly mocked fixtures, not a live inference test.

Tests cover exact quotation preservation (including negation and numbers), complete citation membership, source-scope/hash integrity, context limits, duplicate/unknown IDs, model abstention, absent evidence bypassing inference, provider-disabled behavior, malformed nested JSON, extra fields, truncated completions, tool responses, oversized bodies, redirects, HTTP/transport failures, response deadlines, safe errors, trace persistence, CLI inspection, and latest-discovered-but-unindexed filings. Migration round trips and schema parity continue to pass.

Ruff lint/format and strict mypy passed (35 application source files). Locked dependency sync and configuration validation passed. CI will run the expanded suite with the existing PostgreSQL/Qdrant services; remote CI execution has not been observed.

## Real-data check

`scripts/verify_research.py` completed using the real local PostgreSQL 17.11 database, Qdrant 1.19.1 server, pinned MiniLM embeddings, and existing SEC filings. Report: ignored `data/research-verification.json`.

- Question: “What risks does NVIDIA disclose about export restrictions?”
- Scope: NVDA 10-K, accession `0001045810-26-000021`.
- Active generation: `eda4080d-8a91-4bb2-b1f5-aa4c89c89897`.
- Six actual filing passages returned with canonical text/hash checks and SEC URLs.
- Status: `EVIDENCE_ONLY`; reason: model not configured. No generated answer was stored.
- Saved run `f5cf6901-dfe7-4d5d-8b82-8d521355d7ff` read back exactly from PostgreSQL.
- An unknown issuer query returned insufficient evidence and no answer.
- Recorded at `2026-09-28T11:48:18.280910+00:00`; no SEC refresh was performed.

## Remaining verification and limits

The user must choose a local or hosted model endpoint and model name before live inference can be exercised. Credentials should be entered in the local `.env` file. No generative model was downloaded and no hosted inference service was contacted. The same verification script supports a configured endpoint and then requires a model-selected, canonically cited answer to pass.

This baseline verifies quotation integrity, not semantic entailment, completeness, model quality, or calibrated confidence. It does not provide free-form financial synthesis, calculations, BM25, reranking, or the final dashboard. The default score cutoff is uncalibrated, and chunks can include fragments at their boundaries. See [the RAG contract](phase-5-rag-contract.md) for the precise behavior and limitations.
