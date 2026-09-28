"""Dense baseline with exact extractive claims, explicit abstention, and durable tracing."""

import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import QdrantClient

from app.core.config import Settings
from app.database import schema as db
from app.indexing.chunking import digest
from app.indexing.corpus import latest_accession
from app.indexing.embeddings import Encoder
from app.indexing.index import search
from app.research.models import PROMPT_VERSION
from app.research.provider import CompatibleSelector, ModelFailure, Selector


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=3, max_length=2000)
    ticker: str = Field(pattern=r"^[A-Z0-9.\-]{1,20}$")
    form: Literal["10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A"]
    accession: str | None = Field(default=None, pattern=r"^\d{10}-\d{2}-\d{6}$")


def answer_from_evidence(
    request: ResearchRequest,
    retrieved: dict[str, Any],
    settings: Settings,
    selector: Selector | None,
) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    used_chars = 0
    seen = set()
    for hit in retrieved.get("results", []):
        if (
            not math.isfinite(hit["score"])
            or hit["score"] < settings.rag_min_score
            or hit["chunk_id"] in seen
        ):
            continue
        if digest(hit["text"]) != hit["chunk_hash"]:
            raise ValueError("Evidence text hash mismatch")
        if request.ticker not in hit["tickers"] or request.form != hit["filing_type"]:
            raise ValueError("Evidence scope mismatch")
        if request.accession and hit["accession_number"] != request.accession:
            raise ValueError("Evidence accession mismatch")
        if hit["index_generation"] != retrieved.get("generation"):
            raise ValueError("Mixed evidence generations")
        if used_chars + len(hit["text"]) > settings.rag_max_context_chars:
            continue  # Never truncate evidence or fabricate offsets to fit the context.
        evidence.append({"source_id": f"S{len(evidence) + 1}", **hit})
        seen.add(hit["chunk_id"])
        used_chars += len(hit["text"])
        if len(evidence) == settings.rag_top_k:
            break
    response: dict[str, Any] = {
        "status": "INSUFFICIENT_EVIDENCE",
        "reason": "no_matching_evidence",
        "baseline": PROMPT_VERSION,
        "answer": None,
        "retrieved_evidence": evidence,
        "generated_explanation": None,
        "calculated_values": [],
        "market_data": [],
        "model_outputs": [],
        "generation": retrieved.get("generation"),
        "scope": {"ticker": request.ticker, "form": request.form, "accession": request.accession},
        "upstream_refreshed": False,
        "source_freshness": "Stored filings; this request did not check SEC for updates",
        "relevance_verified": False,
        "generation_ms": 0.0,
    }
    if not evidence:
        return response
    if selector is None:
        response.update(status="EVIDENCE_ONLY", reason="model_not_configured")
        return response
    model_evidence = [
        {
            key: e[key]
            for key in (
                "source_id",
                "text",
                "company",
                "tickers",
                "filing_type",
                "filing_date",
                "accepted_at",
                "report_period",
                "section",
            )
        }
        for e in evidence
    ]
    started = time.monotonic()
    try:
        selection = selector.select(request.question, model_evidence)
    except ModelFailure as exc:
        response.update(status="EVIDENCE_ONLY", reason="model_" + str(exc))
        return response
    finally:
        response["generation_ms"] = round((time.monotonic() - started) * 1000, 3)
    sources = {e["source_id"]: e for e in evidence}
    if not selection.answerable:
        response["reason"] = "model_reported_insufficient_evidence"
        return response
    if any(source_id not in sources for source_id in selection.source_ids):
        response["reason"] = "invalid_citation_selection"
        return response
    claims: list[dict[str, Any]] = []
    for source_id in selection.source_ids:
        source = sources[source_id]
        claims.append(
            {
                "claim_id": f"C{len(claims) + 1}",
                "kind": "filing_quote",
                "text": source["text"],
                "source_id": source_id,
                "verification": "exact_canonical_passage",
                "citation": {
                    key: source[key]
                    for key in (
                        "chunk_id",
                        "index_generation",
                        "source_url",
                        "company",
                        "tickers",
                        "accession_number",
                        "filing_type",
                        "filing_date",
                        "accepted_at",
                        "report_period",
                        "section",
                        "page",
                        "source_anchor",
                        "document_hash",
                        "parsed_hash",
                        "chunk_hash",
                        "start_offset",
                        "end_offset",
                        "offset_basis",
                        "source_observed_at",
                    )
                },
            }
        )
    response.update(
        status="ANSWERED",
        reason=None,
        answer={
            "kind": "extractive",
            "claims": claims,
            "qualification": (
                "Quoted filing passages selected by the model; not independent factual verification"
            ),
        },
    )
    return response


def save_run(
    engine: sa.Engine,
    request: ResearchRequest,
    settings: Settings,
    response: dict[str, Any],
    retrieval_ms: float,
) -> dict[str, Any]:
    run_id = uuid4()
    response = {
        **response,
        "run_id": str(run_id),
        "retrieval_ms": retrieval_ms,
        "created_at": datetime.now(UTC).isoformat(),
    }
    with engine.begin() as connection:
        connection.execute(
            db.research_runs.insert().values(
                id=run_id,
                question_hash=digest(request.question),
                filters=response["scope"],
                generation_id=response["generation"],
                status=response["status"],
                prompt_version=PROMPT_VERSION,
                model_provider=settings.llm_provider,
                model_name=settings.llm_model,
                retrieval_ms=retrieval_ms,
                generation_ms=response["generation_ms"],
                response=response,
            )
        )
        for claim in (response["answer"] or {}).get("claims", []):
            citation = claim["citation"]
            connection.execute(
                db.research_citations.insert().values(
                    run_id=run_id,
                    claim_id=claim["claim_id"],
                    generation_id=citation["index_generation"],
                    chunk_id=citation["chunk_id"],
                    exact_quote=claim["text"],
                    quote_hash=digest(claim["text"]),
                    start_offset=citation["start_offset"],
                    end_offset=citation["end_offset"],
                    verification_status=claim["verification"],
                )
            )
    return response


def research(
    engine: sa.Engine,
    encoder: Encoder,
    qdrant: QdrantClient,
    settings: Settings,
    request: ResearchRequest,
    *,
    selector: Selector | None = None,
    latest: Callable[[Settings, str, str], str | None] = latest_accession,
) -> dict[str, Any]:
    started = time.monotonic()
    accession = request.accession or latest(settings, request.ticker, request.form)
    if accession is None:
        retrieved: dict[str, Any] = {"results": [], "generation": None}
    else:
        request = request.model_copy(update={"accession": accession})
        retrieved = search(
            engine,
            encoder,
            qdrant,
            request.question,
            ticker=request.ticker,
            form=request.form,
            accession=accession,
            limit=settings.rag_top_k,
        )
    retrieval_ms = round((time.monotonic() - started) * 1000, 3)
    if selector is None and settings.llm_provider != "disabled":
        selector = CompatibleSelector(settings)
    response = answer_from_evidence(request, retrieved, settings, selector)
    if accession is None:
        response["reason"] = "no_discovered_filing"
    elif not retrieved.get("results"):
        response["reason"] = "requested_filing_not_indexed_or_no_active_index"
    return save_run(engine, request, settings, response, retrieval_ms)


def read_run(engine: sa.Engine, run_id: UUID) -> dict[str, Any] | None:
    with engine.connect() as connection:
        value = connection.execute(
            sa.select(db.research_runs.c.response).where(db.research_runs.c.id == run_id)
        ).scalar_one_or_none()
        return dict(value) if value is not None else None
