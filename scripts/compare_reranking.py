"""Record real model ordering and timing on Phase 6 queries without relevance claims."""

import json
import time
from datetime import UTC, datetime

from compare_retrieval import QUERIES

from app.core.config import REPOSITORY_ROOT, Settings
from app.database.connection import database_engine
from app.indexing.embeddings import SentenceEncoder
from app.indexing.index import client, search
from app.research.reranking import REVISION, LocalReranker, rerank


def main() -> None:
    settings = Settings()
    started = time.perf_counter()
    scorer = LocalReranker(settings)
    load_ms = round((time.perf_counter() - started) * 1000, 3)
    encoder = SentenceEncoder(settings)
    engine, qdrant = database_engine(settings), client(settings)
    rows = []
    try:
        for ticker, form, query in QUERIES:
            before = search(
                engine, encoder, qdrant, query, ticker=ticker, form=form, mode="hybrid", limit=40
            )
            after = rerank(query, before, scorer)
            assert after["reranking"]["status"] == "applied"
            rows.append(
                {
                    "query": query,
                    "ticker": ticker,
                    "form": form,
                    "generation": before["generation"],
                    "retrieval": before["retrieval"],
                    "reranking": after["reranking"],
                    "top5_changed": [h["chunk_id"] for h in before["results"][:5]]
                    != [h["chunk_id"] for h in after["results"][:5]],
                    "ranked_candidates": after["results"],
                }
            )
        assert len({r["generation"] for r in rows}) == 1
        manifest = json.loads(
            (settings.embedding_cache_path / "reranker" / REVISION / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        report = {
            "recorded_at": datetime.now(UTC).isoformat(),
            "model": manifest,
            "load_and_checksum_ms": load_ms,
            "human_relevance_labels": False,
            "quality_improvement_claimed": False,
            "timings": "Single CPU samples; first inference included, model loading separate",
            "comparisons": rows,
        }
        target = REPOSITORY_ROOT / "evaluation/phase-7-reranking.json"
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "queries": len(rows),
                    "top5_changed": sum(r["top5_changed"] for r in rows),
                    "load_ms": load_ms,
                    "inference_ms": [r["reranking"]["elapsed_ms"] for r in rows],
                }
            )
        )
    finally:
        engine.dispose()
        qdrant.close()


if __name__ == "__main__":
    main()
