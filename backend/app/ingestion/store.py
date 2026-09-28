"""Local ingestion journal, not the planned PostgreSQL financial database.

The CLI holds a process lock for mutations. SQLite transactions record progress;
content-addressed files are atomically published before their journal references.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.providers.sec.models import Filing


def now() -> str:
    return datetime.now(UTC).isoformat()


class IngestionStore:
    def __init__(self, directory: Path, raw_directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.raw_directory = raw_directory
        raw_directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "journal.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS filings (
                cik TEXT NOT NULL, accession TEXT NOT NULL, metadata TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT 'discovered', error TEXT,
                raw_hash TEXT, parsed_hash TEXT, parser_version TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                PRIMARY KEY(cik, accession)
            );
            CREATE TABLE IF NOT EXISTS resources (
                url TEXT PRIMARY KEY, provider TEXT NOT NULL DEFAULT 'SEC EDGAR',
                last_attempted_fetch TEXT, last_successful_fetch TEXT,
                source_timestamp TEXT, data_version TEXT, error_message TEXT,
                status TEXT NOT NULL DEFAULT 'UNAVAILABLE'
            );
            CREATE TABLE IF NOT EXISTS source_objects (
                url TEXT NOT NULL, content_hash TEXT NOT NULL, source_timestamp TEXT,
                first_fetched_at TEXT NOT NULL, provider TEXT NOT NULL DEFAULT 'SEC EDGAR',
                PRIMARY KEY(url,content_hash)
            );
            INSERT OR IGNORE INTO source_objects
                (url,content_hash,source_timestamp,first_fetched_at)
                SELECT url,data_version,source_timestamp,last_successful_fetch
                FROM resources WHERE data_version IS NOT NULL;
            CREATE TABLE IF NOT EXISTS fetch_logs (
                id INTEGER PRIMARY KEY, url TEXT NOT NULL, attempted_at TEXT NOT NULL,
                completed_at TEXT NOT NULL, outcome TEXT NOT NULL, attempt INTEGER NOT NULL,
                http_status INTEGER, error TEXT, latency_ms REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS provider_control (
                id INTEGER PRIMARY KEY CHECK(id=1), next_allowed_at REAL NOT NULL
            );
            INSERT OR IGNORE INTO provider_control VALUES(1,0);
            CREATE TABLE IF NOT EXISTS source_observations (
                url TEXT NOT NULL, content_hash TEXT NOT NULL, observed_at TEXT NOT NULL,
                PRIMARY KEY(url,content_hash,observed_at)
            );
            INSERT OR IGNORE INTO source_observations
                SELECT url,content_hash,first_fetched_at FROM source_objects;
            INSERT OR IGNORE INTO source_observations
                SELECT url,data_version,last_successful_fetch FROM resources
                WHERE data_version IS NOT NULL;
        """)

    def close(self) -> None:
        self.db.close()

    def cooldown(self) -> float:
        return float(
            self.db.execute("SELECT next_allowed_at FROM provider_control WHERE id=1").fetchone()[0]
        )

    def defer(self, until: float) -> None:
        with self.db:
            self.db.execute(
                "UPDATE provider_control SET next_allowed_at=MAX(next_allowed_at,?) WHERE id=1",
                (until,),
            )

    def clear_artifacts(self, filing: Filing) -> None:
        with self.db:
            self.db.execute(
                "UPDATE filings SET raw_hash=NULL,parsed_hash=NULL,parser_version=NULL "
                "WHERE cik=? AND accession=?",
                (filing.cik, filing.accession_number),
            )

    def save(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        target = self.raw_directory / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("Stored source checksum mismatch")
            return digest
        fd, temporary = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return digest

    def read(self, digest: str) -> bytes:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid source hash")
        content = (self.raw_directory / digest[:2] / digest).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("Stored source checksum mismatch")
        return content

    def discover(self, filing: Filing) -> bool:
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO filings(cik,accession,metadata,updated_at) VALUES(?,?,?,?)",
                (filing.cik, filing.accession_number, filing.model_dump_json(), now()),
            )
        return cursor.rowcount == 1

    def filing_state(self, filing: Filing) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM filings WHERE cik=? AND accession=?",
            (filing.cik, filing.accession_number),
        ).fetchone()
        if row is None:
            raise ValueError("Filing has not been discovered")
        return dict(row)

    def stage(
        self,
        filing: Filing,
        stage: str,
        *,
        error: str | None = None,
        raw_hash: str | None = None,
        parsed_hash: str | None = None,
        parser_version: str | None = None,
    ) -> None:
        with self.db:
            self.db.execute(
                """UPDATE filings SET stage=?,error=?,raw_hash=COALESCE(?,raw_hash),
                parsed_hash=COALESCE(?,parsed_hash),parser_version=COALESCE(?,parser_version),
                attempts=attempts+?,updated_at=? WHERE cik=? AND accession=?""",
                (
                    stage,
                    error,
                    raw_hash,
                    parsed_hash,
                    parser_version,
                    int(stage == "processing"),
                    now(),
                    filing.cik,
                    filing.accession_number,
                ),
            )

    def attempted(self, url: str) -> str:
        timestamp = now()
        with self.db:
            self.db.execute(
                """INSERT INTO resources(url,last_attempted_fetch) VALUES(?,?)
                ON CONFLICT(url) DO UPDATE
                SET last_attempted_fetch=excluded.last_attempted_fetch""",
                (url, timestamp),
            )
        return timestamp

    def fetched(
        self,
        url: str,
        attempted: str,
        attempt: int,
        latency: float,
        *,
        digest: str | None = None,
        source_timestamp: str | None = None,
        http_status: int | None = None,
        error: str | None = None,
    ) -> None:
        timestamp = now()
        with self.db:
            self.db.execute(
                "INSERT INTO fetch_logs VALUES(NULL,?,?,?,?,?,?,?,?)",
                (
                    url,
                    attempted,
                    timestamp,
                    "failed" if error else "success",
                    attempt,
                    http_status,
                    error,
                    latency,
                ),
            )
            if error:
                self.db.execute(
                    """UPDATE resources SET error_message=?,status=CASE
                    WHEN data_version IS NULL THEN 'UNAVAILABLE' ELSE 'STALE' END WHERE url=?""",
                    (error, url),
                )
            else:
                self.db.execute(
                    "INSERT OR IGNORE INTO source_observations VALUES(?,?,?)",
                    (url, digest, timestamp),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO source_objects"
                    "(url,content_hash,source_timestamp,first_fetched_at) VALUES(?,?,?,?)",
                    (url, digest, source_timestamp, timestamp),
                )
                self.db.execute(
                    """UPDATE resources SET last_successful_fetch=?,source_timestamp=?,
                    data_version=?,status='RECENT',error_message=NULL WHERE url=?""",
                    (timestamp, source_timestamp, digest, url),
                )

    def invalidate(self, url: str, error: str) -> None:
        """A fetched payload may still fail semantic validation downstream."""
        with self.db:
            self.db.execute(
                "UPDATE resources SET status='UNAVAILABLE',error_message=? WHERE url=?",
                (error, url),
            )

    def summary(self, stale_after_seconds: int) -> dict[str, Any]:
        resources = [dict(row) for row in self.db.execute("SELECT * FROM resources ORDER BY url")]
        for resource in resources:
            last = resource["last_successful_fetch"]
            if resource["status"] == "RECENT" and last:
                age = (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds()
                if age > stale_after_seconds:
                    resource["status"] = "STALE"
        stages = dict(
            self.db.execute("SELECT stage,COUNT(*) FROM filings GROUP BY stage").fetchall()
        )
        return {"stages": stages, "resources": resources}

    def save_json(self, value: dict[str, Any]) -> str:
        return self.save(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))
