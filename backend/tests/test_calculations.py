from datetime import UTC, date, datetime
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from app.services.calculation_query import CalculationRequest
from app.services.calculations import calculate


def fact(metric, value, start="2024-01-01", end="2024-12-31", basis="annual", **updates):
    return {
        "metric": metric,
        "value": str(value),
        "ticker": "TEST",
        "unit": "USD",
        "period_start": date.fromisoformat(start) if start else None,
        "period_end": date.fromisoformat(end),
        "basis": basis,
        "status": "STALE",
        "as_of": datetime(2026, 1, 1, tzinfo=UTC),
        "source": {"fact_id": "test-only"},
        **updates,
    }


def instant(metric, value, end="2024-12-31"):
    return fact(metric, value, start=None, end=end, basis="instant")


@pytest.mark.parametrize(
    "metric,numerator",
    [
        ("gross_margin", "gross_profit"),
        ("operating_margin", "operating_income"),
        ("net_margin", "net_income"),
    ],
)
def test_margins_are_percent_with_lineage(metric, numerator):
    inputs = {numerator: fact(numerator, 25), "revenue": fact("revenue", 200)}
    result = calculate(metric, inputs)
    assert Decimal(result["value"]) == Decimal("12.5")
    assert result["unit"] == "percent" and result["operands"] == inputs
    assert result["freshness"] == "STALE"


@pytest.mark.parametrize("value", [0, -20])
def test_nonpositive_denominator(value):
    result = calculate(
        "net_margin", {"net_income": fact("net_income", 10), "revenue": fact("revenue", value)}
    )
    assert result["reason"] == "nonpositive_denominator" and result["value"] is None


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"period_start": date(2024, 1, 2)}, "incompatible_periods"),
        ({"unit": "EUR"}, "incompatible_units"),
        ({"ticker": "OTHER"}, "incompatible_issuers"),
        ({"value": None}, "missing_input"),
        ({"source": {}}, "missing_lineage"),
        ({"as_of": datetime(2025, 1, 1, tzinfo=UTC)}, "incompatible_cutoffs"),
        ({"value": 1.1}, "inexact_input"),
        ({"value": "NaN"}, "invalid_numeric_range"),
        ({"value": "bad"}, "invalid_operand"),
        ({"basis": "quarter"}, "invalid_period_basis"),
        ({"metric": "gross_profit"}, "incompatible_metrics"),
    ],
)
def test_refuses_incompatible_operands(updates, reason):
    inputs = {"net_income": fact("net_income", 10) | updates, "revenue": fact("revenue", 100)}
    assert calculate("net_margin", inputs)["reason"] == reason


@pytest.mark.parametrize("name", ["growth", "yoy", "cagr"])
def test_growth_and_cagr(name):
    inputs = {
        "current": fact("revenue", 121),
        "previous": fact("revenue", 100, "2022-01-01", "2022-12-31"),
    }
    if name == "yoy":
        inputs["previous"] = fact("revenue", 100, "2023-01-01", "2023-12-31")
    result = calculate(name, inputs)
    assert abs(Decimal(result["value"]) - Decimal(10 if name == "cagr" else 21)) < Decimal("1e-30")
    for bad in [0, -100]:
        inputs["previous"]["value"] = str(bad)
        assert calculate(name, inputs)["reason"] == "nonpositive_growth_base"


def test_cagr_zero_endpoint_negative_endpoint_and_spacing():
    inputs = {
        "current": fact("revenue", 0),
        "previous": fact("revenue", 100, "2022-01-01", "2022-12-31"),
    }
    assert Decimal(calculate("cagr", inputs)["value"]) == -100
    inputs["current"]["value"] = "-1"
    assert calculate("cagr", inputs)["value"] is None
    assert calculate("yoy", inputs)["reason"] == "yoy_requires_one_year"
    inputs["current"] = fact("revenue", 100, "2024-04-01", "2025-03-31")
    assert calculate("cagr", inputs)["reason"] == "incompatible_annual_spacing"


