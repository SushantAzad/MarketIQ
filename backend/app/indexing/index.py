"""Recoverable generation publication. PostgreSQL is the retrieval source of truth."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from qdrant_client import QdrantClient, models

from app.core.config import Settings
from app.database import schema as db
from app.indexing.chunking import digest
from app.indexing.corpus import import_corpus
from app.indexing.embeddings import Encoder, validate_vectors
from app.indexing.lexical import VERSION, publish_lexical
from app.services.financial_import import put, stable_id

ALIAS = "filing_chunks_active"
LOCK = 71432004


def client(settings: Settings) -> QdrantClient:
    return QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None,
        timeout=60,
    )


def payload_hash(payload: dict[str, Any]) -> str:
    return digest(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def alias_target(qdrant: QdrantClient) -> str | None:
    return next(
        (a.collection_name for a in qdrant.get_aliases().aliases if a.alias_name == ALIAS), None
    )


def validate_collection(
    qdrant: QdrantClient,
    name: str,
    expected: dict[str, dict[str, Any]],
    dimension: int,
    vectors: dict[str, list[float]],
) -> None:
    info = qdrant.get_collection(name)
    config = info.config.params.vectors
    if (
        not isinstance(config, dict)
        or "text" not in config
        or config["text"].size != dimension
        or config["text"].distance != models.Distance.COSINE
    ):
        raise ValueError("Collection vector configuration mismatch")
    if qdrant.count(name, exact=True).count != len(expected):
        raise ValueError("Collection count mismatch")
    found = set()
    offset: str | int | UUID | None = None
    while True:
        points, offset = qdrant.scroll(
            name, limit=128, offset=offset, with_payload=True, with_vectors=True
        )
        for point in points:
            key = str(point.id)
            if key not in expected or point.payload != expected[key]:
                raise ValueError("Collection payload checksum mismatch")
            vector = point.vector.get("text") if isinstance(point.vector, dict) else None
            if not isinstance(vector, list) or len(vector) != dimension:
                raise ValueError("Stored vector dimension mismatch")
            target = vectors[expected[key]["chunk_hash"]]
            # Qdrant stores normalized float32 vectors; permit float rounding only.
            norm = sum(v * v for v in target) ** 0.5
            if any(abs(a - b / norm) > 1e-5 for a, b in zip(vector, target, strict=True)):
                raise ValueError("Stored vector checksum mismatch")
            found.add(key)
        if offset is None:
            break
    if found != set(expected):
        raise ValueError("Collection membership mismatch")


def publish(
    engine: sa.Engine,
    settings: Settings,
    encoder: Encoder,
    qdrant: QdrantClient,
    *,
    rebuild: bool = False,
    from_canonical: bool = False,
) -> dict[str, Any]:
    with engine.connect() as connection:
        # Session lock spans SQL commits and external writes, across CLI processes/hosts.
        connection.execute(sa.text("SELECT pg_advisory_lock(:key)"), {"key": LOCK})
        connection.commit()
        try:
            return _publish(connection, settings, encoder, qdrant, rebuild, from_canonical)
        finally:
            connection.rollback()
            connection.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK})
            connection.commit()


def _publish(
    connection: sa.Connection,
    settings: Settings,
    encoder: Encoder,
    qdrant: QdrantClient,
    rebuild: bool,
    from_canonical: bool,
) -> dict[str, Any]:
    if from_canonical:
        if not rebuild:
            raise ValueError("Canonical recovery requires an explicit rebuild")
        active = (
            connection.execute(
                sa.select(db.index_generations).where(db.index_generations.c.state == "active")
            )
            .mappings()
            .one()
        )
        if active["model_key"] != encoder.key:
            raise ValueError("Canonical recovery requires the original model")
        corpus = []
        for row in connection.execute(
            sa.select(
                db.document_chunks.c.id,
                db.document_chunks.c.text,
                db.document_chunks.c.content_hash,
                db.generation_chunks.c.payload,
            )
            .join(db.generation_chunks, db.generation_chunks.c.chunk_id == db.document_chunks.c.id)
            .where(db.generation_chunks.c.generation_id == active["id"])
        ).mappings():
            payload = dict(row["payload"])
            payload.pop("index_generation")
            if (
                digest(row["text"]) != row["content_hash"]
                or row["content_hash"] != payload["chunk_hash"]
            ):
                raise ValueError("Canonical evidence checksum mismatch")
            corpus.append({"id": row["id"], "text": row["text"], "payload": payload})
        if len(corpus) != active["chunk_count"] or not corpus:
            raise ValueError("Canonical generation membership mismatch")
    else:
        corpus = import_corpus(connection, settings, encoder)
    corpus.sort(key=lambda c: str(c["id"]))
    manifest = payload_hash(
        {
            "model": encoder.key,
            "chunks": [{"id": str(c["id"]), "payload": c["payload"]} for c in corpus],
        }
    )
    if from_canonical and manifest != active["manifest_hash"]:
        raise ValueError("Canonical manifest checksum mismatch")
    existing = connection.execute(
        sa.select(db.index_generations.c.id)
        .join(
            db.lexical_generations,
            db.lexical_generations.c.generation_id == db.index_generations.c.id,
        )
        .where(
            db.index_generations.c.manifest_hash == manifest,
            db.index_generations.c.state == "active",
            db.lexical_generations.c.version == VERSION,
        )
    ).scalar_one_or_none()
    generation_id = uuid4() if rebuild else existing or stable_id("generation", manifest + VERSION)
    name = f"filing_chunks_{settings.embedding_revision[:12]}_{generation_id.hex}"
    expected = {
        str(c["id"]): {**c["payload"], "index_generation": str(generation_id)} for c in corpus
    }
    put(
        connection,
        db.index_generations,
        [
            {
                "id": generation_id,
                "collection_name": name,
                "manifest_hash": manifest,
                "model_key": encoder.key,
                "model_name": settings.embedding_model,
                "model_revision": settings.embedding_revision,
                "dimension": encoder.dimension,
                "chunk_count": len(corpus),
                "state": "building",
            }
        ],
    )
    put(
        connection,
        db.generation_chunks,
        [
            {"generation_id": generation_id, "chunk_id": c["id"], "payload": expected[str(c["id"])]}
            for c in corpus
        ],
    )
    connection.commit()
    texts = {c["payload"]["chunk_hash"]: c["text"] for c in corpus}
    cached = dict(
        connection.execute(
            sa.select(db.embedding_cache.c.content_hash, db.embedding_cache.c.vector).where(
                db.embedding_cache.c.model_key == encoder.key
            )
        )
        .tuples()
        .all()
    )
    missing = sorted(set(texts) - set(cached))
    for start in range(0, len(missing), settings.embedding_batch_size):
        hashes = missing[start : start + settings.embedding_batch_size]
        batch = encoder.encode([texts[h] for h in hashes])
        validate_vectors(batch, len(hashes), encoder.dimension)
        put(
            connection,
            db.embedding_cache,
            [
                {"model_key": encoder.key, "content_hash": h, "vector": v}
                for h, v in zip(hashes, batch, strict=True)
            ],
        )
        connection.commit()
        cached.update(zip(hashes, batch, strict=True))
    validate_vectors([cached[h] for h in texts], len(texts), encoder.dimension)
    if not qdrant.collection_exists(name):
        qdrant.create_collection(
            name,
            vectors_config={
                "text": models.VectorParams(size=encoder.dimension, distance=models.Distance.COSINE)
            },
        )
    for key, kind in {
        "company_id": "keyword",
        "filing_id": "keyword",
        "filing_type": "keyword",
        "accepted_at": "datetime",
        "fiscal_year": "integer",
        "section": "keyword",
        "tickers": "keyword",
        "accession_number": "keyword",
    }.items():
        qdrant.create_payload_index(
            name, key, field_schema=models.PayloadSchemaType(kind), wait=True
        )
    active = (
        connection.execute(
            sa.select(db.index_generations.c.state).where(
                db.index_generations.c.id == generation_id
            )
        ).scalar_one()
        == "active"
    )
    if not active:
        for start in range(0, len(corpus), 64):
            qdrant.upsert(
                name,
                points=[
                    models.PointStruct(
                        id=str(c["id"]),
                        vector={"text": cached[c["payload"]["chunk_hash"]]},
                        payload=expected[str(c["id"])],
                    )
                    for c in corpus[start : start + 64]
                ],
                wait=True,
            )
    validate_collection(qdrant, name, expected, encoder.dimension, cached)
    publish_lexical(connection, generation_id, corpus, manifest)
    if not active:
        connection.execute(
            db.index_generations.update()
            .where(db.index_generations.c.id == generation_id)
            .values(state="validated")
        )
        connection.commit()
    old = alias_target(qdrant)
    if old != name:
        operations: list[models.CreateAliasOperation | models.DeleteAliasOperation] = []
        if old:
            operations.append(
                models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=ALIAS))
            )
        operations.append(
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(collection_name=name, alias_name=ALIAS)
            )
        )
        qdrant.update_collection_aliases(operations)
    # Crash after alias switch: searches fail closed; replay validates and reconciles SQL.
    if not active:
        connection.execute(
            db.index_generations.update()
            .where(db.index_generations.c.state == "active")
            .values(state="retired")
        )
        connection.execute(
            db.index_generations.update()
            .where(db.index_generations.c.id == generation_id)
            .values(state="active", activated_at=datetime.now(UTC))
        )
    connection.commit()
    return {
        "state": "active",
        "generation": str(generation_id),
        "collection": name,
        "chunks": len(corpus),
        "documents": len({c["payload"]["parsed_hash"] for c in corpus}),
        "new_embeddings": len(missing),
        "cached_embeddings": len(texts) - len(missing),
        "upstream_refreshed": False,
        "validated": True,
        "lexical_version": VERSION,
    }


def _dense_search(
    engine: sa.Engine,
    encoder: Encoder,
    qdrant: QdrantClient,
    query: str,
    *,
    ticker: str | None = None,
    form: str | None = None,
    accession: str | None = None,
    section: str | None = None,
    accepted_before: datetime | None = None,
    limit: int = 5,
    generation_id: UUID | None = None,
    allowed_ids: list[str] | None = None,
) -> dict[str, Any]:
    if not query.strip() or not 1 <= limit <= 40:
        raise ValueError("Invalid query or result limit")
    if accepted_before is not None and accepted_before.tzinfo is None:
        raise ValueError("Acceptance cutoff requires a timezone")
    with engine.connect() as connection:
        generation = (
            connection.execute(
                sa.select(db.index_generations).where(
                    db.index_generations.c.id == generation_id
                    if generation_id is not None
                    else db.index_generations.c.state == "active"
                )
            )
            .mappings()
            .one_or_none()
        )
        if generation is None:
            return {"status": "UNAVAILABLE", "reason": "No active generation", "results": []}
        if (
            generation["model_key"] != encoder.key
            or alias_target(qdrant) != generation["collection_name"]
        ):
            raise ValueError("Index activation/model mismatch; rebuild or replay required")
        conditions: list[models.Condition] = [
            models.FieldCondition(key=key, match=models.MatchValue(value=value))
            for key, value in {
                "tickers": ticker,
                "filing_type": form,
                "accession_number": accession,
                "section": section,
            }.items()
            if value is not None
        ]
        if accepted_before:
            conditions.append(
                models.FieldCondition(
                    key="accepted_at", range=models.DatetimeRange(lte=accepted_before)
                )
            )
        if allowed_ids is not None:
            conditions.append(models.HasIdCondition(has_id=list(allowed_ids)))
        vector = encoder.encode([query])[0]
        hits = qdrant.query_points(
            generation["collection_name"],
            query=vector,
            using="text",
            query_filter=models.Filter(must=conditions),
            limit=limit,
            with_payload=True,
        ).points
        results = []
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
                        db.generation_chunks.c.generation_id == generation["id"],
                        db.document_chunks.c.id == UUID(str(hit.id)),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                canonical is None
                or canonical["payload"] != hit.payload
                or digest(canonical["text"]) != canonical["content_hash"]
            ):
                raise ValueError("Retrieved evidence checksum mismatch")
            results.append({"score": hit.score, "text": canonical["text"], **canonical["payload"]})
        return {
            "status": "AVAILABLE" if results else "UNAVAILABLE",
            "scope": "indexed filings only",
            "upstream_refreshed": False,
            "generation": str(generation["id"]),
            "results": results,
        }


def search(
    engine: sa.Engine,
    encoder: Encoder | None,
    qdrant: QdrantClient,
    query: str,
    *,
    ticker: str | None = None,
    form: str | None = None,
    accession: str | None = None,
    section: str | None = None,
    accepted_before: datetime | None = None,
    limit: int = 5,
    mode: str = "dense",
    min_dense_score: float | None = None,
) -> dict[str, Any]:
    from app.indexing.retrieval import retrieve

    return retrieve(
        engine,
        encoder,
        qdrant,
        query,
        ticker=ticker,
        form=form,
        accession=accession,
        section=section,
        accepted_before=accepted_before,
        limit=limit,
        mode=mode,
        min_dense_score=min_dense_score,
    )
