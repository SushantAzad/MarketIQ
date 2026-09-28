# Phase 5: temporary extractive RAG baseline

This phase adds model-assisted source selection, citations, abstention, and persistent passage/claim tracing to the Phase 4 dense index. It intentionally returns exact quoted passages instead of unverified model-written financial explanations. BM25 and reranking remain Phases 6 and 7; deterministic calculations remain Phase 8.

## Retrieval and answers

Requests require a question, ticker, and exact SEC form. An accession may be supplied; otherwise the latest locally discovered filing is selected. Missing/unindexed filings do not fall back to older filings. Retrieval preserves Phase 4's canonical text/payload integrity checks and concrete generation identity. It does not make a fresh SEC request.

The baseline retrieves up to six chunks, rejects nonfinite/below-threshold scores, checks issuer/form/accession/generation/hash consistency, deduplicates chunk IDs, and keeps whole passages within a 12,000-character context budget. The default 0.3 cosine cutoff is configurable and uncalibrated. It cannot prove answerability or relevance.

A selector receives the question and labeled filing passages as untrusted data. Its only permitted output is a strict JSON object with a boolean `answerable` and at most three distinct supplied `source_ids`. Abstention requires an empty source list. Extra fields, arbitrary prose, invalid JSON, unknown source IDs, duplicate IDs, and inconsistent states are rejected. Any unknown ID rejects the entire answer rather than leaking partially accepted claims.

The application constructs claims from the complete canonical text of each selected chunk. The model cannot author claim text, amounts, citation links, or offsets. Within-chunk qualifications and negations are retained verbatim. A claim is explicitly a **filing quote**, not an independently verified assertion. Chunks may begin/end mid-sentence and adjacent qualifications may be outside the chunk; readers can inspect the original filing. Quotation integrity is verified, while semantic relevance and answer completeness remain unverified (`relevance_verified: false`). The model is prompted to abstain on unsupported questions, missing entities/periods, calculations, current prices, and forecasts. No prompt can guarantee semantic correctness; model selection must be evaluated before broader synthesis is enabled.

`generated_explanation` remains null. Calculated values, market data, and predictive model outputs remain empty. The baseline does not pretend to implement deterministic financial reasoning merely by quoting numbers from filings.

## Provider behavior

Default `LLM_PROVIDER=disabled` makes no inference request. With evidence, the result is `EVIDENCE_ONLY`; without evidence it is `INSUFFICIENT_EVIDENCE`. Structured financial queries from Phase 3 remain independently available.

The compatible adapter uses the existing httpx dependency, a user-configured base URL/model, optional redacted API key, and a bounded non-streaming chat-completions request. It requests JSON-object output, disables redirects, never exposes tools, rejects truncated/tool/refusal responses, limits decoded response data to 64 KiB, and checks a response deadline as well as transport timeouts. There is no automatic model download, external endpoint selection, or retry that could multiply billed requests. Remote endpoints require HTTPS; plain HTTP is restricted to loopback hosts.

Provider failure returns a safe error code with the retrieved evidence and a null answer. Bodies, raw exceptions, and credentials are not returned or persisted. A slow provider can still occupy the CLI until its transport timeout; background research jobs remain later work.

This transport contract follows the chat-completions subset described in [Ollama's compatibility documentation](https://github.com/ollama/ollama/blob/main/docs/api/openai-compatibility.mdx). Compatibility and output quality for a selected server/model require a live test; mocked transport tests do not establish them.

## Persistence

Alembic revision `1486d73c77a0` adds `research_runs` and `research_citations`.

Each run stores a UUID, question hash, effective filters, concrete generation, status, prompt version, provider/model names, timings, timestamp, and bounded response. Original question text, endpoint URL/key, and raw model output are excluded. Exact quotes are public source evidence, retained in the response and citation ledger for reproducibility.

Each answered claim links to the run, canonical chunk, and exact generation membership through foreign keys. Its exact quote, hash, offsets, and `exact_canonical_passage` verification status are stored in the same SQL transaction. Existing generations/chunks are retained, so publication of a new index does not invalidate saved citations. `show` reads the saved snapshot without rerunning retrieval or inference. Infrastructure/integrity failures cannot claim a successfully persisted trace when PostgreSQL itself is unavailable.

## Verification boundary

Automated tests establish schema validation, transport bounds, abstention branches, exact quotation fidelity, scope enforcement, canonical passage/claim joins, and trace round trips. Synthetic model responses appear only in automated fixtures. Real-data verification uses actual SEC documents and real PostgreSQL/Qdrant/model embeddings. If generation is disabled, the verification report explicitly records `live_model_verified: false`; it does not invent a production answer to satisfy a test.
