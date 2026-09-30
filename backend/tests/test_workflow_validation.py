import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.indexing.chunking import digest
from app.workflow.validation import validate_document


def sample():
    generation, chunk = str(uuid4()), str(uuid4())
    text = "Test-only canonical filing passage."
    payload = {
        "chunk_id": chunk,
        "index_generation": generation,
        "tickers": ["TEST"],
        "filing_type": "10-K",
        "accession_number": "0000000001-25-000001",
        "chunk_hash": digest(text),
        "document_hash": "a" * 64,
        "source_url": "https://www.sec.gov/Archives/test.htm",
        "start_offset": 0,
        "end_offset": len(text),
    }
    hit = {**payload, "text": text, "source_id": "S1"}
    citation = {k: v for k, v in payload.items() if k != "tickers"}
    response = {
        "generation": generation,
        "retrieved_evidence": [hit],
        "scope": {"ticker": "TEST", "form": "10-K", "accession": payload["accession_number"]},
        "answer": {
            "kind": "extractive",
            "claims": [
                {"kind": "filing_quote", "text": text, "source_id": "S1", "citation": citation}
            ],
        },
    }
    canonical = {"text": text, "content_hash": digest(text), "payload": copy.deepcopy(payload)}
    connection = SimpleNamespace(
        execute=lambda *_: SimpleNamespace(
            mappings=lambda: SimpleNamespace(one_or_none=lambda: canonical)
        )
    )
    return connection, {"step": {"ticker": "TEST"}, "result": response}


def test_exact_canonical_citation_is_accepted():
    connection, row = sample()
    assert validate_document(connection, row)


@pytest.mark.parametrize(
    "tamper",
    [
        "text",
        "quote",
        "url",
        "hash",
        "source",
        "generation",
        "scope",
        "duplicate",
        "missing_citation",
        "explanation",
    ],
)
def test_tampered_evidence_or_claim_is_rejected(tamper):
    connection, row = sample()
    result = row["result"]
    hit = result["retrieved_evidence"][0]
    claim = result["answer"]["claims"][0]
    if tamper == "text":
        hit["text"] += " fabricated"
    elif tamper == "quote":
        claim["text"] += " fabricated"
    elif tamper == "url":
        claim["citation"]["source_url"] = "https://wrong.test"
    elif tamper == "hash":
        hit["chunk_hash"] = "b" * 64
    elif tamper == "source":
        claim["source_id"] = "S999"
    elif tamper == "generation":
        hit["index_generation"] = str(uuid4())
    elif tamper == "scope":
        result["scope"]["ticker"] = "OTHER"
    elif tamper == "duplicate":
        result["retrieved_evidence"].append(copy.deepcopy(hit))
    elif tamper == "missing_citation":
        del claim["citation"]["chunk_id"]
    else:
        result["generated_explanation"] = "Unsupported explanation"
    assert not validate_document(connection, row)
