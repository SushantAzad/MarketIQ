"""Period-explicit reads with provenance and conservative point-in-time semantics."""

from datetime import UTC, date, datetime
from typing import Any

import sqlalchemy as sa

from app.database import schema as db
from app.services.normalization import METRIC_CONCEPTS, VERSION


def financial_value(
    connection: sa.Connection,
    ticker: str,
    metric: str,
    *,
    basis: str,
    unit: str = "USD",
    period_end: date | None = None,
    as_of: datetime | None = None,
    freshness_seconds: int = 600,
) -> dict[str, Any]:
    if metric not in METRIC_CONCEPTS:
        raise ValueError("Unknown financial metric")
    if basis not in {"instant", "quarter", "annual", "ytd_6m", "ytd_9m", "duration_other"}:
        raise ValueError("Unknown period basis")
    cutoff = as_of or datetime.now(UTC)
    if cutoff.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    company_id = connection.execute(
        sa.select(db.securities.c.company_id).where(db.securities.c.ticker == ticker.upper())
    ).scalar_one_or_none()
    empty: dict[str, Any] = {
        "ticker": ticker.upper(),
        "metric": metric,
        "basis": basis,
        "unit": unit,
        "value": None,
        "status": "UNAVAILABLE",
    }
    if company_id is None:
        return {**empty, "reason": "Company has not been imported"}
    # Restrict facts to the latest snapshot that was actually observed by the cutoff.
    # A removed/corrected observation in a newer snapshot must not leak from an older one.
    snapshot = (
        connection.execute(
            sa.select(
                db.source_objects,
                db.normalization_runs.c.status.label("normalization_status"),
                db.snapshot_observations.c.observed_at,
            )
            .join(
                db.normalization_runs, db.normalization_runs.c.source_id == db.source_objects.c.id
            )
            .join(
                db.snapshot_observations,
                db.snapshot_observations.c.source_id == db.source_objects.c.id,
            )
            .where(
                db.normalization_runs.c.company_id == company_id,
                db.normalization_runs.c.normalization_version == VERSION,
                db.snapshot_observations.c.observed_at <= cutoff,
            )
            .order_by(db.snapshot_observations.c.observed_at.desc(), db.source_objects.c.id)
            .limit(1)
        )
        .mappings()
        .first()
    )
    if snapshot is None:
        return {**empty, "reason": "No source snapshot observed by the requested cutoff"}
    concepts = METRIC_CONCEPTS[metric]
    priority = sa.case(
        {concept: index for index, concept in enumerate(concepts)},
        value=db.financial_facts.c.concept,
        else_=len(concepts),
    )
    query = (
        sa.select(
            db.financial_facts,
            db.filings.c.accession_number,
            db.filings.c.filing_date,
            db.filings.c.source_url.label("filing_url"),
            db.fact_sources.c.source_locator,
        )
        .join(db.fact_sources, db.fact_sources.c.fact_id == db.financial_facts.c.id)
        .join(db.filings, db.filings.c.id == db.financial_facts.c.filing_id)
        .where(
            db.financial_facts.c.company_id == company_id,
            db.financial_facts.c.taxonomy == "us-gaap",
            db.financial_facts.c.normalization_version == VERSION,
            db.financial_facts.c.concept.in_(concepts),
            db.financial_facts.c.unit == unit,
            db.financial_facts.c.period_basis == basis,
            db.financial_facts.c.period_end <= cutoff.date(),
            db.financial_facts.c.reported_available_at <= cutoff,
            db.fact_sources.c.source_id == snapshot["id"],
            db.filings.c.form_type.in_(["10-K", "10-K/A", "10-Q", "10-Q/A"]),
        )
    )
    if period_end is not None:
        query = query.where(db.financial_facts.c.period_end == period_end)
    rows = (
        connection.execute(
            query.order_by(
                db.financial_facts.c.period_end.desc(),
                db.financial_facts.c.reported_available_at.desc(),
                priority,
                db.financial_facts.c.id,
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return {**empty, "reason": "No matching reported fact for this period, unit, and cutoff"}
    selected = rows[0]
    peers = [
        row
        for row in rows
        if (row["period_end"], row["reported_available_at"], row["concept"])
        == (selected["period_end"], selected["reported_available_at"], selected["concept"])
    ]
    if len({(row["value"], row["period_start"]) for row in peers}) > 1:
        return {**empty, "reason": "Conflicting same-period observations require review"}
    state = (
        connection.execute(
            sa.select(db.data_source_state).where(
                db.data_source_state.c.source_url == snapshot["source_url"]
            )
        )
        .mappings()
        .first()
    )
    current_time = datetime.now(UTC)
    verified = state["last_successful_fetch"] if state else snapshot["first_fetched_at"]
    status = "RECENT" if (current_time - verified).total_seconds() <= freshness_seconds else "STALE"
    if state and (state["status"] != "RECENT" or state["data_version"] != snapshot["content_hash"]):
        status = "STALE"
    return {
        "ticker": ticker.upper(),
        "metric": metric,
        "value": str(selected["value"]),
        "unit": selected["unit"],
        "basis": selected["period_basis"],
        "period_start": selected["period_start"],
        "period_end": selected["period_end"],
        "filing_fiscal_year": selected["filing_fiscal_year"],
        "filing_fiscal_period": selected["filing_fiscal_period"],
        "status": status,
        "data_kind": "REPORTED_FINANCIAL_FACT",
        "normalization_status": snapshot["normalization_status"],
        "as_of": cutoff,
        "as_of_semantics": "observed_snapshot_and_reported_availability",
        "reported_available_at": selected["reported_available_at"],
        "availability_basis": selected["availability_basis"],
        "source": {
            "provider": "SEC EDGAR",
            "fact_id": selected["id"],
            "concept": selected["concept"],
            "accession_number": selected["accession_number"],
            "filing_url": selected["filing_url"],
            "snapshot_url": snapshot["source_url"],
            "source_locator": selected["source_locator"],
            "content_hash": snapshot["content_hash"],
            "first_observed_at": snapshot["first_fetched_at"],
            "last_successful_fetch": verified,
            "last_attempted_fetch": state["last_attempted_fetch"] if state else None,
            "source_timestamp": snapshot["source_timestamp"],
            "error_message": state["error_message"] if state else None,
        },
    }
