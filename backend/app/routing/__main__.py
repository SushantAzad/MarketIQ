"""Inspect a typed query plan or execute bounded internal routes."""

import argparse
import json
from datetime import datetime

from app.core.config import Settings
from app.database.connection import database_engine
from app.routing.models import Period, QueryRequest
from app.routing.planner import load_issuers, plan_query
from app.routing.service import execute_query


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "ask"])
    parser.add_argument("question")
    parser.add_argument("--tickers", nargs="+", default=[])
    parser.add_argument("--basis", choices=["instant", "annual", "quarter", "ytd_6m", "ytd_9m"])
    parser.add_argument("--period-end")
    parser.add_argument("--comparison-end")
    parser.add_argument("--form", choices=["10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A"])
    parser.add_argument("--accession")
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--unit", default="USD")
    args = parser.parse_args()
    try:
        if bool(args.basis) != bool(args.period_end) or args.comparison_end and not args.basis:
            raise ValueError("Supply basis and period end together")
        period = (
            Period(basis=args.basis, period_end=args.period_end, comparison_end=args.comparison_end)
            if args.basis
            else None
        )
        request = QueryRequest(
            question=args.question,
            tickers=args.tickers,
            period=period,
            form=args.form,
            accession=args.accession,
            as_of=args.as_of,
            unit=args.unit,
        )
        settings = Settings()
        engine = database_engine(settings)
        try:
            if args.command == "plan":
                with engine.connect() as connection:
                    result = plan_query(request, load_issuers(connection)).model_dump(mode="json")
            else:
                result = execute_query(engine, settings, request)
        finally:
            engine.dispose()
        print(json.dumps(result, default=str, indent=2))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "UNAVAILABLE",
                    "error": type(exc).__name__,
                    "reason": "Check routing arguments, database configuration and migrations",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
