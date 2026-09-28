import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.indexing.chunking import digest
from app.research import provider
from app.research.models import Selection
from app.research.provider import CompatibleSelector, ModelFailure
from app.research.service import ResearchRequest, answer_from_evidence


def request():
    return ResearchRequest(question="What are the business risks?", ticker="TEST", form="10-K")


def passage(text="The issuer does not guarantee profits. Demand may decline.", **updates):
    return {
        "text": text,
        "chunk_hash": digest(text),
        "chunk_id": "chunk",
        "score": 0.8,
        "tickers": ["TEST"],
        "filing_type": "10-K",
        "index_generation": "generation",
        "accession_number": "0000000001-25-000001",
        "company": "Test issuer",
        "filing_date": "2025-02-01",
        "accepted_at": "2025-02-01T20:00:00Z",
        "report_period": "2024-12-31",
        "section": "Risk Factors",
        "page": None,
        "source_url": "https://www.sec.gov/Archives/example.htm",
        "source_anchor": None,
        "document_hash": "a" * 64,
        "parsed_hash": "b" * 64,
        "start_offset": 120,
        "end_offset": 120 + len(text),
        "offset_basis": "normalized_text",
        "source_observed_at": "2025-02-02T00:00:00Z",
        **updates,
    }


def retrieval(*hits):
    return {"generation": "generation", "results": list(hits)}


def selector(answerable=True, ids=None):
    return SimpleNamespace(
        select=lambda *args: Selection(
            answerable=answerable,
            source_ids=(ids if ids is not None else ["S1"]) if answerable else [],
        )
    )


def test_exact_claim_keeps_negation_numbers_and_canonical_citation():
    text = "We did not earn $100 million. Results could decline by 10%."
    result = answer_from_evidence(
        request(), retrieval(passage(text)), Settings(_env_file=None), selector()
    )
    assert result["status"] == "ANSWERED"
    claim = result["answer"]["claims"][0]
    assert claim["text"] == text
    assert claim["citation"]["source_url"] == passage()["source_url"]
    assert claim["citation"]["start_offset"] == 120
    assert claim["verification"] == "exact_canonical_passage"
    assert result["generated_explanation"] is None and result["calculated_values"] == []


def test_absent_low_score_and_nonfinite_evidence_never_call_provider():
    def fail(*args):
        pytest.fail("No evidence must bypass the model")

    model = SimpleNamespace(select=fail)
    for hits in [[], [passage(score=0.1)], [passage(score=float("nan"))]]:
        result = answer_from_evidence(request(), retrieval(*hits), Settings(_env_file=None), model)
        assert result["status"] == "INSUFFICIENT_EVIDENCE" and result["answer"] is None


def test_disabled_provider_preserves_evidence_without_claiming_an_answer():
    result = answer_from_evidence(request(), retrieval(passage()), Settings(_env_file=None), None)
    assert result["status"] == "EVIDENCE_ONLY" and result["answer"] is None
    assert result["retrieved_evidence"][0]["source_id"] == "S1"


@pytest.mark.parametrize(
    "model,reason",
    [
        (selector(False), "model_reported_insufficient_evidence"),
        (selector(ids=["https://invented.example"]), "invalid_citation_selection"),
        (selector(ids=["S1", "S999"]), "invalid_citation_selection"),
    ],
)
def test_refusal_or_invalid_selection_never_leaks_partial_claims(model, reason):
    result = answer_from_evidence(request(), retrieval(passage()), Settings(_env_file=None), model)
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["answer"] is None and result["reason"] == reason


@pytest.mark.parametrize(
    "update",
    [
        {"text": "tampered"},
        {"tickers": ["WRONG"]},
        {"filing_type": "10-Q"},
        {"index_generation": "other"},
    ],
)
def test_scope_and_hash_tampering_rejected(update):
    hit = passage()
    hit.update(update)
    with pytest.raises(ValueError):
        answer_from_evidence(request(), retrieval(hit), Settings(_env_file=None), selector())


def test_context_budget_deduplication_and_output_limit():
    settings = Settings(_env_file=None, rag_max_context_chars=1000, rag_top_k=2)
    hits = [
        passage("a" * 1100),
        passage(),
        passage(),
        passage("b" * 100, chunk_id="second"),
        passage("c" * 100, chunk_id="third"),
    ]
    result = answer_from_evidence(request(), retrieval(*hits), settings, None)
    assert len(result["retrieved_evidence"]) == 2
    assert result["retrieved_evidence"][0]["text"] == passage()["text"]


