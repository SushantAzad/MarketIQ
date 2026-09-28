"""Transactional, replay-safe import of a verified SEC snapshot into PostgreSQL."""

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from app.database import schema as db
from app.providers.sec.models import normalize_cik
from app.services.normalization import (
    VERSION,
    fingerprint,
    normalize_companyfacts,
    reported_availability,
    statement_type,
)


def stable_id(kind: str, key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"marketiq:{kind}:{key}")


def put(connection: sa.Connection, table: sa.Table, rows: list[dict[str, Any]]) -> None:
    # Bulk batches stay below PostgreSQL's parameter count ceiling.
    for offset in range(0, len(rows), 500):
        connection.execute(
            insert(table).values(rows[offset : offset + 500]).on_conflict_do_nothing()
        )


def import_snapshot(
    connection: sa.Connection,
    content: bytes,
    source: dict[str, Any],
    *,
    filing_metadata: dict[str, dict[str, Any]] | None = None,
    tickers: list[str] | None = None,
) -> dict[str, Any]:
    """Caller owns transaction; no partial company import commits on failure."""
    content_hash = hashlib.sha256(content).hexdigest()
    if source["content_hash"] != content_hash:
        raise ValueError("Source checksum mismatch")
    fetched_at = datetime.fromisoformat(source["first_fetched_at"])
    if fetched_at.tzinfo is None:
        raise ValueError("Source fetch timestamp requires timezone")
    payload = json.loads(content, parse_float=Decimal)
    cik = normalize_cik(payload["cik"])
    expected_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    if source["source_url"] != expected_url:
        raise ValueError("Source URL/issuer mismatch")
    name = payload.get("entityName")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Missing company name")
    company_id = stable_id("company", cik)
    source_id = stable_id("source", expected_url + ":" + content_hash)
    # Serialize imports for this issuer. Different issuers can import concurrently.
    connection.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(cik)})
    put(connection, db.companies, [{"id": company_id, "cik": cik, "legal_name": name}])
    put(
        connection,
        db.source_objects,
        [
            {
                "id": source_id,
                "provider": "SEC EDGAR",
                "source_url": expected_url,
                "content_hash": content_hash,
                "object_key": f"{content_hash[:2]}/{content_hash}",
                "first_fetched_at": fetched_at,
                "source_timestamp": source.get("source_timestamp"),
            }
        ],
    )
    observed_at = datetime.fromisoformat(source.get("observed_at", source["first_fetched_at"]))
    if observed_at.tzinfo is None or observed_at < fetched_at:
        raise ValueError("Invalid observation timestamp")
    put(
        connection, db.snapshot_observations, [{"source_id": source_id, "observed_at": observed_at}]
    )
    put(
        connection,
        db.securities,
        [
            {
                "id": stable_id("security", ticker),
                "company_id": company_id,
                "ticker": ticker,
                "provider": "SEC EDGAR",
            }
            for ticker in (tickers or [])
        ],
    )
    existing = (
        connection.execute(
            sa.select(db.normalization_runs).where(
                db.normalization_runs.c.source_id == source_id,
                db.normalization_runs.c.normalization_version == VERSION,
            )
        )
        .mappings()
        .first()
    )
    metadata_groups: dict[UUID, list[str]] = {}
    for accession, details in (filing_metadata or {}).items():
        if details.get("metadata_source_id"):
            metadata_groups.setdefault(details["metadata_source_id"], []).append(accession)
    for metadata_source_id, accessions in metadata_groups.items():
        connection.execute(
            db.filings.update()
            .where(
                db.filings.c.company_id == company_id,
                db.filings.c.accession_number.in_(accessions),
            )
            .values(metadata_source_id=metadata_source_id)
        )
    if existing:
        return {
            "cik": cik,
            "status": "unchanged",
            "accepted": existing["accepted_count"],
            "rejected": existing["rejected_count"],
        }
    facts, rejected = normalize_companyfacts(payload)
    filings: dict[UUID, dict[str, Any]] = {}
    statements: dict[UUID, dict[str, Any]] = {}
    fact_rows: dict[UUID, dict[str, Any]] = {}
    observations = []
    for fact in facts:
        filing_id = stable_id("filing", cik + ":" + fact.accession)
        details = (filing_metadata or {}).get(fact.accession, {})
        accepted_at = (
            datetime.fromisoformat(details["accepted_at"]) if details.get("accepted_at") else None
        )
        available_at, availability_basis = reported_availability(fact.filed, accepted_at)
        accession_path = fact.accession.replace("-", "")
        archive = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_path}/"
        # Unknown primary filenames are not guessed; use the filing index.
        url = archive + details.get("primary_document", f"{fact.accession}-index.html")
        filings[filing_id] = {
            "id": filing_id,
            "company_id": company_id,
            "accession_number": fact.accession,
            "form_type": fact.form,
            "filing_date": fact.filed,
            "accepted_at": accepted_at,
            "report_period": details.get("report_period"),
            "source_url": url,
            "metadata_source_id": details.get("metadata_source_id", source_id),
        }
        context_hash = fingerprint([fact.start, fact.end, fact.unit])
        statement = statement_type(fact.concept) if fact.taxonomy == "us-gaap" else "unclassified"
        statement_id = stable_id("statement", f"{filing_id}:{statement}:{context_hash}:{VERSION}")
        statements[statement_id] = {
            "id": statement_id,
            "filing_id": filing_id,
            "statement_type": statement,
            "period_start": fact.start,
            "period_end": fact.end,
            "period_basis": fact.basis,
            "unit": fact.unit,
            "context_hash": context_hash,
            "normalization_version": VERSION,
        }
        fact_id = stable_id("fact", cik + ":" + fact.fingerprint)
        fact_rows[fact_id] = {
            "id": fact_id,
            "company_id": company_id,
            "filing_id": filing_id,
            "statement_id": statement_id,
            "taxonomy": fact.taxonomy,
            "concept": fact.concept,
            "value": fact.value,
            "unit": fact.unit,
            "period_start": fact.start,
            "period_end": fact.end,
            "period_basis": fact.basis,
            "filing_fiscal_year": fact.filing_fiscal_year,
            "filing_fiscal_period": fact.filing_fiscal_period,
            "reported_available_at": available_at,
            "availability_basis": availability_basis,
            "fingerprint": fingerprint([cik, fact.fingerprint]),
            "normalization_version": VERSION,
        }
        observations.append(
            {"fact_id": fact_id, "source_id": source_id, "source_locator": fact.locator}
        )
    put(connection, db.filings, list(filings.values()))
    put(connection, db.financial_statements, list(statements.values()))
    put(connection, db.financial_facts, list(fact_rows.values()))
    put(connection, db.fact_sources, observations)
    run_id = stable_id("run", f"{source_id}:{VERSION}")
    status = "partial" if rejected else "complete"
    put(
        connection,
        db.normalization_runs,
        [
            {
                "id": run_id,
                "company_id": company_id,
                "source_id": source_id,
                "normalization_version": VERSION,
                "accepted_count": len(facts),
                "rejected_count": len(rejected),
                "status": status,
            }
        ],
    )
    put(
        connection,
        db.rejected_facts,
        [
            {
                "id": stable_id("rejected", f"{run_id}:{item['source_locator']}"),
                "run_id": run_id,
                **item,
            }
            for item in rejected
        ],
    )
    return {"cik": cik, "status": status, "accepted": len(facts), "rejected": len(rejected)}
