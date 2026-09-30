import pytest
from pydantic import ValidationError

from app.routing.models import Intent, Issuer, Period, QueryRequest, Step
from app.routing.planner import plan_query
from app.routing.service import alignment

REGISTRY = [
    Issuer(ticker=t, company_id=t, legal_name=n, aliases=[t, n])
    for t, n in [("NVDA", "Nvidia"), ("MSFT", "Microsoft")]
]
PERIOD = Period(basis="annual", period_end="2025-12-31")


@pytest.mark.parametrize(
    "question,intents",
    [
        ("Nvidia revenue", [Intent.FACT]),
        ("NVDA net margin", [Intent.CALCULATION]),
        ("NVDA revenue and net margin", [Intent.CALCULATION, Intent.FACT]),
        ("NVDA stock price", [Intent.MARKET]),
        ("NVDA risk score", [Intent.RISK]),
        ("Compare NVDA and MSFT revenue", [Intent.FACT, Intent.COMPARISON]),
    ],
)
def test_representative_intents(question, intents):
    plan = plan_query(QueryRequest(question=question, period=PERIOD), REGISTRY)
    assert plan.status == "READY", plan.clarifications
    assert set(plan.intents) == set(intents)


@pytest.mark.parametrize(
    "question,options",
    [
        ("revenue", {}),
        ("NVDA revenue", {}),
        ("NVDA revenue in 2025", {}),
        ("Compare NVDA and unknown revenue", {"period": PERIOD}),
        ("NVDA profit margin", {"period": PERIOD}),
        ("NVDA revenue growth", {"period": PERIOD}),
        ("NVDA growth", {"period": PERIOD}),
        ("Explain NVDA net margin", {"period": PERIOD, "form": "10-K"}),
        ("NVDA risks in 2024", {"form": "10-K"}),
        ("NVDA revenue", {"period": PERIOD, "tickers": ["MSFT"]}),
        ("NVDA assets", {"period": PERIOD}),
        ("NVDA revenue as of yesterday", {"period": PERIOD}),
        ("NVDA revenue not net income", {"period": PERIOD}),
        ("NVDA revenue and dividend yield", {"period": PERIOD}),
    ],
)
def test_ambiguous_questions_need_clarification(question, options):
    plan = plan_query(QueryRequest(question=question, **options), REGISTRY)
    assert plan.status == "NEEDS_CLARIFICATION" and plan.clarifications


def test_growth_consumes_input_metric_and_preserves_comparison_dates():
    period = PERIOD.model_copy(update={"comparison_end": PERIOD.period_end.replace(year=2024)})
    plan = plan_query(QueryRequest(question="NVDA revenue YoY", period=period), REGISTRY)
    assert plan.status == "READY" and len(plan.steps) == 1
    assert plan.steps[0].metric == "yoy" and plan.steps[0].input_metric == "revenue"


def test_document_and_metric_are_separate_routes():
    plan = plan_query(
        QueryRequest(
            question="Explain NVDA net margin",
            period=PERIOD,
            form="10-K",
            accession="0001045810-26-000021",
        ),
        REGISTRY,
    )
    assert plan.status == "READY"
    assert set(plan.intents) == {Intent.CALCULATION, Intent.DOCUMENT}
    assert (
        plan_query(QueryRequest(question="Nvidia risks", form="10-K"), REGISTRY).status == "READY"
    )
    assert plan_query(QueryRequest(question="Nvidia overview", form="10-K"), REGISTRY).intents == [
        Intent.COMPANY
    ]


def test_exact_date_can_supply_period_but_calendar_year_cannot():
    plan = plan_query(QueryRequest(question="NVDA annual revenue ending 2025-12-31"), REGISTRY)
    assert plan.status == "READY" and plan.steps[0].period == PERIOD
    assert (
        plan_query(QueryRequest(question="NVDA annual revenue 2025"), REGISTRY).status
        == "NEEDS_CLARIFICATION"
    )


def test_strict_models_reject_arbitrary_tools_and_naive_cutoffs():
    with pytest.raises(ValidationError):
        Step(intent="SQL", ticker="NVDA", metric="DROP TABLE")
    with pytest.raises(ValidationError):
        Step(intent=Intent.CALCULATION, ticker="NVDA", metric="invented")
    with pytest.raises(ValidationError):
        QueryRequest(question="NVDA revenue", as_of="2025-01-01")


@pytest.mark.parametrize(
    "question,options",
    [
        ("NVDA revenue in euros", {}),
        ("NVDA revenue as of 2024-01-01", {"as_of": "2026-01-01T00:00:00Z"}),
        ("NVDA revenue for 2024-12-31", {}),
    ],
)
def test_explicit_scope_cannot_silently_override_question(question, options):
    plan = plan_query(QueryRequest(question=question, period=PERIOD, **options), REGISTRY)
    assert plan.status == "NEEDS_CLARIFICATION"


def test_comparisons_refuse_mismatched_dates_and_missing_issuers():
    def row(ticker, start):
        return {
            "step": {"metric": "revenue", "input_metric": "revenue", "ticker": ticker},
            "result": {
                "value": "100",
                "unit": "USD",
                "basis": "annual",
                "period_start": start,
                "period_end": "2025-12-31",
            },
        }

    rows = [row("NVDA", "2025-01-01"), row("MSFT", "2025-01-01")]
    assert alignment(rows, {"NVDA", "MSFT"})[0]["status"] == "ALIGNED"
    rows[1]["result"]["period_start"] = "2025-01-02"
    assert alignment(rows, {"NVDA", "MSFT"})[0]["status"] == "UNALIGNED"
    assert alignment(rows[:1], {"NVDA", "MSFT"})[0]["reason"] == "missing_values"
