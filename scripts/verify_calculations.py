"""Check calculations against existing real SEC facts without refreshing upstream data."""

import json
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from uuid import UUID

from app.core.config import Settings
from app.database.connection import database_engine
from app.services.calculation_query import CalculationRequest, read_calculation, run_calculation
from app.services.financial_query import financial_value


def main() -> None:
    settings = Settings()
    engine = database_engine(settings)
    cutoff = datetime.now(UTC)
    results = []
    try:
        for ticker in ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA"]:
            with engine.connect() as connection:
                revenue = financial_value(
                    connection, ticker, "revenue", basis="annual", as_of=cutoff
                )
            assert revenue["value"] is not None
            for metric in ["net_margin", "fcf", "current_ratio", "roe", "ebitda", "debt_equity"]:
                request = CalculationRequest(
                    ticker=ticker,
                    metric=metric,
                    period_end=revenue["period_end"],
                    basis="instant" if metric in {"current_ratio", "debt_equity"} else "annual",
                    as_of=cutoff,
                )
                result = run_calculation(engine, request)
                assert read_calculation(engine, UUID(result["run_id"])) == result
                if result["value"] is not None:
                    operands = result["operands"]
                    assert all(
                        o["source"]["content_hash"] and o["source"]["fact_id"]
                        for o in operands.values()
                    )
                    with localcontext() as context:
                        context.prec = 38
                        if metric == "net_margin":
                            expected = (
                                Decimal(operands["net_income"]["value"])
                                / Decimal(operands["revenue"]["value"])
                                * 100
                            )
                            assert Decimal(result["value"]) == expected
                        if metric == "fcf":
                            expected = Decimal(operands["operating_cash_flow"]["value"]) - Decimal(
                                operands["capital_expenditures"]["value"]
                            )
                            assert Decimal(result["value"]) == expected
                else:
                    assert result["reason"]
                results.append(result)
        report = {
            "verified_at": cutoff.isoformat(),
            "upstream_refreshed": False,
            "trace_round_trips": len(results),
            "results": results,
        }
        target = settings.raw_storage_path.parent / "calculation-verification.json"
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "report": str(target),
                    "checks": len(results),
                    "available": sum(r["value"] is not None for r in results),
                    "results": [
                        {k: r[k] for k in ["ticker", "metric", "value", "reason", "run_id"]}
                        for r in results
                    ],
                },
                indent=2,
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
