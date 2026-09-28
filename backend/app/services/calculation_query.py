"""One-snapshot operand selection and durable calculation dependency tracing."""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.dialects.postgresql import insert

from app.database import schema as db
from app.services.calculations import FORMULAS, REQUIRED, calculate, day
from app.services.financial_query import QUERY_CONCEPTS, financial_value
from app.services.normalization import fingerprint


class CalculationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(pattern=r"^[A-Z0-9.\-]{1,20}$")
    metric: str
    basis: Literal["instant", "annual", "quarter", "ytd_6m", "ytd_9m"]
    period_end: date
    comparison_end: date | None = None
    input_metric: str = "revenue"
    unit: str = "USD"
    as_of: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def valid(self) -> Self:
        if self.metric not in FORMULAS or self.input_metric not in QUERY_CONCEPTS:
            raise ValueError("Unknown calculation or input metric")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("as_of must have a timezone")
        comparison = self.metric in {"growth", "yoy", "cagr", "fcf_growth", "discrete_quarter"}
        if comparison != (self.comparison_end is not None):
            raise ValueError("Comparison date required only for comparative calculations")
        if self.comparison_end and self.comparison_end >= self.period_end:
            raise ValueError("Comparison must precede target period")
        return self


def build_calculation(connection: sa.Connection, request: CalculationRequest) -> dict[str, Any]:
    def fact(metric: str, basis: str, end: date) -> dict[str, Any]:
        return financial_value(
            connection,
            request.ticker,
            metric,
            basis=basis,
            period_end=end,
            unit=request.unit,
            as_of=request.as_of,
        )

    def fcf(end: date) -> dict[str, Any]:
        return {
            "ticker": request.ticker,
            "period_end": end,
            "basis": request.basis,
            "as_of": request.as_of,
            **calculate("fcf", {k: fact(k, request.basis, end) for k in REQUIRED["fcf"]}),
        }

    name = request.metric
    if name in {"growth", "yoy", "cagr", "fcf_growth", "discrete_quarter"}:
        assert request.comparison_end is not None
        if name == "fcf_growth":
            operands = {"current": fcf(request.period_end), "previous": fcf(request.comparison_end)}
        else:
            previous_basis: str = request.basis
            if name == "discrete_quarter":
                previous_basis = {"ytd_6m": "quarter", "ytd_9m": "ytd_6m", "annual": "ytd_9m"}.get(
                    request.basis, request.basis
                )
            operands = {
                "current": fact(request.input_metric, request.basis, request.period_end),
                "previous": fact(request.input_metric, previous_basis, request.comparison_end),
            }
    elif name in {"roe", "roa"}:
        balance = "equity" if name == "roe" else "assets"
        income = fact("net_income", request.basis, request.period_end)
        start = income.get("period_start")
        opening = (
            fact(balance, "instant", day(start) - timedelta(days=1))
            if start
            else {
                "metric": balance,
                "value": None,
                "status": "UNAVAILABLE",
                "reason": "income_period_start_unavailable",
            }
        )
        operands = {
            "net_income": income,
            f"opening_{balance}": opening,
            f"closing_{balance}": fact(balance, "instant", request.period_end),
        }
    else:
        operands = {k: fact(k, request.basis, request.period_end) for k in REQUIRED[name]}
    return {
        "ticker": request.ticker,
        "period_end": request.period_end,
        "basis": request.basis,
        "as_of": request.as_of,
        **calculate(name, operands),
    }


def save_calculation(connection: sa.Connection, result: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(result, default=str))
    company = connection.scalar(
        sa.select(db.securities.c.company_id).where(db.securities.c.ticker == result["ticker"])
    )
    if company is None:
        return {**result, "run_id": None, "persistence": "unknown_issuer_not_saved"}
    input_hash = fingerprint(result)
    run_id = uuid5(NAMESPACE_URL, "marketiq:calculation:" + input_hash)
    references = []
    for name, operand in result["operands"].items():
        if operand.get("data_kind") == "CALCULATED_FINANCIAL_METRIC":
            child = save_calculation(connection, operand)
            result["operands"][name] = child
            references.append(
                {
                    "metric_id": run_id,
                    "operand_name": name,
                    "input_metric_id": UUID(child["run_id"]),
                    "fact_id": None,
                }
            )
        elif operand.get("source", {}).get("fact_id"):
            references.append(
                {
                    "metric_id": run_id,
                    "operand_name": name,
                    "fact_id": UUID(operand["source"]["fact_id"]),
                    "input_metric_id": None,
                }
            )
    result["run_id"] = str(run_id)
    connection.execute(
        insert(db.financial_metrics)
        .values(
            id=run_id,
            company_id=company,
            metric_name=result["metric"],
            period_start=day(result["period_start"]) if result.get("period_start") else None,
            period_end=day(result["period_end"]),
            period_basis=result["basis"],
            value=result["value"],
            unit=result["unit"] or "unavailable",
            formula_version=result["formula_version"],
            input_set_hash=input_hash,
            unavailable_reason=result["reason"],
            response=result,
        )
        .on_conflict_do_nothing()
    )
    if references:
        connection.execute(insert(db.metric_inputs).on_conflict_do_nothing(), references)
    return result


def run_calculation(engine: sa.Engine, request: CalculationRequest) -> dict[str, Any]:
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with connection.begin():
            result = build_calculation(connection, request)
            result["request"] = request.model_dump(mode="json")
            return save_calculation(connection, result)


def read_calculation(engine: sa.Engine, run_id: UUID) -> dict[str, Any] | None:
    with engine.connect() as connection:
        result = connection.scalar(
            sa.select(db.financial_metrics.c.response).where(db.financial_metrics.c.id == run_id)
        )
        return dict(result) if result is not None else None
