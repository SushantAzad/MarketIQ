"""Versioned BM25 corpus statistics in PostgreSQL; no external search service."""

import json
import math
import re
import unicodedata
from collections import Counter
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from app.database import schema as db
from app.indexing.chunking import digest
from app.services.financial_import import put

VERSION = "bm25-nfkc-v1-k1-1.2-b-0.75"
RRF_VERSION = "rrf-v1-k60-window40"
CANDIDATES = 40
RRF_K = 60
STOP_WORDS = frozenset(
    "a an the and or of to in for on at by with is are was were be been "
    "this that these those what which how does do did as from".split()
)


def terms(text: str) -> dict[str, int]:
    tokens = re.findall(r"[^\W_]+(?:[.\-][^\W_]+)*", unicodedata.normalize("NFKC", text).casefold())
    return dict(sorted(Counter(t for t in tokens if t not in STOP_WORDS).items()))


def manifest_hash(records: list[dict[str, Any]]) -> str:
    return digest(json.dumps(records, sort_keys=True, separators=(",", ":"), default=str))


def publish_lexical(
    connection: sa.Connection,
    generation: UUID,
    corpus: list[dict[str, Any]],
    manifest: str,
) -> None:
    records = []
    for chunk in sorted(corpus, key=lambda c: str(c["id"])):
        counts = terms(chunk["text"])
        records.append(
            {
                "generation_id": generation,
                "chunk_id": chunk["id"],
                "content_hash": digest(chunk["text"]),
                "terms": counts,
                "length": sum(counts.values()),
            }
        )
    checksum = manifest_hash(records)
    put(
        connection,
        db.lexical_generations,
        [
            {
                "generation_id": generation,
                "version": VERSION,
                "corpus_manifest": manifest,
                "index_hash": checksum,
                "chunk_count": len(records),
            }
        ],
    )
    put(connection, db.lexical_chunks, records)
    stored = [
        dict(r)
        for r in connection.execute(
            sa.select(db.lexical_chunks)
            .where(db.lexical_chunks.c.generation_id == generation)
            .order_by(db.lexical_chunks.c.chunk_id)
        ).mappings()
    ]
    meta = (
        connection.execute(
            sa.select(db.lexical_generations).where(
                db.lexical_generations.c.generation_id == generation
            )
        )
        .mappings()
        .one()
    )
    if (
        stored != records
        or meta["version"] != VERSION
        or meta["corpus_manifest"] != manifest
        or meta["chunk_count"] != len(records)
        or meta["index_hash"] != checksum
    ):
        raise ValueError("Lexical generation integrity mismatch; rebuild required")


def bm25(query: str, documents: dict[str, dict[str, int]]) -> list[tuple[str, float]]:
    """Positive IDF BM25, using all documents in the permitted scope for statistics."""
    query_terms = set(terms(query))
    lengths = {key: sum(counts.values()) for key, counts in documents.items()}
    if not documents or not query_terms or not sum(lengths.values()):
        return []
    average = sum(lengths.values()) / len(documents)
    df = {term: sum(term in counts for counts in documents.values()) for term in query_terms}
    scored = []
    for key, counts in documents.items():
        score = 0.0
        for term in sorted(query_terms):
            frequency = counts.get(term, 0)
            if frequency:
                idf = math.log1p((len(documents) - df[term] + 0.5) / (df[term] + 0.5))
                score += (
                    idf
                    * frequency
                    * 2.2
                    / (frequency + 1.2 * (0.25 + 0.75 * lengths[key] / average))
                )
        if score > 0:
            scored.append((key, score))
    return sorted(scored, key=lambda row: (-row[1], row[0]))


def fuse(*rankings: list[str]) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        # Each retriever contributes at most once per chunk; ties have stable ID ordering upstream.
        for rank, key in enumerate(dict.fromkeys(ranking), start=1):
            scores[key] = scores.get(key, 0.0) + 1 / (RRF_K + rank)
    return sorted(scores.items(), key=lambda row: (-row[1], row[0]))
