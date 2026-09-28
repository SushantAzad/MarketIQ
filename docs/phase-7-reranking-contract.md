# Phase 7: bounded cross-encoder reranking

Research optionally retrieves 40 candidates in the selected retrieval mode, reranks at most 40, then applies the existing six-passage / 12,000-character whole-chunk evidence budget. These limits are starting settings, not measured optima; the character budget is not an LLM token budget. The standalone index search command still exposes the original retrieval ordering.

Model: `cross-encoder/ms-marco-MiniLM-L6-v2`, revision `fbf9045f293a58fa68636213c5e0cb8a2de5d45e`, Apache 2.0. Provisioning explicitly downloads safetensors and tokenizer/config files and records SHA-256 checksums, revision, license, and scalar output dimension. Runtime validates the manifest before loading locally with remote code disabled, CPU execution, and batch size eight. New installations default to disabled until explicitly provisioned and enabled.

The model ranks complete question/passage pairs. Any pair exceeding 512 tokenizer tokens fails the stage instead of silently truncating. Raw logits may be negative and are not confidence probabilities. Base cosine/BM25/RRF scores remain intact; rerank scores and both ranks are separate fields. Ties preserve retrieval order. Candidate scope, generation, canonical text, hashes, offsets, and citations are unchanged.

Research persists all candidate IDs, before/after ranks, logits, model key, inference timing and total timing including loading. Invalid scores, missing/corrupt files, oversized input, and inference errors produce a `failed` stage with original retrieval order; the answer selector is suppressed. Empty retrieval skips loading and records `no_candidates`. Disabled mode preserves the Phase 6 path. Exact quote verification is still distinct from relevance verification.

The recorded real-corpus comparison uses the ten Phase 6 queries and stores complete input passages, source hashes, generation, scores, ordering, and timing in `evaluation/phase-7-reranking.json`. These inputs have no human relevance labels. Changed ranking does not demonstrate improved relevance. Generative inference remains independently configured and disabled in this installation.

Primary references: [model card](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2/tree/fbf9045f293a58fa68636213c5e0cb8a2de5d45e), [CrossEncoder API](https://www.sbert.net/docs/package_reference/cross_encoder/model.html).
