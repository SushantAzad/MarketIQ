from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from test_filing_index import corpus as corpus
from test_filing_index import qdrant as qdrant
from test_index_chunking import TestEncoder

from app.database import schema as db
from app.indexing import index, lexical

pytestmark = pytest.mark.integration


def test_shared_scope_filters_and_bm25_without_embedding_calls(pg, qdrant, corpus):
    settings, _, filing = corpus
    encoder = TestEncoder()
    published = index.publish(pg, settings, encoder, qdrant)
    assert published["lexical_version"] == lexical.VERSION
    with pg.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(db.lexical_chunks))
            == published["chunks"]
        )
    for mode in ["dense", "bm25", "hybrid"]:
        actual_encoder = None if mode == "bm25" else encoder
        result = index.search(
            pg,
            actual_encoder,
            qdrant,
            "credit risk",
            ticker="TEST",
            form="10-K",
            accession=filing.accession_number,
            mode=mode,
        )
        assert result["results"] and result["retrieval"]["scoped_chunks"] == published["chunks"]
        assert all(r["accession_number"] == filing.accession_number for r in result["results"])
        for filter_args in [
            {"ticker": "WRONG"},
            {"form": "10-Q"},
            {"section": "not present"},
            {"accession": "0000000001-25-999999"},
            {"accepted_before": datetime(2000, 1, 1, tzinfo=UTC)},
        ]:
            assert (
                index.search(pg, actual_encoder, qdrant, "credit", mode=mode, **filter_args)[
                    "results"
                ]
                == []
            )
    sparse = index.search(pg, None, qdrant, "credit", mode="bm25")
    assert sparse["results"][0]["score_kind"] == "bm25"
    assert index.search(pg, None, qdrant, "xyzunmatched", mode="bm25")["results"] == []

    class OrthogonalQueryEncoder(TestEncoder):
        def encode(self, texts):
            return [[0.0, 0.0, 1.0] for _ in texts]

    hybrid = index.search(
        pg, OrthogonalQueryEncoder(), qdrant, "credit", mode="hybrid", min_dense_score=0.3
    )
    assert hybrid["results"] and hybrid["retrieval"]["dense_candidates"] == 0
    assert all(r["dense_score"] is None and r["bm25_score"] > 0 for r in hybrid["results"])
    dense_only = index.search(pg, encoder, qdrant, "xyzunmatched", mode="hybrid")
    assert dense_only["results"] and dense_only["retrieval"]["bm25_candidates"] == 0


def test_lexical_failure_cannot_activate_partial_generation_and_recovers(
    pg, qdrant, corpus, monkeypatch
):
    settings, _, _ = corpus
    encoder = TestEncoder()
    first = index.publish(pg, settings, encoder, qdrant)
    changed = settings.model_copy(update={"chunk_tokens": 21})
    original = index.publish_lexical

    def fail(*args):
        raise RuntimeError("lexical build failed")

    monkeypatch.setattr(index, "publish_lexical", fail)
    with pytest.raises(RuntimeError):
        index.publish(pg, changed, encoder, qdrant)
    assert index.alias_target(qdrant) == first["collection"]
    assert (
        index.search(pg, encoder, qdrant, "credit", mode="hybrid")["generation"]
        == first["generation"]
    )
    monkeypatch.setattr(index, "publish_lexical", original)
    repaired = index.publish(pg, changed, encoder, qdrant)
    assert repaired["generation"] != first["generation"] and repaired["new_embeddings"] == 0


