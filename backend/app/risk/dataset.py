"""Build only observable feature/forward-label pairs; never backdate today's snapshots."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any

import sqlalchemy as sa

from app.core.config import Settings
from app.database import schema as db
from app.services.financial_query import financial_value
from app.services.normalization import fingerprint

TARGET = "next_fiscal_year_negative_operating_cash_flow"
FEATURES = ("net_margin", "operating_margin", "current_ratio", "liabilities_assets", "ocf_assets")
VERSION = "observable-cashflow-dataset-v1"


def audit(engine: sa.Engine, settings: Settings) -> dict[str, Any]:
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    pending = []
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        candidates = (
            connection.execute(
                sa.select(
                    db.companies.c.cik,
                    db.securities.c.ticker,
                    db.snapshot_observations.c.observed_at,
                )
                .join(db.securities, db.securities.c.company_id == db.companies.c.id)
                .join(
                    db.normalization_runs, db.normalization_runs.c.company_id == db.companies.c.id
                )
                .join(
                    db.snapshot_observations,
                    db.snapshot_observations.c.source_id == db.normalization_runs.c.source_id,
                )
                .where(db.snapshot_observations.c.observed_at <= now)
                .distinct()
            )
            .mappings()
            .all()
        )
        seen = set()
        for candidate in candidates:
            cik, ticker, cutoff = candidate["cik"], candidate["ticker"], candidate["observed_at"]
            key = (cik, cutoff)
            if key in seen:
                continue
            seen.add(key)
            # Outcome must start after the feature cutoff, not merely be filed after it.
            labels = (
                connection.execute(
                    sa.select(db.financial_facts.c.period_start, db.financial_facts.c.period_end)
                    .join(db.companies, db.companies.c.id == db.financial_facts.c.company_id)
                    .where(
                        db.companies.c.cik == cik,
                        db.financial_facts.c.concept
                        == "NetCashProvidedByUsedInOperatingActivities",
                        db.financial_facts.c.period_basis == "annual",
                        db.financial_facts.c.unit == "USD",
                        db.financial_facts.c.period_start > cutoff.date(),
                        db.financial_facts.c.period_start <= cutoff.date() + timedelta(days=385),
                        db.financial_facts.c.reported_available_at <= now,
                    )
                    .order_by(db.financial_facts.c.period_start)
                    .distinct()
                )
                .mappings()
                .all()
            )
            if not labels:
                pending.append(
                    {
                        "cik": cik,
                        "cutoff": cutoff.isoformat(),
                        "reason": "no_observed_forward_annual_label",
                    }
                )
                continue
            label = financial_value(
                connection,
                ticker,
                "operating_cash_flow",
                basis="annual",
                period_end=labels[0]["period_end"],
                as_of=now,
            )
            if label["value"] is None or label["period_start"] <= cutoff.date():
                pending.append({"cik": cik, "reason": "unusable_forward_label"})
                continue
            revenue = financial_value(connection, ticker, "revenue", basis="annual", as_of=cutoff)
            if revenue["value"] is None:
                pending.append({"cik": cik, "reason": "no_observable_annual_features"})
                continue
            end = revenue["period_end"]
            facts = {"revenue": revenue}
            for metric in [
                "net_income",
                "operating_income",
                "assets",
                "liabilities",
                "current_assets",
                "current_liabilities",
                "operating_cash_flow",
            ]:
                facts[metric] = financial_value(
                    connection,
                    ticker,
                    metric,
                    basis="instant"
                    if metric in {"assets", "liabilities", "current_assets", "current_liabilities"}
                    else "annual",
                    period_end=end,
                    as_of=cutoff,
                )

            def ratio(
                numerator: str, denominator: str, sources: dict[str, Any] = facts
            ) -> float | None:
                a, b = sources[numerator], sources[denominator]
                if a["value"] is None or b["value"] is None or Decimal(b["value"]) <= 0:
                    return None
                if (
                    a.get("period_start")
                    and b.get("period_start")
                    and a["period_start"] != b["period_start"]
                ):
                    return None
                with localcontext() as context:
                    context.prec = 38
                    return float(Decimal(a["value"]) / Decimal(b["value"]))

            features = dict(
                zip(
                    FEATURES,
                    [
                        ratio("net_income", "revenue"),
                        ratio("operating_income", "revenue"),
                        ratio("current_assets", "current_liabilities"),
                        ratio("liabilities", "assets"),
                        ratio("operating_cash_flow", "assets"),
                    ],
                    strict=True,
                )
            )
            rows.append(
                {
                    "cik": cik,
                    "cutoff": cutoff.isoformat(),
                    "features": features,
                    "feature_sources": facts,
                    "label": int(Decimal(label["value"]) < 0),
                    "label_start": str(label["period_start"]),
                    "label_end": str(label["period_end"]),
                    "label_available_at": max(
                        label["reported_available_at"], label["source"]["first_observed_at"]
                    ).isoformat(),
                    "label_source": label,
                }
            )
        summary = connection.execute(
            sa.select(
                sa.func.min(db.snapshot_observations.c.observed_at),
                sa.func.max(db.snapshot_observations.c.observed_at),
            )
        ).one()
        issuers = connection.scalar(sa.select(sa.func.count()).select_from(db.companies))
    cohort_path = settings.raw_storage_path.parent / "risk/cohort.json"
    cohort = json.loads(cohort_path.read_text(encoding="utf-8")) if cohort_path.exists() else {}
    eligible = {r["cik"] for r in cohort.get("candidates", []) if r["exclusion"] is None}
    unique = {}
    for row in sorted(rows, key=lambda r: r["cutoff"]):
        if row["cik"] in eligible:
            unique[(row["cik"], row["label_start"])] = row
    accepted = list(unique.values())
    report = {
        "version": VERSION,
        "target": TARGET,
        "feature_order": FEATURES,
        "as_of": now.isoformat(),
        "snapshot_observation_range": [str(d) for d in summary],
        "imported_issuers": issuers,
        "feature_cutoffs_examined": len(seen),
        "rows": accepted,
        "pending": pending,
        "unclassified_rows_excluded": sum(r["cik"] not in eligible for r in rows),
        "duplicate_outcomes_excluded": sum(r["cik"] in eligible for r in rows) - len(accepted),
        "cohort": cohort,
        "inference_available": False,
    }
    report["dataset_hash"] = fingerprint(report)
    target = settings.raw_storage_path.parent / "risk/dataset.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, default=str, indent=2), encoding="utf-8")
    return report
