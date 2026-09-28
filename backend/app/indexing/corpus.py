"""Import a consistent parsed-journal snapshot into canonical PostgreSQL evidence."""

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any

import sqlalchemy as sa

from app.core.config import Settings
from app.database import schema as db
from app.indexing.chunking import chunk_document
from app.indexing.embeddings import Encoder
from app.providers.sec.models import Filing
from app.services.financial_import import put, stable_id


def read_object(settings: Settings, digest: str) -> bytes:
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid artifact hash")
    body = (settings.raw_storage_path / digest[:2] / digest).read_bytes()
    if hashlib.sha256(body).hexdigest() != digest:
        raise ValueError("Artifact checksum mismatch")
    return body


def import_corpus(
    connection: sa.Connection, settings: Settings, encoder: Encoder
) -> list[dict[str, Any]]:
    path = (settings.sec_state_path / "journal.sqlite3").resolve()
    journal = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    journal.row_factory = sqlite3.Row
    journal.execute("BEGIN")
    try:
        result = []
        for row in journal.execute(
            "SELECT * FROM filings WHERE stage='parsed' ORDER BY cik,accession"
        ):
            filing = Filing.model_validate_json(row["metadata"])
            parsed = json.loads(read_object(settings, row["parsed_hash"]))
            read_object(settings, row["raw_hash"])
            if (
                parsed["raw_hash"] != row["raw_hash"]
                or parsed["source_url"] != filing.url
                or Filing.model_validate(parsed["filing"]) != filing
                or parsed["parser_version"] != row["parser_version"]
            ):
                raise ValueError("Parsed document provenance mismatch")
            company = (
                connection.execute(sa.select(db.companies).where(db.companies.c.cik == filing.cik))
                .mappings()
                .one()
            )
            aliases = list(
                connection.execute(
                    sa.select(db.securities.c.ticker)
                    .where(db.securities.c.company_id == company["id"])
                    .order_by(db.securities.c.ticker)
                ).scalars()
            )
            metadata_source_id = None
            for source in journal.execute(
                "SELECT * FROM source_objects WHERE url LIKE ? ORDER BY first_fetched_at DESC",
                (f"%/submissions/CIK{filing.cik}%",),
            ):
                payload = json.loads(read_object(settings, source["content_hash"]))
                columns = payload.get("filings", {}).get("recent", payload)
                if filing.accession_number in columns.get("accessionNumber", []):
                    metadata_source_id = stable_id(
                        "source", source["url"] + ":" + source["content_hash"]
                    )
                    break
            if metadata_source_id is None:
                raise ValueError("Missing SEC metadata source; sync database first")
            filing_id = stable_id("filing", filing.cik + ":" + filing.accession_number)
            put(
                connection,
                db.filings,
                [
                    {
                        "id": filing_id,
                        "company_id": company["id"],
                        "accession_number": filing.accession_number,
                        "form_type": filing.form_type,
                        "filing_date": filing.filing_date,
                        "accepted_at": filing.accepted_at,
                        "report_period": filing.report_period,
                        "source_url": filing.url,
                        "metadata_source_id": metadata_source_id,
                    }
                ],
            )
            source = journal.execute(
                "SELECT * FROM source_objects WHERE url=? AND content_hash=?",
                (filing.url, row["raw_hash"]),
            ).fetchone()
            if source is None:
                raise ValueError("Missing raw source provenance")
            source_id = stable_id("source", filing.url + ":" + row["raw_hash"])
            put(
                connection,
                db.source_objects,
                [
                    {
                        "id": source_id,
                        "provider": "SEC EDGAR",
                        "source_url": filing.url,
                        "content_hash": row["raw_hash"],
                        "object_key": f"{row['raw_hash'][:2]}/{row['raw_hash']}",
                        "first_fetched_at": datetime.fromisoformat(source["first_fetched_at"]),
                        "source_timestamp": source["source_timestamp"],
                    }
                ],
            )
            document_id = stable_id("document", str(filing_id) + ":" + row["parsed_hash"])
            put(
                connection,
                db.filing_documents,
                [
                    {
                        "id": document_id,
                        "filing_id": filing_id,
                        "source_id": source_id,
                        "document_hash": row["raw_hash"],
                        "parsed_hash": row["parsed_hash"],
                        "parser_version": parsed["parser_version"],
                    }
                ],
            )
            version, chunks = chunk_document(
                parsed, encoder, settings.chunk_tokens, settings.chunk_overlap
            )
            records = []
            for chunk in chunks:
                chunk_id = stable_id("chunk", f"{document_id}:{version}:{chunk['ordinal']}")
                records.append(
                    {
                        "id": chunk_id,
                        "document_id": document_id,
                        "chunker_version": version,
                        **chunk,
                    }
                )
                payload = {
                    "chunk_id": str(chunk_id),
                    "company_id": str(company["id"]),
                    "company": company["legal_name"],
                    "tickers": aliases,
                    "cik": filing.cik,
                    "filing_id": str(filing_id),
                    "accession_number": filing.accession_number,
                    "filing_type": filing.form_type,
                    "filing_date": str(filing.filing_date),
                    "accepted_at": filing.accepted_at.isoformat(),
                    "report_period": str(filing.report_period) if filing.report_period else None,
                    "fiscal_year": None,
                    "section": chunk["section"],
                    "page": None,
                    "source_url": filing.url,
                    "source_anchor": None,
                    "document_hash": row["raw_hash"],
                    "parsed_hash": row["parsed_hash"],
                    "chunk_hash": chunk["content_hash"],
                    "parser_version": parsed["parser_version"],
                    "chunker_version": version,
                    "model_key": encoder.key,
                    "model_revision": settings.embedding_revision,
                    "start_offset": chunk["start_offset"],
                    "end_offset": chunk["end_offset"],
                    "offset_basis": "normalized_text",
                    "section_detection": "heuristic",
                    "source_observed_at": source["first_fetched_at"],
                }
                result.append({"id": chunk_id, "text": chunk["text"], "payload": payload})
            put(connection, db.document_chunks, records)
        if not result:
            raise ValueError("No parsed filings available to index")
        return result
    finally:
        journal.close()


def latest_accession(settings: Settings, ticker: str, form: str) -> str | None:
    """Consult discovered metadata, including documents not yet parsed/indexed."""
    path = (settings.sec_state_path / "journal.sqlite3").resolve()
    journal = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        registry = journal.execute(
            "SELECT data_version FROM resources WHERE url='https://www.sec.gov/files/company_tickers.json'"
        ).fetchone()
        if not registry:
            raise ValueError("Ticker registry unavailable")
        cik = next(
            (
                str(r["cik_str"]).zfill(10)
                for r in json.loads(read_object(settings, registry[0])).values()
                if r["ticker"] == ticker
            ),
            None,
        )
        candidates = [
            Filing.model_validate_json(r[0])
            for r in journal.execute("SELECT metadata FROM filings WHERE cik=?", (cik,))
        ]
        matching = [f for f in candidates if f.form_type == form]
        return max(matching, key=lambda f: f.accepted_at).accession_number if matching else None
    finally:
        journal.close()
