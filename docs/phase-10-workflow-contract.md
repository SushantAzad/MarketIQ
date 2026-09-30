# Phase 10: resumable research orchestration

`research-workflow-v1` uses LangGraph's `StateGraph` with four explicit nodes: **plan → execute → validate → finalize**. Conditional edges stop at clarification, operational failure, retry exhaustion, or completion. The execution node uses Phase 9's allowlisted dispatcher; no autonomous agent, generated SQL, arbitrary tool invocation, or unrestricted loop is introduced. The dependency lock currently resolves LangGraph 1.2.12.

## Durable state and recovery

PostgreSQL `workflow_jobs` stores the version, status, typed-request JSON, frozen query plan, structured-data cutoff, per-issuer selected accession, node attempts, accumulated response, next node, timestamps, and expiry. These are **application-owned node-boundary checkpoints**, not LangGraph's native checkpoint-saver/time-travel API. Each graph invocation starts from the persisted next node; no in-memory graph object is required for recovery.

The structured cutoff is captured at submission unless the request supplies one. The plan and latest locally discovered accession choices are persisted at the plan boundary. Execution uses the saved plan and cutoff rather than reparsing intent or selecting a newer accession on restart. Source generation/model metadata from completed research is retained in its response; before execution commits, active retrieval generations and runtime model settings are not independently frozen. If an interrupted execution must run again, those runtime inputs may have changed; this is not an exactly reproducible model-inference guarantee.

Each node may start at most three times. Ordinary exceptions record a sanitized node failure code and retain that node as the resume point. The caller explicitly retries with `run`; there is no background polling loop. A hard process exit can leave the job marked RUNNING, but the session-level PostgreSQL advisory lock is released on disconnect and another runner can resume. Concurrent callers receive BUSY. Completed and clarification jobs are returned without re-execution. Version mismatches are refused.

Successfully checkpointed nodes are not replayed. An interrupted node can repeat work performed before its checkpoint. Calculation records use their existing content-derived IDs; replay with identical inputs does not duplicate them. Research/provider reads are at least once: a crash after an external/model call or research-run commit but before the workflow checkpoint can repeat the call and create another research audit record. There are no trading, messaging, or payment actions. The execution node is one boundary covering all routed steps; recovery is not yet per tool call.

## Validation and response behavior

Validation checks every document evidence item against its retained index generation, SQL canonical chunk text/hash, complete stored payload, issuer/form/accession scope, and source ID uniqueness. Extractive claims must match the source passage verbatim and carry matching mandatory citation metadata. Unverified generated explanation/model output in the document response is rejected. Invalid document evidence is removed and recorded as `citation_validation_failed`; valid structured facts and calculations remain available.

Finalize recomputes AVAILABLE/PARTIAL/UNAVAILABLE from surviving results. A completed workflow with no values/evidence and no operational unavailable step reports `INSUFFICIENT_EVIDENCE`. This is distinct from a FAILED job, which can be retried. A valid canonical quote is not a semantic relevance or causality assessment. No free-form explanation is synthesized.

## Storage and operation

Migration `10bc42a3cfb1` adds `workflow_jobs` without changing existing source or research tables. Requests and evidence are stored as JSONB, not executable serialized objects. Errors omit raw provider exceptions. LangSmith tracing is explicitly disabled for graph invocation even if ambient tracing settings are enabled.

`WORKFLOW_RETENTION_DAYS` defaults to seven (allowed range 1–90). Read/run hide expired job contents. Physical deletion is explicit with `app.workflow purge-expired`, which skips actively locked jobs. Operations must schedule that command if automatic removal is required; no scheduler was installed in Phase 10. This retention applies to workflow checkpoints, not previously existing filing, calculation, or research audit tables.

Commands from repository root:

```powershell
uv run --locked --directory backend python -m app.workflow submit "Explain NVDA net margin" --basis annual --period-end 2026-01-25 --form 10-K --accession 0001045810-26-000021
uv run --locked --directory backend python -m app.workflow run <job-id>
uv run --locked --directory backend python -m app.workflow show <job-id>
uv run --locked --directory backend python -m app.workflow purge-expired
```

Submit returns a queued job ID. Run is synchronous and also resumes; show reads the saved state. A clarification response requires a corrected new submission. Worker queues, public POST/job/SSE endpoints, authentication and deployment scheduling remain later phases. Model files must already be provisioned; jobs do not download or train models.

Primary API reference: [LangGraph StateGraph](https://reference.langchain.com/python/langgraph/graph/state/StateGraph). Next is Phase 11's risk-model cohort and evaluation gate; frontend design remains Phase 14.
