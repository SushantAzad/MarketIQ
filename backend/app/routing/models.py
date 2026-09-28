from datetime import date, datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.calculations import FORMULAS
from app.services.financial_query import QUERY_CONCEPTS

VERSION = "query-router-v1"
Basis = Literal["instant", "annual", "quarter", "ytd_6m", "ytd_9m"]
Form = Literal["10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Intent(StrEnum):
    FACT = "FINANCIAL_METRIC"
    CALCULATION = "FINANCIAL_CALCULATION"
    DOCUMENT = "DOCUMENT_RESEARCH"
    COMPARISON = "COMPARISON"
    RISK = "RISK_ANALYSIS"
    MARKET = "MARKET_DATA"
    COMPANY = "GENERAL_COMPANY_INFORMATION"


class Period(StrictModel):
    basis: Basis
    period_end: date
    comparison_end: date | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.comparison_end and self.comparison_end >= self.period_end:
            raise ValueError("Comparison end must precede period end")
        return self


class QueryRequest(StrictModel):
    question: str = Field(min_length=3, max_length=2000)
    tickers: list[str] = Field(default_factory=list, max_length=4)
    period: Period | None = None
    issuer_periods: dict[str, Period] = Field(default_factory=dict, max_length=4)
    form: Form | None = None
    accession: str | None = Field(default=None, pattern=r"^\d{10}-\d{2}-\d{6}$")
    as_of: datetime | None = None
    unit: str = Field(default="USD", pattern=r"^[A-Z]{3}$")

    @field_validator("as_of")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("as_of requires a timezone")
        return value

    @field_validator("tickers")
    @classmethod
    def normalized(cls, tickers: list[str]) -> list[str]:
        return list(dict.fromkeys(t.upper().strip() for t in tickers))


class Issuer(StrictModel):
    ticker: str
    company_id: str
    legal_name: str
    aliases: list[str]


class Step(StrictModel):
    intent: Intent
    ticker: str
    metric: str | None = None
    input_metric: str = "revenue"
    period: Period | None = None

    @model_validator(mode="after")
    def allowlisted(self) -> Self:
        if self.intent == Intent.FACT and self.metric not in QUERY_CONCEPTS:
            raise ValueError("Unknown reported metric")
        if self.intent == Intent.CALCULATION and self.metric not in FORMULAS:
            raise ValueError("Unknown formula")
        if self.input_metric not in QUERY_CONCEPTS:
            raise ValueError("Unknown calculation input metric")
        return self


class QueryPlan(StrictModel):
    version: str = VERSION
    request: QueryRequest
    status: Literal["READY", "NEEDS_CLARIFICATION"]
    issuers: list[Issuer]
    intents: list[Intent]
    steps: list[Step] = Field(max_length=24)
    clarifications: list[str]
