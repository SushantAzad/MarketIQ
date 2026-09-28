# Phase 6: BM25 and reciprocal rank fusion

The retrieval API supports `dense`, `bm25`, and `hybrid`. The CLI and research default to hybrid. The Python `index.search` function retains its explicit dense default for existing callers; new callers should specify a mode. LLM configuration, exact extractive citation checks, and latest-discovered filing behavior from Phase 5 remain unchanged.

## Lexical index and score

PostgreSQL `lexical_generations` and `lexical_chunks` hold the tokenizer/scorer version, corpus manifest, complete-index checksum, per-chunk text hash, term frequencies, and token lengths. Composite foreign keys bind lexical documents to exact canonical generation membership.

Version `bm25-nfkc-v1-k1-1.2-b-0.75` specifies Unicode NFKC normalization, case folding, alphanumeric tokens with internal periods/hyphens, and a fixed short English stop list. Negation is retained. No stemming or synonyms are applied. Identifiers such as `H100`, `10-K`, and `1.25` remain tokens. Query terms are deduplicated.

For each unique query term, BM25 uses positive IDF `ln(1 + (N - df + 0.5)/(df + 0.5))`, term-frequency saturation `tf * (k1 + 1)`, and denominator `tf + k1 * (1 - b + b * length / average_length)`. Constants are `k1=1.2`, `b=0.75`. Statistics use **every canonical chunk within the permitted scope**, including empty token documents in N; an all-empty scope returns no results. Scores of zero are omitted. Ties sort by chunk UUID string.

The scorer uses the documented [positive BM25 IDF](https://lucene.apache.org/core/7_6_0/core/org/apache/lucene/search/similarities/BM25Similarity.html) and the standard length/term-frequency formula. It is a small tested implementation using the standard library, not a claim of bit-identical Lucene behavior.

## Scope and fusion

One repeatable-read SQL snapshot resolves the active generation and all matching canonical chunks by ticker alias, exact form, accession, heuristic section, and timezone-aware acceptance cutoff. There is no arbitrary corpus truncation. The permitted chunk IDs constrain the Qdrant query; the same IDs constrain lexical loading and statistics. Returned dense metadata must agree with that SQL snapshot. Both routes preserve exact text/hash checks.

Retrieve at most 40 candidates per route. Hybrid sums `1/(60 + rank)` for each route containing a chunk, using one-based ranks and one contribution per route. Missing-route contributions are zero. Ties sort by UUID. Version `rrf-v1-k60-window40` records these choices. See [the RRF definition](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion).

Results carry `score_kind`, `dense_score`, `bm25_score`, `dense_rank`, and `bm25_rank`. The primary score is cosine, BM25, or RRF according to mode. These numbers are not interchangeable or calibrated confidence values. Research filters dense scores by `RAG_MIN_SCORE` before fusion and admits positive BM25 candidates. It checks component eligibility rather than applying the cosine cutoff to RRF. Unthresholded CLI searches and the recorded baseline comparisons do not use the research cosine cutoff.

BM25 mode performs no query embedding and skips model loading in the CLI. Qdrant is still contacted to confirm alias consistency, so this is not an automatic Qdrant-outage fallback. Missing lexical metadata, wrong versions, corrupt terms/hashes/lengths, or missing members fail closed. The caller can explicitly choose dense mode when appropriate; hybrid never silently reports a partial route as a full hybrid result.

## Publication and recovery

The existing publisher lock serializes builds. Qdrant validation is followed by full lexical membership/content/checksum validation, before SQL `validated` state or alias activation. A lexical failure leaves the previous active generation unchanged. Model embeddings remain cached and reusable.

Old Phase 4/5 generations have no lexical record. They must be rebuilt into a new validated generation for hybrid retrieval; their dense retrieval and saved citations remain available. Ordinary unchanged builds validate both indexes and reuse the active generation. Explicit rebuild creates a fresh pair from journal or canonical SQL evidence. Lexical corruption is rejected rather than silently overwritten in an active generation.

Each retrieval pins one SQL generation. If a concurrent publication switches the alias before the dense call validates it, retrieval returns an activation error; it never fuses candidates from two generations. If publication occurs after that check, querying the retained concrete collection remains consistent with the pinned snapshot. BM25-only retrieval uses its pinned SQL snapshot. Saved research results include the retrieval mode, versions, candidate settings, component ranks/scores, and concrete generation.

## Current limits

Scoped term dictionaries are loaded and verified in-process for each query; BM25 scans the entire scoped corpus. This is adequate for the verified 1,211-chunk corpus, not a claim of large-corpus throughput. There is no posting-list server, persistent query cache, multi-process lexical cache, stemmer, cross-encoder, or measured optimal parameter tuning in this phase. The 40-candidate windows limit ranking candidates, not the corpus used to compute BM25 statistics.

Sections remain heuristic. A requested exact section with no matching canonical chunks returns no results; no hidden cross-section fallback is added. Acceptance cutoffs are not a historical knowledge guarantee. Model-assisted answer selection remains disabled until a model is configured. Human relevance labels and full quality evaluation remain Phase 17; reranking is Phase 7.
