# Research evaluation boundary

`phase-6-baseline.json` records ten unjudged real-corpus questions with dense, BM25, and hybrid top-five lists, immutable source/chunk hashes, generation identity, component scores/ranks, and single-run local timings. Regenerate it with `scripts/compare_retrieval.py` after starting PostgreSQL and Qdrant and setting `PYTHONPATH=backend`.

The queries are implementation probes, not human-verified relevance annotations. Do not calculate or claim Recall, Precision, MRR, nDCG, faithfulness, or quality improvement from these rank lists alone. Timings are individual warm-model samples, not a performance benchmark. Phase 17 will introduce the required human-reviewed, held-out evaluation set.

## Phase 7 recorded reranking

`phase-7-reranking.json` contains the same ten Phase 6 questions, complete bounded candidate passages and source hashes, pinned model manifest, original/resulting ranks, raw logits, and local CPU timings. Regenerate using `scripts/compare_reranking.py` with `PYTHONPATH=backend` after explicitly provisioning the reranker. Inputs are real stored filings; relevance is unlabelled. Ordering changes are not evidence of quality improvement. See `docs/phase-7-verification.md` for run conditions.
