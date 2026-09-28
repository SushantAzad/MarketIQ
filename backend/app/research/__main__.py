"""Ask for scoped filing evidence and inspect saved baseline research results."""

import argparse
import json
from uuid import UUID

from app.core.config import Settings
from app.database.connection import database_engine
from app.indexing.embeddings import SentenceEncoder
from app.indexing.index import client
from app.research.service import ResearchRequest, read_run, research


def main() -> int:
    parser = argparse.ArgumentParser(description="Temporary extractive RAG baseline")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--ticker", required=True)
    ask.add_argument("--form", required=True)
    ask.add_argument(
        "--accession", help="Defaults to latest locally discovered filing of this form"
    )
    show = sub.add_parser("show")
    show.add_argument("run_id", type=UUID)
    sub.add_parser("status")
    args = parser.parse_args()
    try:
        settings = Settings()
        if args.command == "status":
            output: object = {
                "baseline": "extractive-rag-v1",
                "provider": settings.llm_provider,
                "model": settings.llm_model,
                "connectivity_tested": False,
                "generation_configured": settings.llm_provider != "disabled",
            }
        else:
            request = (
                ResearchRequest(
                    question=args.question,
                    ticker=args.ticker.upper(),
                    form=args.form,
                    accession=args.accession,
                )
                if args.command == "ask"
                else None
            )
            engine = database_engine(settings)
            try:
                if request is None:
                    output = read_run(engine, args.run_id)
                    if output is None:
                        print(json.dumps({"status": "NOT_FOUND"}))
                        return 1
                else:
                    encoder = SentenceEncoder(settings)
                    qdrant = client(settings)
                    try:
                        output = research(engine, encoder, qdrant, settings, request)
                    finally:
                        qdrant.close()
            finally:
                engine.dispose()
        print(json.dumps(output, default=str, indent=2))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "UNAVAILABLE",
                    "error": type(exc).__name__,
                    "message": "Check configuration, migrations, model files, and index health",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
