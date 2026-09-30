"""Exercise internal routes against stored real filings; no upstream refresh."""

import json
from datetime import UTC, datetime

from app.core.config import Settings
from app.database.connection import database_engine
from app.routing.models import Period, QueryRequest
from app.routing.service import execute_query


def main() -> None:
    settings = Settings()
    engine = database_engine(settings)
    nvidia = Period(basis="annual", period_end="2026-01-25")
    cases = [
        ("fact", QueryRequest(question="Nvidia revenue", period=nvidia), "AVAILABLE"),
        ("calculation", QueryRequest(question="NVDA net margin", period=nvidia), "AVAILABLE"),
        (
            "aligned_comparison",
            QueryRequest(
                question="Compare META and TSLA revenue",
                period=Period(basis="annual", period_end="2025-12-31"),
            ),
            "AVAILABLE",
        ),
        (
            "different_fiscal_periods",
            QueryRequest(
                question="Compare NVDA and MSFT net margin",
                issuer_periods={
                    "NVDA": nvidia,
                    "MSFT": Period(basis="annual", period_end="2026-06-30"),
                },
            ),
            "PARTIAL",
        ),
        ("ambiguous_year", QueryRequest(question="NVDA revenue in 2025"), "NEEDS_CLARIFICATION"),
        (
            "unavailable_market",
            QueryRequest(question="NVDA revenue and stock price", period=nvidia),
            "PARTIAL",
        ),
        ("unavailable_risk", QueryRequest(question="NVDA risk score"), "UNAVAILABLE"),
        (
            "combined_evidence",
            QueryRequest(
                question="Explain NVDA net margin",
                period=nvidia,
                form="10-K",
                accession="0001045810-26-000021",
            ),
            "AVAILABLE",
        ),
    ]
    results = []
    try:
        for label, request, expected in cases:
            result = execute_query(engine, settings, request)
            assert result["status"] == expected, (label, result["status"], result["unavailable"])
            if label == "aligned_comparison":
                assert result["comparisons"][0]["status"] == "ALIGNED"
            if label == "different_fiscal_periods":
                assert result["comparisons"][0]["status"] == "UNALIGNED"
            if label == "combined_evidence":
                assert result["document_evidence"][0]["result"]["retrieved_evidence"]
                assert result["calculated_values"][0]["result"]["value"] is not None
            results.append({"case": label, "response": result})
        target = settings.raw_storage_path.parent / "routing-verification.json"
        target.write_text(
            json.dumps(
                {
                    "verified_at": datetime.now(UTC).isoformat(),
                    "upstream_refreshed": False,
                    "checks": results,
                },
                default=str,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "report": str(target),
                    "checks": len(results),
                    "statuses": {r["case"]: r["response"]["status"] for r in results},
                },
                indent=2,
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
