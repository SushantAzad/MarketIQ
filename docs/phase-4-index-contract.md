# Phase 4: versioned filing index

PostgreSQL holds canonical document/chunk records, exact evidence text, embedding cache, generation membership, and frozen payloads. Qdrant holds named `text` vectors using cosine similarity. An index build consumes the current **parsed** SEC journal snapshot; it does not download or discover filings. Discovered-only filings are not counted as indexed.

## Embeddings and chunks

The initial provider is Sentence Transformers on CPU with `sentence-transformers/all-MiniLM-L6-v2`, revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. This model produces 384 dimensions. Configuration supports another compatible Sentence Transformer and a mandatory full commit SHA. Dimensions and input limits come from the loaded model; incompatible collections are rejected.

`provision` is the explicit model download command. Normal build/search loads local model files only, disallows remote code, and uses safetensors. The model key includes model name, pinned revision, dimension, normalization, and provider implementation version. Dependencies are locked in `uv.lock`.

Chunks stop at heuristic Item headings and use tokenizer offsets with at most 220 content tokens and 32-token overlap within a section. Inputs are re-tokenized and checked before encoding; silent model truncation is rejected. Evidence is an exact substring of normalized parsed text, with character start/end offsets. Whitespace-only gaps are excluded. Chunk versions include tokenizer/model identity and size/overlap settings. IDs include filing identity, parsed artifact hash, chunker version, and ordinal; this prevents cross-filing collisions and invalid reuse after reparsing.

HTML pages, fiscal years, and source anchors remain null when not established. Section labels are explicitly heuristic, including possible table-of-contents headings. Phase 2 flattened tables remain in the normalized text and can span chunks; this is not reliable row-aware table reconstruction. Numeric answers should use PostgreSQL financial facts, not arithmetic over these passages.

## Publication and recovery

1. Validate raw and parsed object hashes and their source/filing identity before importing canonical evidence.
2. Compute an ordered corpus manifest including source payloads and model identity. Create a generation in `building` state.
3. Cache embeddings by model key and chunk-text SHA-256. Reuse unchanged text, including across generations. Commit each new embedding batch for restart recovery.
4. Create a dedicated Qdrant collection and payload indexes; upsert deterministic chunk IDs with acknowledged writes.
5. Validate dimensions, cosine distance, exact count and membership, every payload, and every stored vector against the cache (normalized float32 tolerance).
6. Mark the generation `validated`, atomically switch the Qdrant alias, then transactionally retire the previous SQL generation and activate the new one.

A PostgreSQL session advisory lock serializes publishers across processes, including external writes. Builds never clear an active collection. Old collections remain available; automatic retention/deletion is deliberately not implemented.

Qdrant and SQL cannot share an atomic transaction. A failure between the alias switch and SQL activation causes searches to fail closed. Replaying the same build validates and reconciles that generation. A failure before publication leaves the prior active generation usable. Search resolves one concrete SQL generation and checks its alias/model; it does not follow a changing alias during the query.

`build --rebuild` creates a new collection from the current parsed journal corpus. `build --rebuild --from-canonical` reconstructs the active SQL corpus, validating its manifest, without raw files or the SEC journal. It requires the same model; cached embeddings are reused. This also recovers a deleted Qdrant collection. Retained generations are not silently pruned.

## Retrieval boundaries

The CLI returns evidence, not generated answers. Filters cover ticker aliases, exact filing form, accession internally, section, and acceptance cutoff. Payload indexes also cover company, filing, and nullable fiscal year. Each result is rejoined to the generation's canonical SQL chunk; text hash and full payload must match before it is returned.

`--latest` requires `--ticker` and `--form`. It selects the latest **discovered** accession from the local journal and restricts retrieval to that accession. An unindexed newer filing yields unavailable, never an older substitute. It does not claim a fresh SEC check: run SEC ingestion and database synchronization first when a new upstream check is needed. Amendments are separate exact forms (for example `10-K/A`). Acceptance-time filtering alone is not a historical knowledge/as-of guarantee.

Dense retrieval relevance has not been benchmarked. No BM25, reranker, generated-answer accuracy, API, or dashboard integration is claimed in this phase. Those follow the agreed phase sequence.

## References

- [Qdrant 1.19.1 official release](https://github.com/qdrant/qdrant/releases/tag/v1.19.1): Windows runtime used for verification.
- [Qdrant Python client](https://github.com/qdrant/qdrant-client): collection, filter, and alias operations.
- [Sentence Transformer model API](https://sbert.net/docs/package_reference/sentence_transformer/model.html): pinned revision and local-only model loading.
- [Pinned MiniLM model](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/tree/1110a243fdf4706b3f48f1d95db1a4f5529b4d41): initial embedding artifacts.
