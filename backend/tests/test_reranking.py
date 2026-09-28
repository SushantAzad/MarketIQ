import json
import sys
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.research.reranking import (
    MODEL,
    REVISION,
    LocalReranker,
    checksums,
    provision,
    rerank,
)


def test_bounded_stable_ranking_preserves_original_evidence():
    hits = [{"text": str(i), "score": 0.1, "chunk_id": str(i)} for i in range(50)]

    def score(question, texts):
        assert question == "query" and len(texts) == 40
        return [0.0, 2.0, 2.0] + [-1.0] * 37

    result = rerank("query", {"results": hits}, SimpleNamespace(key="test", score=score))
    assert [h["chunk_id"] for h in result["results"][:3]] == ["1", "2", "0"]
    assert result["results"][0]["score"] == 0.1
    assert "rerank_score" not in hits[1]
    assert result["reranking"]["candidate_count"] == 40


@pytest.mark.parametrize("scores", [[], [float("nan")], [float("inf")], [1, 2]])
def test_invalid_scores_fail(scores):
    with pytest.raises(ValueError):
        rerank(
            "q", {"results": [{"text": "t"}]}, SimpleNamespace(key="test", score=lambda *_: scores)
        )


def test_empty_skips_model():
    assert rerank("q", {}, SimpleNamespace(key="test"))["reranking"]["status"] == "no_candidates"


def test_offline_loading_integrity_and_no_truncation(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, embedding_cache_path=tmp_path)
    path = tmp_path / "reranker" / REVISION
    path.mkdir(parents=True)
    (path / "model.safetensors").write_bytes(b"test-only")
    monkeypatch.setitem(
        sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=lambda *a, **kw: None)
    )
    manifest = provision(settings)
    assert manifest["files"] == checksums(path)
    captured = {}

    def factory(*a, **kw):
        captured.update(kw)
        return SimpleNamespace(
            tokenizer=lambda *a, **kw: {"input_ids": [[0] * 512]}, predict=lambda *a, **kw: [-3.0]
        )

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=factory))
    model = LocalReranker(settings)
    assert captured["local_files_only"] and not captured["trust_remote_code"]
    assert model.score("q", ["t"]) == [-3.0]
    model.model.tokenizer = lambda *a, **kw: {"input_ids": [[0] * 513]}
    with pytest.raises(ValueError, match="truncation"):
        model.score("q", ["t"])
    for texts in [[], ["t"] * 41]:
        with pytest.raises(ValueError):
            model.score("q", texts)
    (path / "model.safetensors").write_bytes(b"changed")
    with pytest.raises(ValueError, match="manifest"):
        LocalReranker(settings)
    assert json.loads((path / "manifest.json").read_text())["model"] == MODEL