def test_growth_does_not_mix_ytd_or_overlap():
    inputs = {
        "current": fact("revenue", 100),
        "previous": fact("revenue", 50, end="2024-06-30", basis="ytd_6m"),
    }
    assert calculate("growth", inputs)["reason"] == "incompatible_periods"
    inputs["previous"] = fact("revenue", 50, "2023-12-01", "2024-11-30")
    assert calculate("growth", inputs)["reason"] == "incompatible_periods"


def test_fcf_sign_and_large_exact_operands():
    inputs = {
        "operating_cash_flow": fact("operating_cash_flow", "9007199254740993.25"),
        "capital_expenditures": fact("capital_expenditures", "0.20"),
    }
    with localcontext() as context:
        context.prec = 5
        assert calculate("fcf", inputs)["value"] == "9007199254740993.05"
    inputs["capital_expenditures"]["value"] = "-1"
    assert calculate("fcf", inputs)["reason"] == "negative_capex_payment"


def test_ratios_and_debt_components():
    assert Decimal(
        calculate(
            "current_ratio",
            {
                "current_assets": instant("current_assets", 300),
                "current_liabilities": instant("current_liabilities", 200),
            },
        )["value"]
    ) == Decimal("1.5")
    inputs = {
        k: instant(k, v)
        for k, v in {
            "short_term_debt": 10,
            "current_long_term_debt": 20,
            "noncurrent_debt": 70,
            "equity": 50,
        }.items()
    }
    assert calculate("debt_equity", inputs)["value"] == "2"
    inputs["short_term_debt"]["value"] = None
    assert calculate("debt_equity", inputs)["reason"] == "missing_input"


@pytest.mark.parametrize("name,balance", [("roe", "equity"), ("roa", "assets")])
def test_return_uses_opening_and_closing_balances(name, balance):
    inputs = {
        "net_income": fact("net_income", 30),
        f"opening_{balance}": instant(balance, 100, "2023-12-31"),
        f"closing_{balance}": instant(balance, 200),
    }
    result = calculate(name, inputs)
    assert result["value"] == "20.0" and result["annualized"] is False
    inputs[f"opening_{balance}"]["period_end"] = date(2023, 12, 30)
    assert calculate(name, inputs)["reason"] == "incompatible_average_balance_periods"


def test_ebitda_explicit_components_and_no_zero_filling():
    inputs = {
        k: fact(k, v)
        for k, v in {
            "net_income": 100,
            "interest_expense": 10,
            "income_tax_expense": -5,
            "depreciation": 20,
            "amortization": 5,
        }.items()
    }
    assert calculate("ebitda", inputs)["value"] == "130"
    del inputs["amortization"]
    assert calculate("ebitda", inputs)["reason"] == "missing_or_unexpected_operands"


def test_discrete_quarter_preserves_both_ytd_operands():
    inputs = {
        "current": fact("operating_cash_flow", 80, end="2024-06-30", basis="ytd_6m"),
        "previous": fact("operating_cash_flow", 30, end="2024-03-31", basis="quarter"),
    }
    result = calculate("discrete_quarter", inputs)
    assert result["value"] == "50" and result["basis"] == "quarter"
    assert result["period_start"] == date(2024, 4, 1) and result["operands"] == inputs
    inputs["previous"]["period_start"] = date(2024, 1, 2)
    assert calculate("discrete_quarter", inputs)["reason"] == "incompatible_ytd_periods"


def test_request_rejects_ambiguous_periods_and_unknown_formulas():
    base = {"ticker": "TEST", "metric": "growth", "basis": "annual", "period_end": "2024-12-31"}
    for update in [
        {},
        {"comparison_end": "2025-12-31"},
        {"comparison_end": "2023-12-31", "as_of": "2025-01-01"},
        {"metric": "invented"},
        {"input_metric": "invented"},
    ]:
        with pytest.raises(ValidationError):
            CalculationRequest(**(base | update))
    with pytest.raises(ValueError):
        calculate("invented", {})
