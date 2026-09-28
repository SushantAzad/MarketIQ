import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient, models
from test_index_chunking import TestEncoder

from app.core.config import Settings
from app.database import schema as db
from app.indexing import __main__ as cli
from app.indexing import index
from app.indexing.corpus import import_corpus, latest_accession, read_object
from app.ingestion.parser import parse_document
from app.ingestion.store import IngestionStore
from app.providers.sec.models import Filing
from app.services.financial_import import put, stable_id

pytestmark = pytest.mark.integration


@pytest.fixture
def qdrant(monkeypatch):
    url = os.environ.get("MARKETIQ_TEST_QDRANT_URL")
    if not url:
        pytest.skip("Set MARKETIQ_TEST_QDRANT_URL for real Qdrant server tests")
    client = QdrantClient(url=url, timeout=30)
    alias = "miq_test_" + uuid4().hex
    monkeypatch.setattr(index, "ALIAS", alias)
    created = []
    original = client.create_collection
    close = client.close

    def create(name, **kwargs):
        result = original(name, **kwargs)
        created.append(name)
        return result

    monkeypatch.setattr(client, "create_collection", create)
    try:
        yield client
    finally:
        if index.alias_target(client):
            client.update_collection_aliases(
                [models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias))]
            )
        for name in created:
            client.delete_collection(name)
        close()


@pytest.fixture
def corpus(pg, tmp_path):
    settings = Settings(
        _env_file=None,
        sec_state_path=tmp_path / "sec",
        raw_storage_path=tmp_path / "raw",
        chunk_tokens=20,
        chunk_overlap=4,
    )
    store = IngestionStore(settings.sec_state_path, settings.raw_storage_path)
    company_id = uuid4()
    # Unique CIK makes test collection names independent of other simultaneous test runs.
    cik = str(company_id.int % 10**10).zfill(10)
    filing = Filing(
        cik=cik,
        accession_number="0000000001-25-000001",
        form_type="10-K",
        filing_date="2025-02-01",
        accepted_at="2025-02-01T20:00:00Z",
        report_period="2024-12-31",
        primary_document="annual.htm",
    )
    raw = ("<h1>Item 1A. Risk Factors</h1><p>" + "credit risk disclosure " * 60 + "</p>").encode()
    raw_hash = store.save(raw)
    store.fetched(filing.url, store.attempted(filing.url), 1, 1, digest=raw_hash)
    metadata_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    metadata_hash = store.save_json({"accessionNumber": [filing.accession_number]})
    store.fetched(metadata_url, store.attempted(metadata_url), 1, 1, digest=metadata_hash)
    registry_url = "https://www.sec.gov/files/company_tickers.json"
    registry_hash = store.save_json({"0": {"ticker": "TEST", "cik_str": int(cik)}})
    store.fetched(registry_url, store.attempted(registry_url), 1, 1, digest=registry_hash)
    parsed = {
        **parse_document(raw, filing.url),
        "filing": filing.model_dump(mode="json"),
        "raw_hash": raw_hash,
    }
    parsed_hash = store.save_json(parsed)
    store.discover(filing)
    store.stage(
        filing,
        "parsed",
        raw_hash=raw_hash,
        parsed_hash=parsed_hash,
        parser_version=parsed["parser_version"],
    )
    with pg.begin() as connection:
        put(connection, db.companies, [{"id": company_id, "cik": cik, "legal_name": "Test issuer"}])
        put(
            connection,
            db.securities,
            [{"id": uuid4(), "company_id": company_id, "ticker": "TEST", "provider": "test"}],
        )
        put(
            connection,
            db.source_objects,
            [
                {
                    "id": stable_id("source", metadata_url + ":" + metadata_hash),
                    "provider": "SEC EDGAR",
                    "source_url": metadata_url,
                    "content_hash": metadata_hash,
                    "object_key": metadata_hash,
                    "first_fetched_at": datetime.now(UTC),
                }
            ],
        )
    yield settings, store, filing
    store.close()


