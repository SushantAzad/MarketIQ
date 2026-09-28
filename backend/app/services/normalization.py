"""Lossless values and explicit periods from SEC companyfacts (no derived ratios)."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

VERSION = "companyfacts-v1"
METRIC_CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "net_income": ("NetIncomeLoss",),
    "operating_income": ("OperatingIncomeLoss",),
    "gross_profit": ("GrossProfit",),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "equity": ("StockholdersEquity",),
    "current_assets": ("AssetsCurrent",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "cash": ("CashAndCashEquivalentsAtCarryingValue",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capital_expenditures": ("PaymentsToAcquirePropertyPlantAndEquipment",),
}
BALANCE_CONCEPTS = {
    c
    for m in ("assets", "liabilities", "equity", "current_assets", "current_liabilities", "cash")
    for c in METRIC_CONCEPTS[m]
}
CASH_CONCEPTS = {
    c for m in ("operating_cash_flow", "capital_expenditures") for c in METRIC_CONCEPTS[m]
}
INCOME_CONCEPTS = {
    c
    for m in ("revenue", "net_income", "operating_income", "gross_profit")
    for c in METRIC_CONCEPTS[m]
}


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def period_basis(start: date | None, end: date) -> str:
    if start is None:
        return "instant"
    days = (end - start).days + 1
    if days <= 0:
        raise ValueError("reversed_period")
    for lower, upper, basis in (
        (70, 110, "quarter"),
        (160, 210, "ytd_6m"),
        (250, 300, "ytd_9m"),
        (330, 385, "annual"),
    ):
        if lower <= days <= upper:
            return basis
    return "duration_other"


def statement_type(concept: str) -> str:
    if concept in BALANCE_CONCEPTS:
        return "balance_sheet"
    if concept in CASH_CONCEPTS:
        return "cash_flow"
    return "income" if concept in INCOME_CONCEPTS else "unclassified"


@dataclass(frozen=True)
class NormalizedFact:
    taxonomy: str
    concept: str
    unit: str
    value: Decimal
    accession: str
    form: str
    filed: date
    start: date | None
    end: date
    basis: str
    filing_fiscal_year: int | None
    filing_fiscal_period: str | None
    locator: str
    fingerprint: str


def normalize_entry(
    taxonomy: str,
    concept: str,
    unit: str,
    entry: dict[str, Any],
    locator: str,
) -> NormalizedFact:
    if not isinstance(entry, dict):
        raise ValueError("invalid_entry")
    if any(key in entry for key in ("dimensions", "segment")):
        raise ValueError("dimensional_context_not_supported")
    raw_value = entry["val"]
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, Decimal)):
        raise ValueError("value_must_be_exact_number")
    value = Decimal(raw_value)
    if not value.is_finite() or abs(value.adjusted()) > 1000:
        raise ValueError("invalid_numeric_range")
    accession = entry["accn"]
    if not isinstance(accession, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
        raise ValueError("invalid_accession")
    form = entry["form"]
    if not isinstance(form, str) or not re.fullmatch(r"[A-Z0-9/-]{1,12}", form):
        raise ValueError("invalid_form")
    if not unit or len(unit) > 80 or len(taxonomy) > 80 or len(concept) > 256:
        raise ValueError("invalid_concept_or_unit")
    end = date.fromisoformat(entry["end"])
    start = date.fromisoformat(entry["start"]) if entry.get("start") else None
    filed = date.fromisoformat(entry["filed"])
    if end > filed:
        raise ValueError("fact_period_after_filing")
    fy = entry.get("fy")
    fp = entry.get("fp")
    if fy is not None and (
        isinstance(fy, bool) or not isinstance(fy, int) or not 1900 <= fy <= 2200
    ):
        raise ValueError("invalid_filing_fiscal_year")
    if fp is not None and (not isinstance(fp, str) or len(fp) > 12):
        raise ValueError("invalid_filing_fiscal_period")
    basis = period_basis(start, end)
    identity = [
        VERSION,
        taxonomy,
        concept,
        unit,
        str(value),
        accession,
        form,
        filed,
        start,
        end,
        fy,
        fp,
    ]
    return NormalizedFact(
        taxonomy,
        concept,
        unit,
        value,
        accession,
        form,
        filed,
        start,
        end,
        basis,
        fy,
        fp,
        locator,
        fingerprint(identity),
    )


def reported_availability(filed: date, accepted_at: datetime | None) -> tuple[datetime, str]:
    if accepted_at is not None:
        if accepted_at.tzinfo is None:
            raise ValueError("accepted_at requires timezone")
        return accepted_at.astimezone(UTC), "sec_acceptance"
    # Date-only filing metadata cannot establish intraday availability. Use next Eastern midnight.
    cutoff = datetime.combine(filed + timedelta(days=1), time.min, ZoneInfo("America/New_York"))
    return cutoff.astimezone(UTC), "next_eastern_midnight"


def normalize_companyfacts(
    payload: dict[str, Any],
) -> tuple[list[NormalizedFact], list[dict[str, str]]]:
    if not isinstance(payload.get("facts"), dict):
        raise ValueError("companyfacts requires a facts object")
    result = []
    rejected = []
    for taxonomy, concepts in payload["facts"].items():
        if not isinstance(concepts, dict):
            raise ValueError("invalid taxonomy object")
        for concept, detail in concepts.items():
            if not isinstance(detail, dict) or not isinstance(detail.get("units"), dict):
                raise ValueError("invalid concept units")
            for unit, entries in detail["units"].items():
                if not isinstance(entries, list):
                    raise ValueError("invalid unit observations")
                for index, entry in enumerate(entries):
                    tokens = ["facts", taxonomy, concept, "units", unit, str(index)]
                    locator = "/" + "/".join(
                        t.replace("~", "~0").replace("/", "~1") for t in tokens
                    )
                    try:
                        result.append(normalize_entry(taxonomy, concept, unit, entry, locator))
                    except (ValueError, KeyError, TypeError):
                        # Preserve the exact input in the raw object; don't echo untrusted values.
                        rejected.append(
                            {"source_locator": locator, "reason": "invalid_fact_context_or_value"}
                        )
    return result, rejected
