"""Versioned Decimal arithmetic over sourced, period-explicit operands."""

from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, DecimalException, localcontext
from typing import Any

from app.services.normalization import period_basis

VERSION = "financial-formulas-v1"
FORMULAS = {
    "growth": "(current / previous - 1) * 100",
    "yoy": "(current / previous - 1) * 100; matching periods one fiscal year apart",
    "cagr": "((current / previous) ** (1 / years) - 1) * 100",
    "gross_margin": "gross_profit / revenue * 100",
    "operating_margin": "operating_income / revenue * 100",
    "net_margin": "net_income / revenue * 100",
    "fcf": "operating_cash_flow - capital_expenditures",
    "fcf_growth": "(current_fcf / previous_fcf - 1) * 100",
    "debt_equity": "(short_term_debt + current_long_term_debt + noncurrent_debt) / equity",
    "current_ratio": "current_assets / current_liabilities",
    "roe": "net_income / ((opening_equity + closing_equity) / 2) * 100",
    "roa": "net_income / ((opening_assets + closing_assets) / 2) * 100",
    "ebitda": "net_income + interest_expense + income_tax_expense + depreciation + amortization",
    "discrete_quarter": "current_ytd - previous_ytd; same fiscal start, adjacent YTD endpoints",
}
REQUIRED = {
    "growth": ("current", "previous"),
    "yoy": ("current", "previous"),
    "cagr": ("current", "previous"),
    "fcf_growth": ("current", "previous"),
    "gross_margin": ("gross_profit", "revenue"),
    "operating_margin": ("operating_income", "revenue"),
    "net_margin": ("net_income", "revenue"),
    "fcf": ("operating_cash_flow", "capital_expenditures"),
    "debt_equity": ("short_term_debt", "current_long_term_debt", "noncurrent_debt", "equity"),
    "current_ratio": ("current_assets", "current_liabilities"),
    "roe": ("net_income", "opening_equity", "closing_equity"),
    "roa": ("net_income", "opening_assets", "closing_assets"),
    "ebitda": (
        "net_income",
        "interest_expense",
        "income_tax_expense",
        "depreciation",
        "amortization",
    ),
    "discrete_quarter": ("current", "previous"),
}