def test_insert_replay_filters_rebuild_and_canonical_evidence(pg, qdrant, corpus):
    settings, store, filing = corpus
    encoder = TestEncoder()
    first = index.publish(pg, settings, encoder, qdrant)
    assert first["new_embeddings"] > 0
    encoded = encoder.encoded
    replay = index.publish(pg, settings, encoder, qdrant)
    assert replay["new_embeddings"] == 0 and encoder.encoded == encoded
    assert replay["generation"] == first["generation"]
    hit = index.search(pg, encoder, qdrant, "risk", ticker="TEST", form="10-K")["results"][0]
    assert hit["source_url"] == filing.url and hit["page"] is None
    parsed = json.loads(store.read(store.filing_state(filing)["parsed_hash"]))
    assert hit["text"] == parsed["text"][hit["start_offset"] : hit["end_offset"]]
    for filters in [
        {"ticker": "OTHER"},
        {"form": "10-Q"},
        {"section": "missing"},
        {"accession": "0000000001-25-999999"},
        {"accepted_before": datetime(2024, 1, 1, tzinfo=UTC)},
    ]:
        assert index.search(pg, encoder, qdrant, "risk", **filters)["results"] == []
    assert index.search(pg, encoder, qdrant, "risk", section=hit["section"])["results"]
    encoded = encoder.encoded
    rebuilt = index.publish(pg, settings, encoder, qdrant, rebuild=True)
    assert rebuilt["generation"] != first["generation"]
    assert rebuilt["new_embeddings"] == 0 and encoder.encoded == encoded
    assert qdrant.count(first["collection"], exact=True).count == first["chunks"]
    assert index.alias_target(qdrant) == rebuilt["collection"]


def test_upsert_failure_preserves_active_and_replay_recovers(pg, qdrant, corpus, monkeypatch):
    settings, _, _ = corpus
    encoder = TestEncoder()
    first = index.publish(pg, settings, encoder, qdrant)
    changed = settings.model_copy(update={"chunk_tokens": 21})
    original = qdrant.upsert
    monkeypatch.setattr(
        qdrant, "upsert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("outage"))
    )
    with pytest.raises(RuntimeError):
        index.publish(pg, changed, encoder, qdrant)
    assert index.alias_target(qdrant) == first["collection"]
    assert index.search(pg, encoder, qdrant, "risk")["results"]
    monkeypatch.setattr(qdrant, "upsert", original)
    recovered = index.publish(pg, changed, encoder, qdrant)
    assert recovered["new_embeddings"] == 0
    assert recovered["generation"] != first["generation"]


def test_alias_crash_fails_closed_then_reconciles(pg, qdrant, corpus, monkeypatch):
    settings, _, _ = corpus
    encoder = TestEncoder()
    index.publish(pg, settings, encoder, qdrant)
    original = qdrant.update_collection_aliases

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Simulated crash after alias publication")

    monkeypatch.setattr(qdrant, "update_collection_aliases", crash)
    changed = settings.model_copy(update={"chunk_tokens": 21})
    with pytest.raises(RuntimeError):
        index.publish(pg, changed, encoder, qdrant)
    with pytest.raises(ValueError, match="activation"):
        index.search(pg, encoder, qdrant, "risk")
    monkeypatch.setattr(qdrant, "update_collection_aliases", original)
    assert index.publish(pg, changed, encoder, qdrant)["validated"]
    assert index.search(pg, encoder, qdrant, "risk")["results"]


def test_tampered_payload_rejected_and_rebuild_repairs(pg, qdrant, corpus):
    settings, _, _ = corpus
    encoder = TestEncoder()
    first = index.publish(pg, settings, encoder, qdrant)
    qdrant.set_payload(
        first["collection"],
        {"source_url": "https://invalid.test"},
        points=models.Filter(must=[]),
        wait=True,
    )
    with pytest.raises(ValueError, match="evidence"):
        index.search(pg, encoder, qdrant, "risk")
    with pytest.raises(ValueError, match="payload"):
        index.publish(pg, settings, encoder, qdrant)
    index.publish(pg, settings, encoder, qdrant, rebuild=True)
    assert index.search(pg, encoder, qdrant, "risk")["results"]


def test_vector_and_count_corruption_prevent_validation(pg, qdrant, corpus):
    settings, _, _ = corpus
    encoder = TestEncoder()
    first = index.publish(pg, settings, encoder, qdrant)
    point = qdrant.scroll(first["collection"], limit=1, with_payload=True)[0][0]
    qdrant.upsert(
        first["collection"],
        [models.PointStruct(id=point.id, vector={"text": [1.0, 0.0, 0.0]}, payload=point.payload)],
        wait=True,
    )
    with pytest.raises(ValueError, match="vector checksum"):
        index.publish(pg, settings, encoder, qdrant)
    qdrant.delete(
        first["collection"], points_selector=models.PointIdsList(points=[point.id]), wait=True
    )
    with pytest.raises(ValueError, match="count"):
        index.publish(pg, settings, encoder, qdrant)
    assert index.publish(pg, settings, encoder, qdrant, rebuild=True)["validated"]