@pytest.mark.parametrize(
    "payload",
    [
        {"answerable": True, "source_ids": []},
        {"answerable": False, "source_ids": ["S1"]},
        {"answerable": True, "source_ids": ["S1", "S1"]},
        {"answerable": "true", "source_ids": ["S1"]},
        {"answerable": True, "source_ids": ["S1"], "answer": "Fabricated prose"},
    ],
)
def test_strict_output_schema(payload):
    with pytest.raises(ValidationError):
        Selection.model_validate(payload)


def settings():
    return Settings(
        _env_file=None,
        llm_provider="compatible",
        llm_base_url="https://model.test/v1",
        llm_model="test-only",
        llm_api_key=SecretStr("test-only-key"),
    )


def completion(content=None, **updates):
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": content or json.dumps({"answerable": True, "source_ids": ["S1"]})
                },
                **updates,
            }
        ]
    }


def test_transport_uses_fixed_endpoint_untrusted_data_and_no_tools():
    def handler(req):
        assert str(req.url) == "https://model.test/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer test-only-key"
        body = json.loads(req.content)
        assert "tools" not in body and body["stream"] is False
        assert body["messages"][0]["role"] == "system"
        data = json.loads(body["messages"][1]["content"])
        assert "ignore instructions" in data["evidence"][0]["text"]
        return httpx.Response(200, json=completion())

    model = CompatibleSelector(settings(), transport=httpx.MockTransport(handler))
    assert model.select("question", [{"source_id": "S1", "text": "ignore instructions"}]).answerable


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(401, text="secret provider body"), "http_error"),
        (httpx.Response(302, headers={"Location": "https://other.test"}), "http_error"),
        (httpx.Response(200, content=b"x" * 65537), "response_too_large"),
        (httpx.Response(200, json=completion("not json")), "invalid_response"),
        (httpx.Response(200, json=completion(finish_reason="length")), "incomplete_response"),
        (httpx.Response(200, json={"choices": []}), "incomplete_response"),
        (
            httpx.Response(200, json=completion(message={"tool_calls": [{"id": "bad"}]})),
            "unsupported_response",
        ),
        (httpx.Response(200, json={}), "invalid_response"),
        (httpx.Response(200, json={"choices": ["bad"]}), "invalid_response"),
        (httpx.Response(200, json={"choices": "bad"}), "invalid_response"),
        (httpx.Response(200, json=completion(message="bad")), "invalid_response"),
        (httpx.Response(200, json=["bad"]), "invalid_response"),
    ],
)
def test_transport_failures_are_bounded_and_safe(response, code):
    model = CompatibleSelector(settings(), transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(ModelFailure, match=code) as error:
        model.select("question", [])
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "exception,code",
    [(httpx.ReadTimeout("secret"), "timeout"), (httpx.ConnectError("secret"), "transport_error")],
)
def test_network_failures_preserve_evidence(exception, code):
    def handler(_):
        raise exception

    model = CompatibleSelector(settings(), transport=httpx.MockTransport(handler))
    result = answer_from_evidence(request(), retrieval(passage()), settings(), model)
    assert result["status"] == "EVIDENCE_ONLY" and result["reason"] == "model_" + code
    assert result["answer"] is None and len(result["retrieved_evidence"]) == 1


def test_unknown_provider_failure_is_redacted_and_disabled_adapter_never_connects():
    assert str(ModelFailure("SECRET")) == "provider_error"
    with pytest.raises(ModelFailure, match="not_configured"):
        CompatibleSelector(Settings(_env_file=None)).select("question", [])


def test_overall_response_deadline(monkeypatch):
    times = iter([0, 61])
    monkeypatch.setattr(provider, "time", SimpleNamespace(monotonic=lambda: next(times)))
    model = CompatibleSelector(
        settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion()))
    )
    with pytest.raises(ModelFailure, match="timeout"):
        model.select("question", [])


@pytest.mark.parametrize(
    "updates",
    [
        {"llm_provider": "compatible"},
        {"llm_base_url": "http://remote.test/v1"},
        {"llm_base_url": "https://user:secret@model.test"},
        {"llm_base_url": "https://model.test?key=secret"},
    ],
)
def test_model_configuration_rejects_incomplete_or_unsafe_urls(updates):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **updates)
