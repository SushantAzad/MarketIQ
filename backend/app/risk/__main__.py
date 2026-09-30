"""Offline cohort collection, point-in-time dataset audit and gated experiments."""

import argparse
import json
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.database.connection import database_engine
from app.risk.cohort import collect
from app.risk.dataset import audit
from app.risk.training import gate, timestamp, train


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cohort = sub.add_parser("cohort")
    cohort.add_argument("--limit", type=int, default=80)
    cohort.add_argument("--enable-sec", action="store_true")
    cohort.add_argument("--historical-ciks", nargs="*", default=[])
    sub.add_parser("audit")
    sub.add_parser("status")
    training = sub.add_parser("train")
    training.add_argument("--validation-start", type=timestamp, required=True)
    training.add_argument("--test-start", type=timestamp, required=True)
    training.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        settings = Settings(sec_enabled=True) if getattr(args, "enable_sec", False) else Settings()
        if args.command == "cohort":
            cohort_report = collect(
                settings, limit=args.limit, historical_ciks=args.historical_ciks
            )
            output: dict[str, Any] = {k: v for k, v in cohort_report.items() if k != "candidates"}
        else:
            path = settings.raw_storage_path.parent / "risk/dataset.json"
            if args.command == "audit":
                engine = database_engine(settings)
                try:
                    dataset = audit(engine, settings)
                finally:
                    engine.dispose()
            else:
                dataset = json.loads(path.read_text(encoding="utf-8"))
            if args.command == "train":
                output = train(dataset, args.output, args.validation_start, args.test_start)
            else:
                output = {
                    "gate": gate(dataset),
                    "imported_issuers": dataset["imported_issuers"],
                    "feature_cutoffs_examined": dataset["feature_cutoffs_examined"],
                    "snapshot_observation_range": dataset["snapshot_observation_range"],
                    "pending_outcomes": len(dataset["pending"]),
                    "eligible_cohort_candidates": dataset.get("cohort", {}).get(
                        "eligible_candidates", 0
                    ),
                    "dataset_path": str(path),
                }
        print(json.dumps(output, default=str, indent=2))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "UNAVAILABLE",
                    "error": type(exc).__name__,
                    "reason": "Check risk configuration, source coverage and offline dependencies",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
