"""Run with `uv run --directory backend python -m app.ingestion --help`."""

import argparse
import json
import time

from filelock import FileLock, Timeout
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import configure_logging
from app.ingestion.pipeline import SecPipeline
from app.ingestion.store import IngestionStore
from app.providers.sec.client import SecClient, SecError
from app.providers.sec.models import FORMS, INITIAL_TICKERS


def main() -> int:
    parser = argparse.ArgumentParser(description="SEC discovery and primary-document ingestion")
    parser.add_argument("command", choices=["run", "watch", "status"])
    parser.add_argument("--tickers", nargs="+", default=list(INITIAL_TICKERS))
    parser.add_argument("--forms", nargs="+", choices=sorted(FORMS))
    parser.add_argument(
        "--limit", type=int, default=3, help="Maximum pending documents per issuer per cycle"
    )
    parser.add_argument("--backfill", action="store_true", help="Discover older submissions pages")
    parser.add_argument(
        "--enable-sec", action="store_true", help="Enable SEC for this command only"
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000:
        parser.error("--limit must be between 1 and 10000")
    try:
        settings = Settings(sec_enabled=True) if args.enable_sec else Settings()
    except ValidationError:
        print(
            json.dumps({"error": "Invalid configuration; check SEC application/contact and limits"})
        )
        return 2
    settings.sec_state_path.mkdir(parents=True, exist_ok=True)
    configure_logging(settings.log_level)
    if args.command == "status":
        store = IngestionStore(settings.sec_state_path, settings.raw_storage_path)
        try:
            print(json.dumps(store.summary(settings.sec_poll_seconds * 2), indent=2))
            return 0
        finally:
            store.close()
    # A single local ingestion writer shares one limiter. Multi-host workers require Redis later.
    try:
        with FileLock(settings.sec_state_path / "ingestion.lock", timeout=0):
            store = IngestionStore(settings.sec_state_path, settings.raw_storage_path)
            try:
                client = SecClient(settings, store)
                try:
                    pipeline = SecPipeline(client, store)
                    while True:
                        try:
                            report = pipeline.run(
                                [ticker.upper() for ticker in args.tickers],
                                limit=args.limit,
                                backfill=args.backfill,
                                forms=set(args.forms) if args.forms else None,
                            )
                        except SecError as exc:
                            if args.command == "run":
                                raise
                            report = {"companies": {}, "errors": [{"error": str(exc)}]}
                        print(json.dumps(report, indent=2), flush=True)
                        if args.command == "run":
                            return int(
                                bool(report["errors"])
                                or any(row["failed"] for row in report["companies"].values())
                            )
                        time.sleep(settings.sec_poll_seconds)
                finally:
                    client.close()
            finally:
                store.close()
    except Timeout:
        print(json.dumps({"error": "Another local SEC ingestion process is running"}))
        return 2
    except (SecError, ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, SecError) else type(exc).__name__}))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