def day(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def calculate(name: str, operands: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return an auditable result, including unavailable inputs; never invent a value."""
    if name not in FORMULAS:
        raise ValueError("Unknown formula")
    result: dict[str, Any] = {
        "metric": name,
        "data_kind": "CALCULATED_FINANCIAL_METRIC",
        "formula_version": VERSION,
        "formula": FORMULAS[name],
        "decimal_precision": 38,
        "rounding": "ROUND_HALF_EVEN",
        "status": "UNAVAILABLE",
        "value": None,
        "reason": None,
        "operands": operands,
        "unit": None,
        "upstream_refreshed": False,
    }

    def unavailable(reason: str) -> dict[str, Any]:
        return {**result, "reason": reason}

    if set(operands) != set(REQUIRED[name]):
        return unavailable("missing_or_unexpected_operands")
    rows = list(operands.values())
    if name not in {"growth", "yoy", "cagr", "fcf_growth", "discrete_quarter"}:
        if any(
            r.get("metric") != k.removeprefix("opening_").removeprefix("closing_")
            for k, r in operands.items()
        ):
            return unavailable("incompatible_metrics")
    elif name == "fcf_growth" and any(r.get("metric") != "fcf" for r in rows):
        return unavailable("incompatible_metrics")
    if any(r.get("value") is None or r.get("status") == "UNAVAILABLE" for r in rows):
        return unavailable("missing_input")
    if any(not (r.get("source", {}).get("fact_id") or r.get("operands")) for r in rows):
        return unavailable("missing_lineage")
    if len({r.get("ticker") for r in rows}) != 1 or not rows[0].get("ticker"):
        return unavailable("incompatible_issuers")
    if len({r.get("unit") for r in rows}) != 1 or not rows[0].get("unit"):
        return unavailable("incompatible_units")
    if rows[0]["unit"] not in {"USD", "EUR", "GBP", "JPY", "CAD", "CHF", "AUD", "CNY"}:
        return unavailable("unsupported_monetary_unit")
    if len({str(r.get("as_of")) for r in rows}) != 1 or rows[0].get("as_of") is None:
        return unavailable("incompatible_cutoffs")
    anchor = operands[REQUIRED[name][0]]
    result.update(
        ticker=anchor["ticker"],
        as_of=anchor["as_of"],
        period_start=anchor.get("period_start"),
        period_end=anchor["period_end"],
        basis=anchor["basis"],
    )
    try:
        # Float inputs have already lost decimal provenance; refuse them explicitly.
        if any(isinstance(r["value"], (float, bool)) for r in rows):
            return unavailable("inexact_input")
        values = {k: Decimal(r["value"]) for k, r in operands.items()}
        if any(not v.is_finite() or abs(v.adjusted()) > 1000 for v in values.values()):
            return unavailable("invalid_numeric_range")
        periods = {(r.get("period_start"), r["period_end"], r["basis"]) for r in rows}
        for r in rows:
            start = day(r["period_start"]) if r.get("period_start") else None
            if period_basis(start, day(r["period_end"])) != r["basis"]:
                return unavailable("invalid_period_basis")
        with localcontext(Context(prec=38, rounding=ROUND_HALF_EVEN)):
            unit = "percent"
            if name in {"growth", "yoy", "cagr", "fcf_growth", "discrete_quarter"}:
                current, previous = operands["current"], operands["previous"]
                if current["metric"] != previous["metric"]:
                    return unavailable("incompatible_metrics")
                end, old_end = day(current["period_end"]), day(previous["period_end"])
                if end <= old_end:
                    return unavailable("incompatible_period_order")
                if name == "discrete_quarter":
                    expected = {"ytd_6m": "quarter", "ytd_9m": "ytd_6m", "annual": "ytd_9m"}
                    if (
                        expected.get(current["basis"]) != previous["basis"]
                        or current.get("period_start") != previous.get("period_start")
                        or not 70 <= (end - old_end).days <= 110
                    ):
                        return unavailable("incompatible_ytd_periods")
                    value = values["current"] - values["previous"]
                    unit = current["unit"]
                    result.update(period_start=old_end + timedelta(days=1), basis="quarter")
                else:
                    if current["basis"] != previous["basis"]:
                        return unavailable("incompatible_periods")
                    if current.get("period_start") is not None:
                        length = (end - day(current["period_start"])).days
                        old_length = (old_end - day(previous["period_start"])).days
                        if abs(length - old_length) > 7 or day(current["period_start"]) <= old_end:
                            return unavailable("incompatible_periods")
                    if values["previous"] <= 0:
                        return unavailable("nonpositive_growth_base")
                    if name in {"yoy", "cagr"}:
                        years = end.year - old_end.year
                        if (
                            years < 1
                            or abs(Decimal((end - old_end).days) - years * Decimal("365.2425")) > 14
                        ):
                            return unavailable("incompatible_annual_spacing")
                        if name == "yoy" and years != 1:
                            return unavailable("yoy_requires_one_year")
                    if name == "cagr":
                        if current["basis"] != "annual" or values["current"] < 0:
                            return unavailable("cagr_requires_annual_nonnegative_endpoint")
                        result["years"] = years
                        value = (
                            (values["current"] / values["previous"])
                            ** (Decimal(1) / Decimal(years))
                            - 1
                        ) * 100
                    else:
                        value = (values["current"] / values["previous"] - 1) * 100
                    result["comparison_period_end"] = previous["period_end"]
            elif name in {"roe", "roa"}:
                balance = "equity" if name == "roe" else "assets"
                income = operands["net_income"]
                opening, closing = operands[f"opening_{balance}"], operands[f"closing_{balance}"]
                if (
                    not income.get("period_start")
                    or opening["basis"] != "instant"
                    or closing["basis"] != "instant"
                    or day(opening["period_end"]) != day(income["period_start"]) - timedelta(days=1)
                    or closing["period_end"] != income["period_end"]
                ):
                    return unavailable("incompatible_average_balance_periods")
                denominator = (values[f"opening_{balance}"] + values[f"closing_{balance}"]) / 2
                if (
                    denominator <= 0
                    or values[f"opening_{balance}"] <= 0
                    or values[f"closing_{balance}"] <= 0
                ):
                    return unavailable("nonpositive_balance")
                value = values["net_income"] / denominator * 100
                result["annualized"] = False
            else:
                if len(periods) != 1:
                    return unavailable("incompatible_periods")
                instant = name in {"current_ratio", "debt_equity"}
                if (anchor["basis"] == "instant") != instant:
                    return unavailable("incompatible_period_basis")
                if name == "fcf":
                    if values["capital_expenditures"] < 0:
                        return unavailable("negative_capex_payment")
                    value = values["operating_cash_flow"] - values["capital_expenditures"]
                    unit = anchor["unit"]
                elif name == "ebitda":
                    if any(
                        values[k] < 0 for k in ("depreciation", "amortization", "interest_expense")
                    ):
                        return unavailable("negative_expense_component")
                    value = sum(values.values(), Decimal(0))
                    unit = anchor["unit"]
                else:
                    numerator, denominator_key = REQUIRED[name][:2]
                    if name == "debt_equity":
                        denominator_key = "equity"
                        amount = sum((values[k] for k in REQUIRED[name][:-1]), Decimal(0))
                        if any(values[k] < 0 for k in REQUIRED[name][:-1]):
                            return unavailable("negative_debt_component")
                    else:
                        amount = values[numerator]
                    if name == "current_ratio" and amount < 0:
                        return unavailable("negative_current_assets")
                    if values[denominator_key] <= 0:
                        return unavailable("nonpositive_denominator")
                    value = amount / values[denominator_key]
                    if instant:
                        unit = "ratio"
                    else:
                        value *= 100
            result.update(
                status="AVAILABLE",
                value=str(value),
                unit=unit,
                freshness="STALE"
                if any(r.get("status") == "STALE" or r.get("freshness") == "STALE" for r in rows)
                else "RECENT",
            )
            return result
    except (DecimalException, ValueError, TypeError, KeyError):
        return unavailable("invalid_operand")
