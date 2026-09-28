"""Opt-in real retrieval/provider checks; run with PYTHONPATH=backend."""

import json
from datetime import UTC, datetime
from uuid import UUID

from app.core.config import Settings
from app.database.connection import database_engine
from app.indexing.embeddings import SentenceEncoder
from app.indexing.index import client
from app.research.service import ResearchRequest, read_run, research


def main() -> None:
    settings = Settings()
    engine = database_engine(settings)
    encoder = SentenceEncoder(settings)
    qdrant = client(settings)
    try:
        result = research(
            engine,
            encoder,
            qdrant,
            settings,
            ResearchRequest(
                question="What risks does NVIDIA disclose about export restrictions?",
                ticker="NVDA",
                form="10-K",
            ),
        )
        assert result["retrieved_evidence"]
        if settings.reranker_enabled:
            assert result["reranking"]["status"] == "applied"
        assert read_run(engine, UUID(result["run_id"])) == result
        assert all(
            e["source_url"].startswith("https://www.sec.gov/Archives/")
            for e in result["retrieved_evidence"]
        )
        if settings.llm_provider == "disabled":
            assert result["status"] == "EVIDENCE_ONLY" and result["answer"] is None
        else:
            assert result["status"] == "ANSWERED", result["reason"]
            for claim in result["answer"]["claims"]:
                source = next(
                    e for e in result["retrieved_evidence"] if e["source_id"] == claim["source_id"]
                )
                assert claim["text"] == source["text"]
        # An unknown company must not yield substituted issuer evidence or invented answers.
        absent = research(
            engine,
            encoder,
            qdrant,
            settings,
            ResearchRequest(
                question="What risks does this issuer disclose?", ticker="ZZZZ", form="10-K"
            ),
        )
        assert absent["status"] == "INSUFFICIENT_EVIDENCE" and absent["answer"] is None
        report = {
            "verified_at": datetime.now(UTC).isoformat(),
            "run_id": result["run_id"],
            "status": result["status"],
            "passages": len(result["retrieved_evidence"]),
            "scope": result["scope"],
            "generation": result["generation"],
            "retrieval": result["retrieval"],
            "reranking": result["reranking"],
            "trace_round_trip": True,
            "absent_evidence_refused": True,
            "provider": settings.llm_provider,
            "live_model_verified": settings.llm_provider != "disabled",
            "semantic_quality_benchmarked": False,
            "upstream_refreshed": False,
        }
        (settings.raw_storage_path.parent / "research-verification.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, indent=2))
    finally:
        qdrant.close()
        engine.dispose()


if __name__ == "__main__":
    main()