def test_corrupt_or_missing_lexical_rows_fail_closed_then_rebuild(pg, qdrant, corpus):
    settings, _, _ = corpus
    encoder = TestEncoder()
    first = index.publish(pg, settings, encoder, qdrant)
    with pg.begin() as connection:
        connection.execute(db.lexical_chunks.update().values(terms={"fabricated": 99}))
    with pytest.raises(ValueError, match="checksum"):
        index.search(pg, encoder, qdrant, "credit", mode="hybrid")
    with pytest.raises(ValueError, match="integrity"):
        index.publish(pg, settings, encoder, qdrant)
    rebuilt = index.publish(pg, settings, encoder, qdrant, rebuild=True, from_canonical=True)
    assert rebuilt["new_embeddings"] == 0
    assert index.search(pg, encoder, qdrant, "credit", mode="hybrid")["results"]
    with pg.begin() as connection:
        connection.execute(
            db.lexical_chunks.delete().where(
                db.lexical_chunks.c.generation_id == rebuilt["generation"]
            )
        )
    with pytest.raises(ValueError, match="count"):
        index.search(pg, encoder, qdrant, "credit", mode="hybrid")
    assert qdrant.collection_exists(first["collection"])


def test_legacy_generation_requires_new_validated_lexical_generation(pg, qdrant, corpus):
    settings, _, _ = corpus
    encoder = TestEncoder()
    first = index.publish(pg, settings, encoder, qdrant, rebuild=True)
    with pg.begin() as connection:
        connection.execute(db.lexical_chunks.delete())
        connection.execute(db.lexical_generations.delete())
    with pytest.raises(ValueError, match="unavailable"):
        index.search(pg, encoder, qdrant, "credit", mode="hybrid")
    assert index.search(pg, encoder, qdrant, "credit", mode="dense")["results"]
    upgraded = index.publish(pg, settings, encoder, qdrant)
    assert upgraded["generation"] != first["generation"]
    assert upgraded["new_embeddings"] == 0
    assert index.search(pg, encoder, qdrant, "credit", mode="hybrid")["results"]


@pytest.mark.parametrize(
    "kwargs", [{"mode": "wrong"}, {"min_dense_score": float("nan")}, {"min_dense_score": 2.0}]
)
def test_invalid_hybrid_parameters(pg, qdrant, kwargs):
    with pytest.raises(ValueError):
        index.search(pg, TestEncoder(), qdrant, "risk", **kwargs)


def test_dense_and_sparse_share_identical_nonempty_scope(pg, qdrant, corpus, monkeypatch):
    settings, _, _ = corpus
    original = index.import_corpus

    def split_scope(*args):
        documents = original(*args)
        for i, document in enumerate(documents):
            if i % 2:
                document["payload"].update(
                    tickers=["OTHER"],
                    filing_type="10-Q",
                    accepted_at="2026-02-01T00:00:00Z",
                    accession_number="0000000001-26-000099",
                    section="Other section",
                )
        return documents

    monkeypatch.setattr(index, "import_corpus", split_scope)
    encoder = TestEncoder()
    index.publish(pg, settings, encoder, qdrant)
    for scope in [
        {"ticker": "TEST"},
        {"form": "10-K"},
        {"section": "Item 1A. Risk Factors"},
        {"accession": "0000000001-25-000001"},
        {"accepted_before": datetime(2025, 12, 31, tzinfo=UTC)},
    ]:
        for mode in ["dense", "bm25", "hybrid"]:
            result = index.search(pg, encoder, qdrant, "credit", mode=mode, **scope)
            assert result["results"] and all(r["tickers"] == ["TEST"] for r in result["results"])


def test_publication_during_retrieval_never_mixes_generations(pg, qdrant, corpus, monkeypatch):
    settings, _, _ = corpus
    encoder = TestEncoder()
    first = index.publish(pg, settings, encoder, qdrant)
    original = index._dense_search

    def switch_then_retrieve(*args, **kwargs):
        index.publish(pg, settings, encoder, qdrant, rebuild=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(index, "_dense_search", switch_then_retrieve)
    with pytest.raises(ValueError, match="activation"):
        index.search(pg, encoder, qdrant, "credit", mode="hybrid")
    monkeypatch.setattr(index, "_dense_search", original)
    result = index.search(pg, encoder, qdrant, "credit", mode="hybrid")
    assert result["generation"] != first["generation"]
    assert all(h["index_generation"] == result["generation"] for h in result["results"])
