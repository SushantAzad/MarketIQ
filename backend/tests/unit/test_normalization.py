from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.services.normalization import (
    normalize_companyfacts,
    normalize_entry,
    period_basis,
    reported_availability,
)


def entry() -> dict[str, object]:
    return {
        "start": "2024-01-01",
        "end": "2024-12-31",
        "val": Decimal("9007199254740993.123"),
        "accn": "0000000001-25-000001",
        "form": "10-K",
        "filed": "2025-02-01",
        "fy": 2025,
        "fp": "FY",
    }


def test_exact_decimal_and_filing_year_is_not_fact_period() -> None:
    result = normalize_entry("us-gaap", "Revenues", "USD", entry(), "/test")
    assert result.value == Decimal("9007199254740993.123")
    assert result.end.year == 2024
    assert result.filing_fiscal_year == 2025
    assert result.basis == "annual"


@pytest.mark.parametrize(
    "start,end,basis",
    [
        (None, "2024-12-31", "instant"),
        ("2024-01-01", "2024-03-31", "quarter"),
        ("2024-01-01", "2024-06-30", "ytd_6m"),
        ("2024-01-01", "2024-09-30", "ytd_9m"),
        ("2024-01-01", "2024-12-31", "annual"),
        ("2024-01-01", "2024-01-31", "duration_other"),
    ],
)
def test_period_bases(start: str | None, end: str, basis: str) -> None:
    assert (
        period_basis(date.fromisoformat(start) if start else None, date.fromisoformat(end)) == basis
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("val", float("nan")),
        ("val", Decimal("NaN")),
        ("val", True),
        ("val", "100"),
        ("start", "2025-01-01"),
        ("end", "2026-01-01"),
        ("accn", "../bad"),
        ("fy", True),
        ("fy", 1600),
        ("fp", 9),
        ("form", ""),
        ("dimensions", {}),
    ],
)
def test_invalid_financial_values_rejected(field: str, value: object) -> None:
    data = entry()
    data[field] = value
    with pytest.raises(ValueError):
        normalize_entry("us-gaap", "Revenues", "USD", data, "/test")


def test_date_only_availability_uses_eastern_day_boundary() -> None:
    actual, basis = reported_availability(date(2025, 6, 1), None)
    assert actual == datetime(2025, 6, 2, 4, tzinfo=UTC)
    assert basis == "next_eastern_midnight"
    assert reported_availability(date(2025, 1, 1), None)[0].hour == 5
    timestamp = datetime(2025, 1, 1, 16, tzinfo=UTC)
    assert reported_availability(date(2025, 1, 1), timestamp) == (timestamp, "sec_acceptance")


def test_rejections_have_source_pointer_without_fabricated_values() -> None:
    payload = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [entry(), {"val": "bad"}]}}}}}
    accepted, rejected = normalize_companyfacts(payload)
    assert len(accepted) == 1
    assert rejected == [
        {
            "source_locator": "/facts/us-gaap/Revenues/units/USD/1",
            "reason": "invalid_fact_context_or_value",
        }
    ]


def test_unit_pointer_escapes_slashes() -> None:
    payload = {
        "facts": {"us-gaap": {"EarningsPerShareBasic": {"units": {"USD/shares": [entry()]}}}}
    }
    accepted, _ = normalize_companyfacts(payload)
    assert "USD~1shares" in accepted[0].locator
