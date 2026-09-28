import sys
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.indexing.embeddings import SentenceEncoder


def test_provider_is_offline_by_default_pinned_and_rejects_truncation(monkeypatch, tmp_path):
    calls = []

    class Tokenizer:
        def num_special_tokens_to_add(self, **kwargs):
            return 2

        def __call__(self, text, **kwargs):
            assert kwargs["truncation"] is False
            return {"offset_mapping": [(i, i + 1) for i in range(len(text))]}

    class Model:
        tokenizer = Tokenizer()
        max_seq_length = 6

        def __init__(self, name, **kwargs):
            calls.append((name, kwargs))

        def get_embedding_dimension(self):
            return 3

        def encode(self, texts, **kwargs):
            assert kwargs["normalize_embeddings"]
            return SimpleNamespace(tolist=lambda: [[0.6, 0.8, 0.0] for _ in texts])

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Model)
    )
    settings = Settings(_env_file=None, embedding_cache_path=tmp_path)
    encoder = SentenceEncoder(settings)
    assert calls[0][1]["local_files_only"] is True
    assert calls[0][1]["trust_remote_code"] is False
    assert calls[0][1]["revision"] == settings.embedding_revision
    assert encoder.dimension == 3 and encoder.max_tokens == 4
    assert encoder.encode(["abc"]) == [[0.6, 0.8, 0.0]]
    with pytest.raises(ValueError, match="budget"):
        encoder.encode(["abcde"])
    SentenceEncoder(settings, provision=True)
    assert calls[-1][1]["local_files_only"] is False
    monkeypatch.setattr(Model, "get_embedding_dimension", lambda _: None)
    with pytest.raises(ValueError, match="dimension"):
        SentenceEncoder(settings)
