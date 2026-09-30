"""Validate retained generation membership, canonical text and exact citations."""

from typing import Any
from uuid import UUID

import sqlalchemy as sa

from app.database import schema as db
from app.indexing.chunking import digest


def validate_document(connection: sa.Connection, row: dict[str, Any]) -> bool:
    try:
        response, step = row["result"], row["step"]
        if response.get("generated_explanation") is not None or response.get("model_outputs"):
            return False
        if response.get("answer") and response["answer"].get("kind") != "extractive":
            return False
        hits = response.get("retrieved_evidence", [])
        sources = {}
        for hit in hits:
            canonical = (
                connection.execute(
                    sa.select(
                        db.document_chunks.c.text,
                        db.document_chunks.c.content_hash,
                        db.generation_chunks.c.payload,
                    )
                    .join(
                        db.generation_chunks,
                        db.generation_chunks.c.chunk_id == db.document_chunks.c.id,
                    )
                    .where(
                        db.document_chunks.c.id == UUID(hit["chunk_id"]),
                        db.generation_chunks.c.generation_id == UUID(response["generation"]),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                canonical is None
                or canonical["text"] != hit["text"]
                or digest(hit["text"]) != canonical["content_hash"]
                or hit["chunk_hash"] != canonical["content_hash"]
            ):
                return False
            if any(hit.get(k) != v for k, v in canonical["payload"].items()):
                return False
            if (
                step["ticker"] not in hit["tickers"]
                or hit["index_generation"] != response["generation"]
            ):
                return False
            scope = response["scope"]
            if (
                scope["ticker"] != step["ticker"]
                or scope["form"] != hit["filing_type"]
                or scope.get("accession") != hit["accession_number"]
            ):
                return False
            if hit["source_id"] in sources:
                return False
            sources[hit["source_id"]] = hit
        for claim in (response.get("answer") or {}).get("claims", []):
            source = sources.get(claim["source_id"])
            if source is None or claim["text"] != source["text"] or claim["kind"] != "filing_quote":
                return False
            citation = claim["citation"]
            required = {
                "chunk_id",
                "index_generation",
                "chunk_hash",
                "document_hash",
                "source_url",
                "start_offset",
                "end_offset",
                "accession_number",
            }
            if not required <= citation.keys() or any(
                source.get(k) != v for k, v in citation.items()
            ):
                return False
        return not (response.get("answer") and not (response["answer"].get("claims")))
    except (KeyError, ValueError, TypeError):
        return False


def validate_response(
    engine: sa.Engine, response: dict[str, Any], accessions: dict[str, str | None]
) -> dict[str, Any]:
    verified = []
    with engine.connect() as connection:
        for row in response["document_evidence"]:
            scope = row["result"].get("scope", {})
            has_evidence = bool(row["result"].get("retrieved_evidence"))
            expected_form = response["plan"]["request"]["form"]
            scoped = not has_evidence or (
                scope.get("form") == expected_form
                and scope.get("accession") == accessions.get(row["step"]["ticker"])
            )
            if scoped and validate_document(connection, row):
                verified.append(row)
            else:
                response["unavailable"].append(
                    {"step": row["step"], "reason": "citation_validation_failed"}
                )
    response["document_evidence"] = verified
    response["citation_validation"] = "passed" if verified else "no_validated_document_evidence"
    return response
