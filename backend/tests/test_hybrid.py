import math

import pytest
from test_research import passage, request, retrieval

from app.core.config import Settings
from app.indexing import lexical
from app.research.service import answer_from_evidence


def test_bm25_matches_hand_calculation_and_excludes_zero_overlap():
    docs = {"a": {"risk": 2, "credit": 1}, "b": {"credit": 1}, "c": {}}
    ranked = lexical.bm25("risk risk", docs)
    idf = math.log1p((3 - 1 + 0.5) / (1 + 0.5))
    expected = idf * 2 * 2.2 / (2 + 1.2 * (0.25 + 0.75 * 3 / (4 / 3)))
    assert ranked == [("a", pytest.approx(expected))]
    assert lexical.bm25("absent", docs) == []
    assert lexical.bm25("risk", {}) == []
    assert lexical.bm25("risk", {"a": {}}) == []
    assert lexical.bm25("the and", docs) == []


def test_tokenizer_is_versioned_normalized_and_preserves_negation_and_identifiers():
    assert lexical.terms("The Ｈ１００ H100 10-K 1.25 NOT guaranteed") == {
        "h100": 2,
        "10-k": 1,
        "1.25": 1,
        "not": 1,
        "guaranteed": 1,
    }
    assert lexical.VERSION.startswith("bm25-nfkc-v1")


def test_rrf_rank_math_deduplication_and_stable_ties():
    result = lexical.fuse(["a", "a", "b"], ["b", "c"])
    assert result == [
        ("b", pytest.approx(1 / 62 + 1 / 61)),
        ("a", pytest.approx(1 / 61)),
        ("c", pytest.approx(1 / 62)),
    ]
    assert lexical.fuse(["b"], ["a"]) == [("a", 1 / 61), ("b", 1 / 61)]
    assert lexical.fuse([], []) == []


@pytest.mark.parametrize(
    "kind,score,dense,bm25,kept",
    [
        ("rrf", 0.016, None, 2.0, True),
        ("rrf", 0.016, 0.7, None, True),
        ("rrf", 0.016, 0.1, None, False),
        ("rrf", 0.016, None, 0.0, False),
        ("bm25", 0.05, None, 0.05, True),
        ("bm25", 0.0, None, 0.0, False),
        ("cosine", 0.1, 0.1, None, False),
    ],
)
def test_research_never_applies_cosine_threshold_to_rrf(kind, score, dense, bm25, kept):
    result = answer_from_evidence(
        request(),
        retrieval(passage(score_kind=kind, score=score, dense_score=dense, bm25_score=bm25)),
        Settings(_env_file=None),
        None,
    )
    assert bool(result["retrieved_evidence"]) == kept


def test_research_rejects_unknown_score_semantics():
    with pytest.raises(ValueError, match="score kind"):
        answer_from_evidence(
            request(), retrieval(passage(score_kind="unknown")), Settings(_env_file=None), None
        )
