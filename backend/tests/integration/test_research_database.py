import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from test_filing_index import corpus as corpus
from test_filing_index import qdrant as qdrant
from test_index_chunking import TestEncoder

from app.database import schema as db
from app.indexing import index
from app.research import __main__ as cli
from app.research.models import Selection
from app.research.provider import CompatibleSelector
from app.research.service import ResearchRequest, read_run, research

pytestmark = pytest.mark.integration


def test_claim_traces_to_real_services_and_stored_canonical_passage(pg, qdrant, corpus):
    settings, _, filing = corpus
    settings = settings.model_copy(
        update={
            "llm_provider": "compatible",
            "llm_model": "test-only",
            "llm_base_url": "https://model.test/v1",
        }
    )
    encoder = TestEncoder()
    built = index.publish(pg, settings, encoder, qdrant)

    def respond(req):
        evidence = json.loads(json.loads(req.content)["messages"][1]["content"])["evidence"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"answerable": True, "source_ids": [evidence[0]["source_id"]]}
                            )
                        },
                    }
                ]
            },
        )

    selector = CompatibleSelector(settings, transport=httpx.MockTransport(respond))
    request = ResearchRequest(question="What risks are disclosed?", ticker="TEST", form="10-K")
    result = research(pg, encoder, qdrant, settings, request, selector=selector)
    assert result["status"] == "ANSWERED"
    assert result["scope"]["accession"] == filing.accession_number
    assert result["generation"] == built["generation"]
    run_id = UUID(result["run_id"])
    assert read_run(pg, run_id) == result
    with pg.connect() as connection:
        citation = (
            connection.execute(
                sa.select(db.research_citations).where(db.research_citations.c.run_id == run_id)
            )
            .mappings()
            .one()
        )
        text = connection.execute(
            sa.select(db.document_chunks.c.text).where(
                db.document_chunks.c.id == citation["chunk_id"]
            )
        ).scalar_one()
        assert text == citation["exact_quote"] == result["answer"]["claims"][0]["text"]
        assert citation["generation_id"] == UUID(result["generation"])
        row = (
            connection.execute(sa.select(db.research_runs).where(db.research_runs.c.id == run_id))
            .mappings()
            .one()
        )
        assert "question" not in row and row["question_hash"]
        assert "api_key" not in json.dumps(dict(row), default=str)
    assert read_run(pg, uuid4()) is None


def test_refusal_invalid_citations_and_disabled_model_are_auditable(pg, qdrant, corpus):
    settings, _, _ = corpus
    encoder = TestEncoder()
    index.publish(pg, settings, encoder, qdrant)
    request = ResearchRequest(question="What risks are disclosed?", ticker="TEST", form="10-K")
    for selection, status in [
        (None, "EVIDENCE_ONLY"),
        (Selection(answerable=False, source_ids=[]), "INSUFFICIENT_EVIDENCE"),
        (Selection(answerable=True, source_ids=["S999"]), "INSUFFICIENT_EVIDENCE"),
    ]:
        selector = SimpleNamespace(select=lambda *_, s=selection: s) if selection else None
        result = research(pg, encoder, qdrant, settings, request, selector=selector)
        assert result["status"] == status and result["answer"] is None
        assert result["retrieved_evidence"]
        with pg.connect() as connection:
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(db.research_citations))
                == 0
            )


def test_latest_gap_and_missing_company_never_generate_answer(pg, qdrant, corpus):
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
    model = SimpleNamespace(select=lambda *_: pytest.fail("Provider must not be called"))
    request = ResearchRequest(question="What risks are disclosed?", ticker="TEST", form="10-K")
    result = research(pg, encoder, qdrant, settings, request, selector=model)
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["scope"]["accession"] == newer.accession_number and result["answer"] is None
    unknown = request.model_copy(update={"ticker": "UNKNOWN"})
    result = research(pg, encoder, qdrant, settings, unknown, selector=model)
    assert result["reason"] == "no_discovered_filing"


def test_research_cli_ask_show_status_and_errors(pg, qdrant, corpus, monkeypatch, capsys):
    settings, _, _ = corpus
    encoder = TestEncoder()
    index.publish(pg, settings, encoder, qdrant)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "database_engine", lambda _: pg)
    monkeypatch.setattr(cli, "SentenceEncoder", lambda _: encoder)
    monkeypatch.setattr(cli, "client", lambda _: qdrant)
    monkeypatch.setattr(qdrant, "close", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["research", "ask", "What risks are disclosed?", "--ticker", "test", "--form", "10-K"],
    )
    assert cli.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "EVIDENCE_ONLY"
    for args in [["show", output["run_id"]], ["status"]]:
        monkeypatch.setattr(sys, "argv", ["research", *args])
        assert cli.main() == 0
        capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["research", "show", str(uuid4())])
    assert cli.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "NOT_FOUND"
    monkeypatch.setattr(cli, "Settings", lambda: (_ for _ in ()).throw(ValueError("SECRET")))
    assert cli.main() == 1
    assert "SECRET" not in capsys.readouterr().out
