"""Bridge the Phase-2 journal into PostgreSQL; never modify the ingestion journal."""

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from app.core.config import Settings
from app.database import schema as db
from app.services.financial_import import import_snapshot, put, stable_id
from app.services.normalization import fingerprint


def import_journal(engine: sa.Engine, settings: Settings) -> list[dict[str, Any]]:
    path = settings.sec_state_path / "journal.sqlite3"
    if not path.is_file():
        raise ValueError("No SEC ingestion journal; run SEC ingestion first")
    journal = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    journal.row_factory = sqlite3.Row
    journal.execute("BEGIN")  # One consistent WAL snapshot, even if ingestion runs concurrently.
    try:

        def read(digest: str) -> bytes:
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("Invalid journal object hash")
            body = (settings.raw_storage_path / digest[:2] / digest).read_bytes()
            if hashlib.sha256(body).hexdigest() != digest:
                raise ValueError("Journal object checksum mismatch")
            return body

        registry_row = journal.execute(
            "SELECT data_version FROM resources WHERE url='https://www.sec.gov/files/company_tickers.json'"
        ).fetchone()
        aliases: dict[str, list[str]] = {}
        if registry_row and registry_row["data_version"]:
            for row in json.loads(read(registry_row["data_version"])).values():
                aliases.setdefault(str(row["cik_str"]).zfill(10), []).append(row["ticker"])
        metadata: dict[str, dict[str, dict[str, Any]]] = {}
        for row in journal.execute("SELECT cik,accession,metadata FROM filings"):
            metadata.setdefault(row["cik"], {})[row["accession"]] = json.loads(row["metadata"])
        metadata_sources = []
        for source_row in journal.execute(
            "SELECT * FROM source_objects WHERE url LIKE '%/submissions/CIK%' "
            "ORDER BY first_fetched_at"
        ):
            payload = json.loads(read(source_row["content_hash"]))
            cik = source_row["url"].rsplit("CIK", 1)[-1][:10]
            columns = payload.get("filings", {}).get("recent", payload)
            source_id = stable_id("source", source_row["url"] + ":" + source_row["content_hash"])
            for accession in columns.get("accessionNumber", []):
                if accession in metadata.get(cik, {}):
                    metadata[cik][accession]["metadata_source_id"] = source_id
            metadata_sources.append(
                {
                    "id": source_id,
                    "provider": "SEC EDGAR",
                    "source_url": source_row["url"],
                    "content_hash": source_row["content_hash"],
                    "object_key": f"{source_row['content_hash'][:2]}/{source_row['content_hash']}",
                    "first_fetched_at": datetime.fromisoformat(source_row["first_fetched_at"]),
                    "source_timestamp": source_row["source_timestamp"],
                }
            )
        with engine.begin() as connection:
            put(connection, db.source_objects, metadata_sources)
        reports = []
        snapshots = journal.execute(
            "SELECT * FROM source_objects WHERE url LIKE '%/api/xbrl/companyfacts/%' "
            "ORDER BY first_fetched_at,content_hash"
        ).fetchall()
        for row in snapshots:
            cik = row["url"].rsplit("CIK", 1)[-1].removesuffix(".json")
            with engine.begin() as connection:
                reports.append(
                    import_snapshot(
                        connection,
                        read(row["content_hash"]),
                        {
                            "content_hash": row["content_hash"],
                            "source_url": row["url"],
                            "source_timestamp": row["source_timestamp"],
                            "first_fetched_at": row["first_fetched_at"],
                        },
                        filing_metadata=metadata.get(cik),
                        tickers=aliases.get(cik),
                    )
                )
        with engine.begin() as connection:
            if journal.execute(
                "SELECT 1 FROM sqlite_master WHERE name='source_observations'"
            ).fetchone():
                observations = journal.execute(
                    "SELECT * FROM source_observations WHERE url LIKE '%/api/xbrl/companyfacts/%'"
                ).fetchall()
            else:
                observations = journal.execute(
                    "SELECT url,data_version AS content_hash,last_successful_fetch AS observed_at "
                    "FROM resources WHERE url LIKE '%/api/xbrl/companyfacts/%' "
                    "AND data_version IS NOT NULL"
                ).fetchall()
            put(
                connection,
                db.snapshot_observations,
                [
                    {
                        "source_id": stable_id("source", row["url"] + ":" + row["content_hash"]),
                        "observed_at": datetime.fromisoformat(row["observed_at"]),
                    }
                    for row in observations
                ],
            )
            for row in journal.execute("SELECT * FROM resources"):
                values = {
                    key: row[key]
                    for key in (
                        "provider",
                        "source_timestamp",
                        "data_version",
                        "status",
                        "error_message",
                    )
                }
                values["source_url"] = row["url"]
                for key in ("last_attempted_fetch", "last_successful_fetch"):
                    values[key] = datetime.fromisoformat(row[key]) if row[key] else None
                stmt = insert(db.data_source_state).values(**values)
                connection.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[db.data_source_state.c.source_url],
                        set_=values,
                        where=(
                            db.data_source_state.c.last_attempted_fetch
                            <= stmt.excluded.last_attempted_fetch
                        ),
                    )
                )
            logs = []
            for row in journal.execute("SELECT * FROM fetch_logs"):
                key = fingerprint(
                    [row["url"], row["attempted_at"], row["completed_at"], row["attempt"]]
                )
                logs.append(
                    {
                        "id": stable_id("fetch", key),
                        "journal_key": key,
                        "provider": "SEC EDGAR",
                        "source_url": row["url"],
                        "attempted_at": datetime.fromisoformat(row["attempted_at"]),
                        "completed_at": datetime.fromisoformat(row["completed_at"]),
                        "outcome": row["outcome"],
                        "http_status": row["http_status"],
                        "error": row["error"],
                        "latency_ms": row["latency_ms"],
                    }
                )
            put(connection, db.data_fetch_logs, logs)
        return reports
    finally:
        journal.close()
