"""Pinned CPU embeddings; only provision() may download model artifacts."""

import hashlib
import json
import math
from typing import Protocol

from app.core.config import Settings


class Encoder(Protocol):
    key: str
    dimension: int
    max_tokens: int

    def offsets(self, text: str) -> list[tuple[int, int]]: ...

    def encode(self, texts: list[str]) -> list[list[float]]: ...


def validate_vectors(vectors: list[list[float]], count: int, dimension: int) -> None:
    if len(vectors) != count or any(
        len(v) != dimension or not all(math.isfinite(x) for x in v) or sum(x * x for x in v) < 1e-12
        for v in vectors
    ):
        raise ValueError("Invalid embedding batch")


class SentenceEncoder:
    def __init__(self, settings: Settings, *, provision: bool = False) -> None:
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(
            settings.embedding_model,
            revision=settings.embedding_revision,
            cache_folder=str(settings.embedding_cache_path),
            device="cpu",
            local_files_only=not provision,
            trust_remote_code=False,
            model_kwargs={"use_safetensors": True},
        )
        dimension = self.model.get_embedding_dimension()
        if dimension is None:
            raise ValueError("Model does not declare embedding dimension")
        self.dimension = int(dimension)
        self.max_tokens = int(self.model.max_seq_length) - int(
            self.model.tokenizer.num_special_tokens_to_add(pair=False)
        )
        self.batch_size = settings.embedding_batch_size
        self.key = hashlib.sha256(
            json.dumps(
                {
                    "model": settings.embedding_model,
                    "revision": settings.embedding_revision,
                    "dimension": dimension,
                    "normalized": True,
                    "backend": "torch-cpu-v1",
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def offsets(self, text: str) -> list[tuple[int, int]]:
        encoded = self.model.tokenizer(
            text,
            add_special_tokens=False,
            truncation=False,
            return_offsets_mapping=True,
            verbose=False,
        )
        return [(int(a), int(b)) for a, b in encoded["offset_mapping"] if b > a]

    def encode(self, texts: list[str]) -> list[list[float]]:
        if any(len(self.offsets(text)) > self.max_tokens for text in texts):
            raise ValueError("Embedding input exceeds model token budget")
        result: list[list[float]] = self.model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        ).tolist()
        validate_vectors(result, len(texts), self.dimension)
        return result
