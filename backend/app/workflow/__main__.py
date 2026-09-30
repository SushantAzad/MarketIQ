"""Submit, run/resume, inspect and expire local research workflow jobs."""

import argparse
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.database.connection import database_engine
from app.routing.models import Period, QueryRequest
from app.workflow.service import create_job, purge_expired, read_job, run_job


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("question")
    submit.add_argument("--tickers", nargs="+", default=[])
    submit.add_argument("--basis", choices=["instant", "annual", "quarter", "ytd_6m", "ytd_9m"])
    submit.add_argument("--period-end")
    submit.add_argument("--comparison-end")
    submit.add_argument("--form", choices=["10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A"])
    submit.add_argument("--accession")
    submit.add_argument("--as-of", type=datetime.fromisoformat)
    submit.add_argument("--unit", default="USD")
    for name in ["run", "show"]:
        command = sub.add_parser(name)
        command.add_argument("job_id", type=UUID)
    sub.add_parser("purge-expired")
    args = parser.parse_args()
    try:
        settings = Settings()
        engine = database_engine(settings)
        try:
            if args.command == "submit":
                if (
                    bool(args.basis) != bool(args.period_end)
                    or args.comparison_end
                    and not args.basis
                ):
                    raise ValueError("Supply basis and period end together")
                period = (
                    Period(
                        basis=args.basis,
                        period_end=args.period_end,
                        comparison_end=args.comparison_end,
                    )
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
                output: dict[str, Any] = {
                    "job_id": str(create_job(engine, settings, request)),
                    "status": "QUEUED",
                }
            elif args.command == "purge-expired":
                output = {"deleted_expired_jobs": purge_expired(engine)}
            elif args.command == "run":
                output = run_job(engine, settings, args.job_id)
            else:
                output = read_job(engine, args.job_id) or {"status": "NOT_FOUND"}
        finally:
            engine.dispose()
        print(json.dumps(output, default=str, indent=2))
        return int(output.get("status") in {"FAILED", "NOT_FOUND", "INCOMPATIBLE_VERSION"})
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "UNAVAILABLE",
                    "error": type(exc).__name__,
                    "reason": "Check workflow arguments, database and migrations",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
