"""Dispatch only allowlisted plan steps; preserve structured results if evidence fails."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from app.core.config import Settings
from app.database import schema as db
from app.routing.models import Intent, QueryPlan, QueryRequest, Step
from app.routing.planner import load_issuers, plan_query
from app.services.calculation_query import CalculationRequest, build_calculation, save_calculation
from app.services.financial_query import financial_value

DocumentReader = Callable[[Step, QueryRequest], dict[str, Any]]


def document_reader(
    engine: sa.Engine, settings: Settings, step: Step, request: QueryRequest
) -> dict[str, Any]:
    # Imports and model loading stay outside fact/calculation requests.
    from app.indexing.embeddings import SentenceEncoder
    from app.indexing.index import client
    from app.research.service import ResearchRequest, research

    assert request.form is not None
    qdrant = client(settings)
    try:
        encoder = None if settings.rag_retrieval_mode == "bm25" else SentenceEncoder(settings)
        return research(
            engine,
            encoder,
            qdrant,
            settings,
            ResearchRequest(
                question=request.question,
                ticker=step.ticker,
                form=request.form,
                accession=request.accession,
            ),
        )
    finally:
        qdrant.close()


def alignment(rows: list[dict[str, Any]], expected: set[str]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        step = row["step"]
        groups.setdefault((step["metric"], step["input_metric"]), []).append(row)
    output = []
    for (metric, input_metric), members in groups.items():
        periods = [
            {
                "ticker": r["step"]["ticker"],
                "basis": r["result"].get("basis"),
                "period_start": r["result"].get("period_start"),
                "period_end": r["result"].get("period_end"),
                "comparison_period_end": r["result"].get("comparison_period_end"),
                "unit": r["result"].get("unit"),
            }
            for r in members
        ]
        signatures = {tuple(str(p[k]) for k in p if k != "ticker") for p in periods}
        available = (all(r["result"].get("value") is not None for r in members)
                     and {r["step"]["ticker"] for r in members} == expected)
        aligned = available and len(signatures) == 1 and len(members) >= 2
        output.append(
            {
                "metric": metric,
                "input_metric": input_metric,
                "status": "ALIGNED" if aligned else "UNALIGNED",
                "reason": None
                if aligned
                else (
                    "missing_values" if not available else "different_reporting_periods_or_units"
                ),
                "periods": periods,
                "ranking_or_spread_computed": False,
            }
        )
    return output


def execute_query(
    engine: sa.Engine,
    settings: Settings,
    request: QueryRequest,
    *,
    documents: DocumentReader | None = None,
) -> dict[str, Any]:
    facts: list[dict[str, Any]] = []
    calculated: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with connection.begin():
            plan = plan_query(request, load_issuers(connection))
            result: dict[str, Any] = {
                "plan": plan.model_dump(mode="json"),
                "status": plan.status,
                "retrieved_facts": facts,
                "calculated_values": calculated,
                "document_evidence": evidence,
                "market_data": [],
                "model_outputs": [],
                "generated_explanation": None,
                "unavailable": unavailable,
                "comparisons": [],
                "upstream_refreshed": False,
            }
            if plan.status != "READY":
                return result
            cutoff = request.as_of or datetime.now(UTC)
            result["structured_as_of"] = cutoff.isoformat()
            for step in plan.steps:
                if step.intent not in {Intent.FACT, Intent.CALCULATION}:
                    continue
                assert step.period is not None and step.metric is not None
                try:
                    with connection.begin_nested():
                        if step.intent == Intent.FACT:
                            value = financial_value(
                                connection,
                                step.ticker,
                                step.metric,
                                basis=step.period.basis,
                                period_end=step.period.period_end,
                                unit=request.unit,
                                as_of=cutoff,
                            )
                            target = facts
                        else:
                            calculation = CalculationRequest(
                                ticker=step.ticker,
                                metric=step.metric,
                                input_metric=step.input_metric,
                                unit=request.unit,
                                as_of=cutoff,
                                **step.period.model_dump(),
                            )
                            value = build_calculation(connection, calculation)
                            value["request"] = calculation.model_dump(mode="json")
                            value = save_calculation(connection, value)
                            target = calculated
                        target.append({"step": step.model_dump(mode="json"), "result": value})
                except (ValueError, sa.exc.SQLAlchemyError):
                    unavailable.append(
                        {
                            "step": step.model_dump(mode="json"),
                            "reason": "structured_service_unavailable",
                        }
                    )
    for step in plan.steps:
        if step.intent in {Intent.MARKET, Intent.RISK}:
            unavailable.append(
                {
                    "step": step.model_dump(mode="json"),
                    "reason": "market_service_not_implemented"
                    if step.intent == Intent.MARKET
                    else "approved_risk_model_unavailable",
                }
            )
        elif step.intent in {Intent.DOCUMENT, Intent.COMPANY}:
            reason = document_scope_error(engine, plan, step)
            if reason:
                unavailable.append({"step": step.model_dump(mode="json"), "reason": reason})
                continue
            try:
                value = (
                    documents(step, request)
                    if documents
                    else document_reader(engine, settings, step, request)
                )
                evidence.append({"step": step.model_dump(mode="json"), "result": value})
            except Exception:
                # Provider errors can contain endpoint credentials or user text.
                unavailable.append(
                    {"step": step.model_dump(mode="json"), "reason": "document_service_unavailable"}
                )
    if Intent.COMPARISON in plan.intents:
        result["comparisons"] = alignment(facts + calculated, {i.ticker for i in plan.issuers})
    successes = sum(r["result"].get("value") is not None for r in facts + calculated)
    successes += sum(bool(r["result"].get("retrieved_evidence")) for r in evidence)
    incomplete = (len(unavailable) + len(facts) + len(calculated) + len(evidence)) > successes
    incomplete |= any(c["status"] != "ALIGNED" for c in result["comparisons"])
    result["status"] = (
        "PARTIAL" if successes and incomplete else ("AVAILABLE" if successes else "UNAVAILABLE")
    )
    return result


def document_scope_error(engine: sa.Engine, plan: QueryPlan, step: Step) -> str | None:
    request = plan.request
    if request.as_of is not None:
        return "historical_document_cutoff_not_supported"
    if request.accession is None:
        return None
    with engine.connect() as connection:
        filing = connection.execute(
            sa.select(db.filings.c.report_period)
            .join(db.securities, db.securities.c.company_id == db.filings.c.company_id)
            .where(
                db.securities.c.ticker == step.ticker,
                db.filings.c.accession_number == request.accession,
                db.filings.c.form_type == request.form,
            )
        ).first()
    if filing is None:
        return "accession_not_found_in_issuer_form_scope"
    periods = [s.period for s in plan.steps if s.ticker == step.ticker and s.period]
    if any(p.period_end != filing.report_period for p in periods):
        return "document_period_does_not_match_metric_period"
    return None