def test_latest_discovered_unindexed_is_not_replaced_by_old(pg, qdrant, corpus):
    settings, store, filing = corpus
    encoder = TestEncoder()
    index.publish(pg, settings, encoder, qdrant)
    newer = filing.model_copy(
        update={
            "accession_number": "0000000001-26-000001",
            "accepted_at": datetime(2026, 2, 1, tzinfo=UTC),
        }
    )
    store.discover(newer)
    latest = latest_accession(settings, "TEST", "10-K")
    assert latest == newer.accession_number
    assert index.search(pg, encoder, qdrant, "risk", accession=latest)["results"] == []
    assert latest_accession(settings, "TEST", "10-Q") is None


def test_source_corruption_is_atomic_and_existing_index_survives(pg, qdrant, corpus):
    settings, store, filing = corpus
    encoder = TestEncoder()
    first = index.publish(pg, settings, encoder, qdrant)
    digest = store.filing_state(filing)["raw_hash"]
    (settings.raw_storage_path / digest[:2] / digest).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        index.publish(pg, settings, encoder, qdrant)
    assert index.alias_target(qdrant) == first["collection"]
    assert index.search(pg, encoder, qdrant, "risk")["results"]
    with pytest.raises(ValueError):
        read_object(settings, "../../bad")


def test_empty_corpus_invalid_model_vectors_and_search_arguments(pg, qdrant, corpus):
    settings, store, _ = corpus
    encoder = TestEncoder()
    assert index.search(pg, encoder, qdrant, "risk")["status"] == "UNAVAILABLE"
    for query, kwargs in [
        (" ", {}),
        ("risk", {"limit": 0}),
        ("risk", {"accepted_before": datetime(2025, 1, 1)}),
    ]:
        with pytest.raises(ValueError):
            index.search(pg, encoder, qdrant, query, **kwargs)
    store.db.execute("UPDATE filings SET stage='discovered'")
    store.db.commit()
    with pg.begin() as connection, pytest.raises(ValueError, match="No parsed"):
        import_corpus(connection, settings, encoder)


def test_canonical_recovery_without_raw_artifacts_or_journal(pg, qdrant, corpus, tmp_path):
    settings, _, _ = corpus
    encoder = TestEncoder()
    first = index.publish(pg, settings, encoder, qdrant)
    offline = settings.model_copy(
        update={"sec_state_path": tmp_path / "absent", "raw_storage_path": tmp_path / "absent-raw"}
    )
    with pytest.raises(ValueError, match="explicit rebuild"):
        index.publish(pg, offline, encoder, qdrant, from_canonical=True)
    # Real missing collection; canonical evidence and embedding cache must suffice.
    qdrant.delete_collection(first["collection"])
    recovered = index.publish(pg, offline, encoder, qdrant, rebuild=True, from_canonical=True)
    assert recovered["new_embeddings"] == 0 and recovered["chunks"] == first["chunks"]
    assert index.search(pg, encoder, qdrant, "risk")["results"]
    assert index.publish(pg, settings, encoder, qdrant)["generation"] == recovered["generation"]


def test_cli_build_status_search_latest_and_safe_errors(pg, qdrant, corpus, monkeypatch, capsys):
    settings, store, filing = corpus
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "database_engine", lambda _: pg)
    monkeypatch.setattr(cli, "SentenceEncoder", lambda *a, **k: TestEncoder())
    monkeypatch.setattr(cli, "client", lambda _: qdrant)
    monkeypatch.setattr(qdrant, "close", lambda: None)
    import sys

    for args in [
        ["provision"],
        ["build"],
        ["status"],
        ["search", "risk", "--ticker", "test", "--form", "10-K", "--latest"],
        ["search", "risk", "--ticker", "TEST", "--form", "10-Q", "--latest"],
    ]:
        monkeypatch.setattr(sys, "argv", ["index", *args])
        assert cli.main() == 0
        output = json.loads(capsys.readouterr().out)
        if args[0] == "status":
            assert output["consistent"]
        if args[0] == "search":
            assert output["status"] == ("AVAILABLE" if "10-K" in args else "UNAVAILABLE")
    store.discover(
        filing.model_copy(
            update={
                "accession_number": "0000000001-26-000001",
                "accepted_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        )
    )
    monkeypatch.setattr(
        sys, "argv", ["index", "search", "risk", "--ticker", "TEST", "--form", "10-K", "--latest"]
    )
    assert cli.main() == 0
    assert "no matching indexed" in json.loads(capsys.readouterr().out)["reason"]
    monkeypatch.setattr(cli, "Settings", lambda: (_ for _ in ()).throw(ValueError("SECRET")))
    assert cli.main() == 1
    assert "SECRET" not in capsys.readouterr().out
