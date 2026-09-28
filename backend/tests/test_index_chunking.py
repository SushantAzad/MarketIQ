import re

import pytest

from app.indexing.chunking import chunk_document, digest
from app.indexing.embeddings import validate_vectors


class TestEncoder:
    """Deliberately synthetic encoder, only used by automated tests."""

    __test__ = False
    key = digest("test-only-encoder")
    dimension = 3
    max_tokens = 32

    def __init__(self):
        self.encoded = 0

    def offsets(self, text):
        return [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]

    def encode(self, texts):
        self.encoded += len(texts)
        return [[0.6, 0.8, 0.0] for _ in texts]


def test_exact_offsets_section_boundaries_overlap_and_identity():
    text = "Introduction " * 40 + "\nItem 1A. Risk Factors\n" + "credit risk " * 50
    boundary = text.index("Item")
    parsed = {"text": text, "headings": [{"start": boundary, "heading": "Item 1A. Risk Factors"}]}
    version, chunks = chunk_document(parsed, TestEncoder(), 20, 4)
    assert len(chunks) > 5
    covered = set()
    for chunk in chunks:
        assert chunk["text"] == text[chunk["start_offset"] : chunk["end_offset"]]
        assert chunk["content_hash"] == digest(chunk["text"])
        assert 0 < chunk["token_count"] <= 20
        assert not chunk["start_offset"] < boundary < chunk["end_offset"]
        covered.update(range(chunk["start_offset"], chunk["end_offset"]))
    assert all(i in covered for i, char in enumerate(text) if not char.isspace())
    assert chunk_document(parsed, TestEncoder(), 20, 4) == (version, chunks)
    assert chunk_document(parsed, TestEncoder(), 21, 4)[0] != version


@pytest.mark.parametrize("budget,overlap", [(33, 1), (20, 20), (20, -1)])
def test_reject_invalid_budget(budget, overlap):
    with pytest.raises(ValueError):
        chunk_document({"text": "some text"}, TestEncoder(), budget, overlap)


@pytest.mark.parametrize(
    "parsed",
    [
        {"text": " "},
        {"text": "abc", "headings": [{"start": 4, "heading": "bad"}]},
        {"text": "abc", "headings": [{"start": 2, "heading": "a"}, {"start": 1, "heading": "b"}]},
    ],
)
def test_reject_bad_documents(parsed):
    with pytest.raises(ValueError):
        chunk_document(parsed, TestEncoder(), 20, 2)


@pytest.mark.parametrize(
    "vectors", [[], [[1, 0]], [[0, 0, 0]], [[float("nan"), 0, 1]], [[float("inf"), 0, 1]]]
)
def test_reject_invalid_vectors(vectors):
    with pytest.raises(ValueError):
        validate_vectors(vectors, 1, 3)
