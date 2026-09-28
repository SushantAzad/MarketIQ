"""Database migrations, journal import, and sourced financial queries."""

import argparse
import json
from datetime import date, datetime

import sqlalchemy as sa
from alembic.config import Config
from filelock import Timeout
from pydantic import ValidationError

from alembic import command
from app.core.config import REPOSITORY_ROOT, Settings
from app.database.connection import database_engine
from app.database.journal_import import import_journal
from app.database.refresh import refresh_journal
from app.providers.sec.client import SecError
from app.providers.sec.models import INITIAL_TICKERS
from app.services.financial_query import financial_value
from app.services.normalization import METRIC_CONCEPTS


def main() -> int:
    parser = argparse.ArgumentParser(description="PostgreSQL financial data")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Apply pending Alembic migrations")
    sub.add_parser(
        "import-journal", help="Import saved SEC snapshots; does not fetch upstream data"
    )
    sub.add_parser("status")
    sync = sub.add_parser("sync", help="Refresh SEC metadata/facts, then update PostgreSQL")
    sync.add_argument("--tickers", nargs="+", default=list(INITIAL_TICKERS))
    sync.add_argument("--enable-sec", action="store_true")
    query = sub.add_parser("query", help="Read stored financial facts with provenance")
    query.add_argument("ticker")
    query.add_argument("metric", choices=sorted(METRIC_CONCEPTS))
    query.add_argument(
        "--basis",
        required=True,
        choices=["instant", "annual", "quarter", "ytd_6m", "ytd_9m", "duration_other"],
    )
    query.add_argument("--unit", default="USD")
    query.add_argument("--period-end", type=date.fromisoformat)
    query.add_argument("--as-of", type=datetime.fromisoformat)
    args = parser.parse_args()
    try:
        settings = Settings(sec_enabled=True) if getattr(args, "enable_sec", False) else Settings()
        engine = database_engine(settings)
        try:
            if args.command == "migrate":
                config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
                with engine.begin() as connection:
                    config.attributes["connection"] = connection
                    command.upgrade(config, "head")
                output: object = {"migration": "head"}
            elif args.command == "import-journal":
                output = {"upstream_refreshed": False, "imports": import_journal(engine, settings)}
            elif args.command == "sync":
                refresh = refresh_journal(settings, [ticker.upper() for ticker in args.tickers])
                output = {
                    "refresh": refresh,
                    "upstream_refreshed": not refresh["errors"],
                    "imports": import_journal(engine, settings),
                }
                print(json.dumps(output, default=str, indent=2))
                return int(bool(refresh["errors"]))
            elif args.command == "query":
                with engine.connect() as connection:
                    output = financial_value(
                        connection,
                        args.ticker,
                        args.metric,
                        basis=args.basis,
                        unit=args.unit,
                        period_end=args.period_end,
                        as_of=args.as_of,
                        freshness_seconds=settings.sec_poll_seconds * 2,
                    )
            else:
                with engine.connect() as connection:
                    output = {
                        "database": "available",
                        "revision": connection.execute(
                            sa.text("SELECT version_num FROM alembic_version")
                        ).scalar_one(),
                    }
            print(json.dumps(output, default=str, indent=2))
            return 0
        finally:
            engine.dispose()
    except (ValidationError, sa.exc.SQLAlchemyError, ValueError, OSError, SecError, Timeout) as exc:
        # Driver and validation exceptions may contain credentials/values. Print type only.
        print(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "message": "Check database configuration, migrations, and source files",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
