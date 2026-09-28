from types import SimpleNamespace
from uuid import UUID

import pytest
from test_filing_index import corpus as corpus
from test_filing_index import qdrant as qdrant
from test_index_chunking import TestEncoder

from app.indexing import index
from app.research.service import ResearchRequest, read_run, research

pytestmark = pytest.mark.integration


def test_reranking_and_failure_are_persisted_without_losing_sources(pg, qdrant, corpus):
    settings, _, _ = corpus
    settings = settings.model_copy(update={"reranker_enabled": True})
    encoder = TestEncoder()
    index.publish(pg, settings, encoder, qdrant)
    request = ResearchRequest(question="What risks are disclosed?", ticker="TEST", form="10-K")
    scorer = SimpleNamespace(key="test-only", score=lambda q, texts: list(range(len(texts))))
    result = research(pg, encoder, qdrant, settings, request, reranker=scorer)
    assert result["reranking"]["status"] == "applied"
    assert result["retrieved_evidence"][0]["rerank_rank"] == 1
    assert read_run(pg, UUID(result["run_id"])) == result

    def fail(*_):
        raise RuntimeError("test failure")

    scorer.score = fail
    selector = SimpleNamespace(select=lambda *_: pytest.fail("must not generate on failure"))
    result = research(pg, encoder, qdrant, settings, request, reranker=scorer, selector=selector)
    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason"] == "reranker_failed"
    assert result["reranking"]["status"] == "failed"
    assert "rerank_score" not in result["retrieved_evidence"][0]
    assert read_run(pg, UUID(result["run_id"])) == result
