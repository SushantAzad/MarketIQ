"""Record real-corpus dense/BM25/RRF ranks; these are not human relevance labels."""

import json
import time
from datetime import UTC, datetime

from app.core.config import REPOSITORY_ROOT, Settings
from app.database.connection import database_engine
from app.indexing.embeddings import SentenceEncoder
from app.indexing.index import client, search

QUERIES = [
    ("NVDA", "10-K", "export restrictions and supply chain risks"),
    ("NVDA", "10-K", "CUDA"),
    ("NVDA", "10-K", "H100"),
    ("NVDA", "10-Q", "data center revenue demand"),
    ("MSFT", "8-K", "executive officer"),
    ("AAPL", "8-K/A", "financial statements exhibits"),
    ("AMZN", "8-K", "material agreement"),
    ("GOOGL", "8-K", "directors"),
    ("META", "10-Q", "advertising revenue"),
    ("TSLA", "10-Q", "automotive regulatory credits"),
]


def main() -> None:
    settings = Settings()
    encoder = SentenceEncoder(settings)
    engine = database_engine(settings)
    qdrant = client(settings)
    comparisons = []
    generations = set()
    try:
        for ticker, form, query in QUERIES:
            modes = {}
            for mode in ["dense", "bm25", "hybrid"]:
                started = time.perf_counter()
                response = search(
                    engine, encoder, qdrant, query, ticker=ticker, form=form, mode=mode, limit=5
                )
                elapsed = round((time.perf_counter() - started) * 1000, 3)
                generations.add(response["generation"])
                assert all(
                    ticker in h["tickers"] and h["filing_type"] == form for h in response["results"]
                )
                modes[mode] = {
                    "elapsed_ms": elapsed,
                    "retrieval": response["retrieval"],
                    "hits": [
                        {
                            key: h[key]
                            for key in [
                                "chunk_id",
                                "chunk_hash",
                                "document_hash",
                                "accession_number",
                                "source_url",
                                "score",
                                "score_kind",
                                "dense_rank",
                                "bm25_rank",
                                "dense_score",
                                "bm25_score",
                            ]
                        }
                        for h in response["results"]
                    ],
                }
            sets = {mode: {h["chunk_id"] for h in row["hits"]} for mode, row in modes.items()}
            comparisons.append(
                {
                    "query": query,
                    "ticker": ticker,
                    "form": form,
                    "modes": modes,
                    "dense_bm25_overlap_at_5": len(sets["dense"] & sets["bm25"]),
                    "hybrid_chunks_outside_dense_top_5": len(sets["hybrid"] - sets["dense"]),
                }
            )
        assert len(generations) == 1 and None not in generations
        report = {
            "recorded_at": datetime.now(UTC).isoformat(),
            "generation": generations.pop(),
            "scope": "fixed indexed corpus; not a fresh SEC or latest-discovered check",
            "human_relevance_labels": False,
            "quality_improvement_claimed": False,
            "timings": "single local warm-model samples; not a latency benchmark",
            "comparisons": comparisons,
        }
        target = REPOSITORY_ROOT / "evaluation" / "phase-6-baseline.json"
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "report": str(target),
                    "generation": report["generation"],
                    "queries": len(comparisons),
                    "retrieval_runs": len(comparisons) * 3,
                    "queries_with_new_hybrid_top5_chunks": sum(
                        c["hybrid_chunks_outside_dense_top_5"] > 0 for c in comparisons
                    ),
                    "human_relevance_labels": False,
                },
                indent=2,
            )
        )
    finally:
        qdrant.close()
        engine.dispose()


if __name__ == "__main__":
    main()
