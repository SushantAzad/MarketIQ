"""Resumable node boundaries, pinned inputs, bounded retries and single-owner jobs."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict
from uuid import UUID, uuid4

import sqlalchemy as sa
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from app.core.config import Settings
from app.database import schema as db
from app.indexing.corpus import latest_accession
from app.routing.models import Intent, QueryPlan, QueryRequest, Step
from app.routing.planner import load_issuers, plan_query
from app.routing.service import DocumentReader, document_reader, execute_query
from app.workflow.validation import validate_response

VERSION = "research-workflow-v1"
NODES = ("plan", "execute", "validate", "finalize")


def purge_expired(engine: sa.Engine) -> int:
    deleted = 0
    with engine.begin() as connection:
        ids = connection.scalars(
            sa.select(db.workflow_jobs.c.id).where(
                db.workflow_jobs.c.expires_at <= datetime.now(UTC)
            )
        ).all()
        for job_id in ids:
            key = int.from_bytes(job_id.bytes[:8], "big", signed=True)
            if connection.scalar(sa.text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}):
                result = connection.execute(
                    db.workflow_jobs.delete().where(db.workflow_jobs.c.id == job_id)
                )
                deleted += result.rowcount
    return deleted


class State(TypedDict):
    request: dict[str, Any]
    cutoff: str
    next_node: str
    attempts: dict[str, int]
    query_plan: dict[str, Any]
    accessions: dict[str, str | None]
    response: dict[str, Any]


def create_job(engine: sa.Engine, settings: Settings, request: QueryRequest) -> UUID:
    now, job_id = datetime.now(UTC), uuid4()
    state: State = {
        "request": request.model_dump(mode="json"),
        "cutoff": (request.as_of or now).isoformat(),
        "next_node": "plan",
        "attempts": {},
        "query_plan": {},
        "accessions": {},
        "response": {},
    }
    with engine.begin() as connection:
        connection.execute(
            db.workflow_jobs.insert().values(
                id=job_id,
                version=VERSION,
                status="QUEUED",
                state=state,
                updated_at=now,
                expires_at=now + timedelta(days=settings.workflow_retention_days),
            )
        )
    return job_id


def read_job(engine: sa.Engine, job_id: UUID) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = (
            connection.execute(sa.select(db.workflow_jobs).where(db.workflow_jobs.c.id == job_id))
            .mappings()
            .first()
        )
    if row is None:
        return None
    if row["expires_at"] <= datetime.now(UTC):
        return {"id": str(job_id), "status": "EXPIRED"}
    return dict(row)


def checkpoint(
    engine: sa.Engine, job_id: UUID, state: State, status: str = "RUNNING", error: str | None = None
) -> None:
    clean = json.loads(json.dumps(state, default=str))
    with engine.begin() as connection:
        connection.execute(
            db.workflow_jobs.update()
            .where(db.workflow_jobs.c.id == job_id)
            .values(state=clean, status=status, last_error=error, updated_at=datetime.now(UTC))
        )


def run_job(
    engine: sa.Engine,
    settings: Settings,
    job_id: UUID,
    *,
    documents: DocumentReader | None = None,
    stop_after: str | None = None,
) -> dict[str, Any]:
    if stop_after is not None and stop_after not in NODES:
        raise ValueError("Unknown workflow boundary")
    lock_key = int.from_bytes(job_id.bytes[:8], "big", signed=True)
    with engine.connect() as lock:
        acquired = lock.scalar(sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key})
        lock.commit()
        if not acquired:
            return {"id": str(job_id), "status": "BUSY"}
        try:
            job = read_job(engine, job_id)
            if job is None:
                return {"id": str(job_id), "status": "NOT_FOUND"}
            if job["status"] in {"EXPIRED", "COMPLETED", "NEEDS_CLARIFICATION"}:
                return job
            if job["version"] != VERSION:
                return {"id": str(job_id), "status": "INCOMPATIBLE_VERSION"}
            state: State = job["state"]
            request = QueryRequest.model_validate(state["request"])

            def stage(node: str, current: State) -> dict[str, Any]:
                current["attempts"][node] = current["attempts"].get(node, 0) + 1
                if current["attempts"][node] > 3:
                    checkpoint(engine, job_id, current, "FAILED", "attempt_limit_reached")
                    return {"next_node": END}
                checkpoint(engine, job_id, current)
                try:
                    if node == "plan":
                        with engine.connect() as connection:
                            plan = plan_query(request, load_issuers(connection))
                        current["query_plan"] = plan.model_dump(mode="json")
                        if plan.status == "NEEDS_CLARIFICATION":
                            current["response"] = {
                                "status": "NEEDS_CLARIFICATION",
                                "plan": current["query_plan"],
                            }
                            current["next_node"] = END
                            checkpoint(engine, job_id, current, "NEEDS_CLARIFICATION")
                            return dict(current)
                        for step in plan.steps:
                            if step.intent in {Intent.DOCUMENT, Intent.COMPANY}:
                                current["accessions"][step.ticker] = (
                                    request.accession
                                    or latest_accession(settings, step.ticker, str(request.form))
                                )
                    elif node == "execute":

                        def pinned_reader(step: Step, original: QueryRequest) -> dict[str, Any]:
                            accession = current["accessions"].get(step.ticker)
                            if accession is None:
                                return {
                                    "status": "INSUFFICIENT_EVIDENCE",
                                    "retrieved_evidence": [],
                                    "reason": "no_discovered_filing",
                                }
                            pinned = original.model_copy(update={"accession": accession})
                            return (
                                documents(step, pinned)
                                if documents
                                else document_reader(engine, settings, step, pinned)
                            )

                        current["response"] = execute_query(
                            engine,
                            settings,
                            request,
                            documents=pinned_reader,
                            prepared_plan=QueryPlan.model_validate(current["query_plan"]),
                            structured_cutoff=datetime.fromisoformat(current["cutoff"]),
                        )
                    elif node == "validate":
                        current["response"] = validate_response(
                            engine, current["response"], current["accessions"]
                        )
                    else:
                        response = current["response"]
                        values = response["retrieved_facts"] + response["calculated_values"]
                        evidence = response["document_evidence"]
                        successes = sum(r["result"].get("value") is not None for r in values)
                        successes += sum(
                            bool(r["result"].get("retrieved_evidence")) for r in evidence
                        )
                        incomplete = (
                            len(values) + len(evidence) + len(response["unavailable"]) > successes
                        )
                        incomplete |= any(r["status"] != "ALIGNED" for r in response["comparisons"])
                        response["status"] = (
                            ("PARTIAL" if incomplete else "AVAILABLE")
                            if successes
                            else (
                                "UNAVAILABLE"
                                if response["unavailable"]
                                else "INSUFFICIENT_EVIDENCE"
                            )
                        )
                        response["workflow_version"] = VERSION
                        response["workflow_id"] = str(job_id)
                    following = NODES[NODES.index(node) + 1] if node != "finalize" else END
                    current["next_node"] = following
                    checkpoint(
                        engine, job_id, current, "COMPLETED" if following == END else "RUNNING"
                    )
                    return {**current, "next_node": END if node == stop_after else following}
                except Exception:
                    current["next_node"] = node
                    checkpoint(engine, job_id, current, "FAILED", f"{node}_failed")
                    return {"next_node": END}

            graph = StateGraph(State)
            destinations = {name: name for name in NODES} | {END: END}
            graph.add_conditional_edges(START, lambda s: s["next_node"], destinations)
            for node in NODES:

                def invoke(current: State, name: str = node) -> dict[str, Any]:
                    return stage(name, current)

                graph.add_node(node, invoke)
                graph.add_conditional_edges(node, lambda s: s["next_node"], destinations)
            # No question/evidence export to ambient LangSmith tracing settings.
            with tracing_context(enabled=False):
                graph.compile().invoke(state, {"recursion_limit": 12})
            return read_job(engine, job_id) or {"id": str(job_id), "status": "NOT_FOUND"}
        finally:
            lock.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
            lock.commit()
