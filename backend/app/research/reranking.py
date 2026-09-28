"""Pinned, offline cross-encoder scoring; base retrieval scores remain unchanged."""

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any, Protocol

from app.core.config import Settings

MODEL = "cross-encoder/ms-marco-MiniLM-L6-v2"
REVISION = "fbf9045f293a58fa68636213c5e0cb8a2de5d45e"
LIMIT = 40


class Reranker(Protocol):
    key: str

    def score(self, question: str, texts: list[str]) -> list[float]: ...


def checksums(path: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.iterdir())
        if p.is_file() and p.name != "manifest.json"
    }


def provision(settings: Settings) -> dict[str, Any]:
    from huggingface_hub import snapshot_download

    path = settings.embedding_cache_path / "reranker" / REVISION
    snapshot_download(
        MODEL,
        revision=REVISION,
        local_dir=path,
        allow_patterns=["*.json", "*.txt", "*.safetensors", "README.md"],
    )
    manifest = {
        "model": MODEL,
        "revision": REVISION,
        "license": "apache-2.0",
        "output_dimension": 1,
        "files": checksums(path),
    }
    (path / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


class LocalReranker:
    key = f"{MODEL}@{REVISION}:cpu-raw-logit-v1"

    def __init__(self, settings: Settings):
        from sentence_transformers import CrossEncoder
        from torch import nn

        path = settings.embedding_cache_path / "reranker" / REVISION
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if (
            manifest.get("model") != MODEL
            or manifest.get("revision") != REVISION
            or manifest.get("files") != checksums(path)
            or "model.safetensors" not in manifest["files"]
        ):
            raise ValueError("Reranker provisioning manifest mismatch")
        self.model = CrossEncoder(
            str(path),
            device="cpu",
            local_files_only=True,
            trust_remote_code=False,
            model_kwargs={"use_safetensors": True},
            activation_fn=nn.Identity(),
            max_length=512,
        )
        self.batch_size = settings.reranker_batch_size

    def score(self, question: str, texts: list[str]) -> list[float]:
        if not texts or len(texts) > LIMIT:
            raise ValueError("Reranker requires 1 to 40 candidates")
        pairs = [(question, text) for text in texts]
        encoded = self.model.tokenizer(
            [question] * len(texts), texts, truncation=False, padding=False
        )
        if any(len(ids) > 512 for ids in encoded["input_ids"]):
            raise ValueError("Reranker pair exceeds 512 tokens; truncation is forbidden")
        return [
            float(v)
            for v in self.model.predict(pairs, batch_size=self.batch_size, show_progress_bar=False)
        ]


def rerank(question: str, retrieved: dict[str, Any], scorer: Reranker) -> dict[str, Any]:
    started = time.perf_counter()
    hits = retrieved.get("results", [])[:LIMIT]
    scores = scorer.score(question, [h["text"] for h in hits]) if hits else []
    if len(scores) != len(hits) or any(not math.isfinite(s) for s in scores):
        raise ValueError("Invalid reranker scores")
    ranked = [
        {**hit, "retrieval_rank": i + 1, "rerank_score": score}
        for i, (hit, score) in enumerate(zip(hits, scores, strict=True))
    ]
    ranked.sort(key=lambda h: (-h["rerank_score"], h["retrieval_rank"]))
    for rank, hit in enumerate(ranked, 1):
        hit["rerank_rank"] = rank
    return {
        **retrieved,
        "results": ranked,
        "reranking": {
            "status": "applied" if hits else "no_candidates",
            "model_key": scorer.key,
            "candidate_limit": LIMIT,
            "candidate_count": len(hits),
            "score_kind": "raw_logit_not_probability",
            "candidates": [
                {k: h[k] for k in ("chunk_id", "retrieval_rank", "rerank_rank", "rerank_score")}
                for h in ranked
            ],
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        },
    }
