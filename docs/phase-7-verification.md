# Phase 7 verification — 2026-09-28

Implemented and provisioned the pinned MiniLM cross-encoder on CPU. The registry confirmed revision `fbf9045f293a58fa68636213c5e0cb8a2de5d45e` and Apache 2.0 licensing. The local manifest and recorded evaluation include all downloaded file SHA-256 digests. Requests require local files and never download weights.

Validation:

- Full PostgreSQL/Qdrant suite: **201 passed**, **95.14% coverage**, 94.52 seconds.
- After adding complete candidate-rank tracing and total load timing, reran all eight reranker unit/integration tests: **8 passed**.
- Ruff checks and formatting passed; strict mypy passed for 38 application modules.
- Verified stable ties, 40-candidate cap, unchanged base scores, finite scalar outputs, offline flags, manifest integrity, refusal to truncate overlength pairs, empty input, and persisted failure fallback with answer selection suppressed.

Real-corpus verification used active generation `690b5711-4c29-49de-b135-3bb1a7f8ebe3`, containing 1,211 chunks from nine SEC documents. The ten recorded Phase 6 questions all produced changed top-five orderings. The final saved single-sample inference times ranged **141.197–1,051.217 ms**, excluding model load/checksumming (**7,985.312 ms**). Other local validation processes were active during this run; these are observations, not an isolated latency benchmark or an SLA. There are no human relevance labels and no measured quality improvement claim.

`evaluation/phase-7-reranking.json` records complete input passages, canonical hashes, source URLs, generation, retrieval and reranker scores, and original/resulting ranks. Reproduce with `PYTHONPATH=backend` and `uv run --locked python scripts/compare_reranking.py`.

With `RERANKER_ENABLED=true`, the real NVIDIA 10-K research check returned six evidence passages, recorded reranking of 40 candidates, and round-tripped the complete response through PostgreSQL. Run: `b51e7a25-69b7-45c5-b7d3-5d4ad773f876`; accession: `0001045810-26-000021`. Unknown ticker evidence was refused. Full local verification output is in ignored `data/research-verification.json`.

Reranking remains explicit opt-in via `RERANKER_ENABLED=true`; downloaded weights are ready locally. Generative selection remains disabled, so the verified response is `EVIDENCE_ONLY`. No new SEC refresh, financial calculations, frontend dashboard, or free-form synthesis was introduced. Phase 8 is the deterministic calculation engine.
