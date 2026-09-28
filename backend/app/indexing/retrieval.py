"""Same-generation and same-SQL-scope dense/BM25 retrieval with rank-only fusion."""

import math
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from qdrant_client import QdrantClient

from app.database import schema as db
from app.indexing import lexical
from app.indexing.chunking import digest
from app.indexing.embeddings import Encoder


def retrieve(
    engine: sa.Engine,
    encoder: Encoder | None,
    qdrant: QdrantClient,
    query: str,
    *,
    ticker: str | None,
    form: str | None,
    accession: str | None,
    section: str | None,
    accepted_before: datetime | None,
    limit: int,
    mode: str,
    min_dense_score: float | None,
) -> dict[str, Any]:
    from app.indexing.index import _dense_search, alias_target

    if not query.strip() or not 1 <= limit <= 40 or mode not in {"dense", "bm25", "hybrid"}:
        raise ValueError("Invalid retrieval request")
    if accepted_before is not None and accepted_before.tzinfo is None:
        raise ValueError("Acceptance cutoff requires timezone")
    if min_dense_score is not None and (
        not math.isfinite(min_dense_score) or not -1 <= min_dense_score <= 1
    ):
        raise ValueError("Invalid dense cutoff")
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        generation = (
            connection.execute(
                sa.select(db.index_generations).where(db.index_generations.c.state == "active")
            )
            .mappings()
            .one_or_none()
        )
        metadata: dict[str, Any] = {
            "mode": mode,
            "candidate_window": lexical.CANDIDATES,
            "rrf_version": lexical.RRF_VERSION if mode == "hybrid" else None,
            "bm25_version": lexical.VERSION if mode != "dense" else None,
            "min_dense_score": min_dense_score,
            "scoped_chunks": 0,
        }
        output: dict[str, Any] = {
            "status": "UNAVAILABLE",
            "results": [],
            "generation": str(generation["id"]) if generation else None,
            "scope": "SQL-filtered indexed filings",
            "upstream_refreshed": False,
            "retrieval": metadata,
        }
        if generation is None:
            output["reason"] = "No active generation"
            return output
        if alias_target(qdrant) != generation["collection_name"]:
            raise ValueError("Index activation/model mismatch; replay required")
        if mode != "dense":
            meta = (
                connection.execute(
                    sa.select(db.lexical_generations).where(
                        db.lexical_generations.c.generation_id == generation["id"]
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                meta is None
                or meta["version"] != lexical.VERSION
                or meta["corpus_manifest"] != generation["manifest_hash"]
                or meta["chunk_count"] != generation["chunk_count"]
            ):
                raise ValueError("Lexical generation unavailable; rebuild required")
            count = connection.scalar(
                sa.select(sa.func.count())
                .select_from(db.lexical_chunks)
                .where(db.lexical_chunks.c.generation_id == generation["id"])
            )
            if count != generation["chunk_count"]:
                raise ValueError("Lexical membership count mismatch")
        payload = db.generation_chunks.c.payload
        statement = (
            sa.select(
                db.document_chunks.c.id,
                db.document_chunks.c.text,
                db.document_chunks.c.content_hash,
                payload,
            )
            .join(db.generation_chunks, db.generation_chunks.c.chunk_id == db.document_chunks.c.id)
            .where(db.generation_chunks.c.generation_id == generation["id"])
        )
        if ticker is not None:
            statement = statement.where(payload["tickers"].contains([ticker]))
        for key, value in {
            "filing_type": form,
            "accession_number": accession,
            "section": section,
        }.items():
            if value is not None:
                statement = statement.where(payload[key].astext == value)
        if accepted_before is not None:
            statement = statement.where(
                sa.cast(payload["accepted_at"].astext, sa.DateTime(timezone=True))
                <= accepted_before
            )
        canonical = {str(r["id"]): dict(r) for r in connection.execute(statement).mappings()}
        metadata["scoped_chunks"] = len(canonical)
        if not canonical:
            return output
        for row in canonical.values():
            if (
                digest(row["text"]) != row["content_hash"]
                or row["payload"]["chunk_hash"] != row["content_hash"]
                or row["payload"]["index_generation"] != str(generation["id"])
            ):
                raise ValueError("Canonical evidence checksum mismatch")
        sparse: list[tuple[str, float]] = []
        if mode != "dense":
            stored = {
                str(r["chunk_id"]): dict(r)
                for r in connection.execute(
                    sa.select(db.lexical_chunks).where(
                        db.lexical_chunks.c.generation_id == generation["id"],
                        db.lexical_chunks.c.chunk_id.in_([UUID(key) for key in canonical]),
                    )
                ).mappings()
            }
            if set(stored) != set(canonical):
                raise ValueError("Lexical scoped membership mismatch")
            for key, row in stored.items():
                expected = lexical.terms(canonical[key]["text"])
                if (
                    row["terms"] != expected
                    or row["length"] != sum(expected.values())
                    or row["content_hash"] != canonical[key]["content_hash"]
                ):
                    raise ValueError("Lexical evidence checksum mismatch")
            sparse = lexical.bm25(query, {key: row["terms"] for key, row in stored.items()})[
                : lexical.CANDIDATES
            ]
        dense: list[tuple[str, float]] = []
        if mode != "bm25":
            if encoder is None:
                raise ValueError("Dense retrieval requires a provisioned encoder")
            raw = _dense_search(
                engine,
                encoder,
                qdrant,
                query,
                limit=lexical.CANDIDATES,
                generation_id=generation["id"],
                allowed_ids=list(canonical),
            )
            if raw["generation"] != str(generation["id"]):
                raise ValueError("Mixed retrieval generations")
            for hit in raw["results"]:
                key = hit["chunk_id"]
                if key not in canonical or any(
                    hit[k] != v for k, v in canonical[key]["payload"].items()
                ):
                    raise ValueError("Dense candidate outside canonical scope")
                if math.isfinite(hit["score"]) and (
                    min_dense_score is None or hit["score"] >= min_dense_score
                ):
                    dense.append((key, hit["score"]))
            dense.sort(key=lambda item: (-item[1], item[0]))
        rankings = (
            lexical.fuse([k for k, _ in dense], [k for k, _ in sparse])
            if mode == "hybrid"
            else (dense if mode == "dense" else sparse)
        )
        dense_scores, sparse_scores = dict(dense), dict(sparse)
        dense_ranks = {key: i for i, (key, _) in enumerate(dense, 1)}
        sparse_ranks = {key: i for i, (key, _) in enumerate(sparse, 1)}
        output["results"] = [
            {
                **canonical[key]["payload"],
                "text": canonical[key]["text"],
                "score": score,
                "score_kind": {"hybrid": "rrf", "bm25": "bm25", "dense": "cosine"}[mode],
                "dense_score": dense_scores.get(key),
                "bm25_score": sparse_scores.get(key),
                "dense_rank": dense_ranks.get(key),
                "bm25_rank": sparse_ranks.get(key),
            }
            for key, score in rankings[:limit]
        ]
        metadata.update(dense_candidates=len(dense), bm25_candidates=len(sparse))
        output["status"] = "AVAILABLE" if output["results"] else "UNAVAILABLE"
        return output
