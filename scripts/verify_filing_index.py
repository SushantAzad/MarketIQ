"""Opt-in real-data verification; run with PYTHONPATH=backend after provisioning."""

import json
from datetime import UTC, datetime

from app.core.config import Settings
from app.database.connection import database_engine
from app.indexing.embeddings import SentenceEncoder
from app.indexing.index import client, publish, search


def main() -> None:
    settings = Settings()
    encoder = SentenceEncoder(settings)
    engine = database_engine(settings)
    qdrant = client(settings)
    try:
        initial = publish(engine, settings, encoder, qdrant)
        replay = publish(engine, settings, encoder, qdrant)
        assert replay["generation"] == initial["generation"]
        assert replay["new_embeddings"] == 0
        rebuilt = publish(engine, settings, encoder, qdrant, rebuild=True, from_canonical=True)
        assert rebuilt["generation"] != initial["generation"]
        assert rebuilt["new_embeddings"] == 0 and rebuilt["chunks"] == initial["chunks"]
        checks = []
        for ticker, form in [
            ("NVDA", "10-K"),
            ("NVDA", "10-Q"),
            ("MSFT", "8-K"),
            ("AAPL", "8-K/A"),
            ("AMZN", "8-K"),
            ("GOOGL", "8-K"),
            ("META", "10-Q"),
            ("TSLA", "10-Q"),
        ]:
            result = search(
                engine,
                encoder,
                qdrant,
                "business risks and financial performance",
                ticker=ticker,
                form=form,
                limit=2,
            )
            assert result["results"], (ticker, form)
            for hit in result["results"]:
                assert ticker in hit["tickers"] and hit["filing_type"] == form
                assert hit["source_url"].startswith("https://www.sec.gov/Archives/")
                assert hit["page"] is None and hit["text"]
            checks.append(
                {
                    "ticker": ticker,
                    "form": form,
                    "results": len(result["results"]),
                    "accessions": sorted({h["accession_number"] for h in result["results"]}),
                }
            )
        assert not search(engine, encoder, qdrant, "risk", ticker="NONEXISTENT")["results"]
        assert not search(
            engine, encoder, qdrant, "risk", accepted_before=datetime(2000, 1, 1, tzinfo=UTC)
        )["results"]
        report = {
            "verified_at": datetime.now(UTC).isoformat(),
            "runtime": "Qdrant server",
            "model": settings.embedding_model,
            "revision": settings.embedding_revision,
            "dimension": encoder.dimension,
            "initial": initial,
            "replay": replay,
            "rebuilt": rebuilt,
            "filtered_queries": checks,
            "empty_filter_checks": 2,
            "canonical_text_checks": 16,
            "upstream_refreshed": False,
            "semantic_quality_benchmarked": False,
        }
        path = settings.raw_storage_path.parent / "index-verification.json"
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        qdrant.close()
        engine.dispose()


if __name__ == "__main__":
    main()
