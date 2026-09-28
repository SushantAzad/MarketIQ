"""Explicit provisioning, publication, and evidence search commands."""

import argparse
import json
from datetime import datetime

import sqlalchemy as sa

from app.core.config import Settings
from app.database import schema as db
from app.database.connection import database_engine
from app.indexing.corpus import latest_accession
from app.indexing.embeddings import SentenceEncoder
from app.indexing.index import alias_target, client, publish, search


def main() -> int:
    parser = argparse.ArgumentParser(description="Versioned filing vector index")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("provision", help="Download the configured pinned model")
    build = sub.add_parser("build", help="Index all parsed filings; reuse embeddings")
    build.add_argument("--rebuild", action="store_true", help="Create a fresh collection")
    build.add_argument(
        "--from-canonical",
        action="store_true",
        help="Recover the active SQL corpus without the SEC journal; requires --rebuild",
    )
    sub.add_parser("status")
    query = sub.add_parser("search", help="Retrieve evidence; does not generate answers")
    query.add_argument("query")
    query.add_argument("--ticker")
    query.add_argument("--form")
    query.add_argument("--section")
    query.add_argument("--accepted-before", type=datetime.fromisoformat)
    query.add_argument("--limit", type=int, default=5)
    query.add_argument("--latest", action="store_true", help="Requires ticker and exact form")
    args = parser.parse_args()
    if args.command == "search" and args.latest and (not args.ticker or not args.form):
        parser.error("--latest requires --ticker and --form")
    try:
        settings = Settings()
        if args.command == "provision":
            encoder = SentenceEncoder(settings, provision=True)
            output: object = {
                "model_key": encoder.key,
                "dimension": encoder.dimension,
                "max_tokens": encoder.max_tokens,
                "provisioned": True,
            }
        else:
            engine = database_engine(settings)
            qdrant = client(settings)
            try:
                if args.command == "status":
                    with engine.connect() as connection:
                        generations = [
                            dict(r)
                            for r in connection.execute(
                                sa.select(db.index_generations).order_by(
                                    db.index_generations.c.created_at.desc()
                                )
                            ).mappings()
                        ]
                    target = alias_target(qdrant)
                    active = next((g for g in generations if g["state"] == "active"), None)
                    output = {
                        "generations": generations,
                        "alias_target": target,
                        "consistent": active is not None and active["collection_name"] == target,
                    }
                else:
                    encoder = SentenceEncoder(settings)
                    if args.command == "build":
                        output = publish(
                            engine,
                            settings,
                            encoder,
                            qdrant,
                            rebuild=args.rebuild,
                            from_canonical=args.from_canonical,
                        )
                    else:
                        ticker = args.ticker.upper() if args.ticker else None
                        accession = (
                            latest_accession(settings, str(ticker), args.form)
                            if args.latest
                            else None
                        )
                        if args.latest and accession is None:
                            output = {
                                "status": "UNAVAILABLE",
                                "reason": "No discovered filing",
                                "results": [],
                            }
                        else:
                            output = search(
                                engine,
                                encoder,
                                qdrant,
                                args.query,
                                ticker=ticker,
                                form=args.form,
                                accession=accession,
                                section=args.section,
                                accepted_before=args.accepted_before,
                                limit=args.limit,
                            )
                            if args.latest:
                                output["latest_discovered_accession"] = accession
                                if not output["results"]:
                                    output["reason"] = (
                                        "Latest discovered filing has no matching indexed evidence"
                                    )
            finally:
                qdrant.close()
                engine.dispose()
        print(json.dumps(output, default=str, indent=2))
        return 0
    except Exception as exc:
        # SDK/driver errors may carry credentials or payloads. Keep CLI diagnostics safe.
        print(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "message": (
                        "Check migrations, model provisioning, source integrity, "
                        "and Qdrant availability"
                    ),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
