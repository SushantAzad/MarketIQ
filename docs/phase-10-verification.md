# Phase 10 verification — 2026-09-30

Implemented LangGraph 1.2.12 orchestration with application-owned PostgreSQL checkpoints, conditional clarification/completion/failure paths, pinned structured cutoffs and accession choices, single-runner locking, bounded retries, and canonical citation validation. Applied migration `10bc42a3cfb1` to the existing local database. Added submit/run/show/purge-expired CLI commands and a seven-day configurable workflow retention window.

Validation results:

- Full regression suite: **292 passed**, **93.02% application coverage**, 106.02 seconds, including PostgreSQL and Qdrant integration tests and migration round-trip/schema verification.
- Final workflow-specific suite, including added CLI coverage: **19 passed**. This covers submit/show/run/purge, checkpoint resume, terminal replay, recoverable failure, three-attempt limit, sanitized exceptions, concurrent-runner rejection, expiration, version mismatch, clarification, insufficient evidence, and citation rejection.
- Ruff lint/format, strict mypy for **49 application modules**, locked dependency sync, and `git diff --check` passed.
- A simulated hard process exit after a calculation commit but before the workflow checkpoint resumed the execution node and retained one calculation record for identical inputs.
- Citation tests accept exact canonical claims and reject modified passage text, quote text, hashes, URLs, source IDs, generations, scopes, duplicate IDs, missing citation fields, and unsupported explanation text.

Real local-service verification used stored NVIDIA financial facts and the existing indexed 10-K accession `0001045810-26-000021`:

- Restart job `200c7949-8657-4541-aebf-64717fbb600a` checkpointed execution, then a **separate Python process** resumed validation/finalization. Plan and execute attempt counts remained one, and retrieved facts remained identical.
- Evidence job `da6746b8-370f-4a09-b1de-05c778cda130` returned the net-margin calculation and real filing passages, passed canonical citation validation, and round-tripped through PostgreSQL.
- No SEC refresh occurred. Generative inference remains disabled; evidence validation does not establish semantic relevance or a generated causal explanation.

Full local output is in ignored `data/workflow-verification.json`. Reproduce with `PYTHONPATH=backend` and `uv run --locked python scripts/verify_workflow.py`.

Recovery is at the four documented node boundaries, not exactly once per provider call. An interrupted execution stage may repeat research reads/model calls; completed checkpointed stages are not replayed. The retention command must be run or scheduled to physically delete expired workflow rows. Public job/SSE endpoints and deployment workers remain future work.

Next: Phase 11 cohort construction, target definition, leakage checks and risk-model evaluation gates. Frontend design remains Phase 14.
