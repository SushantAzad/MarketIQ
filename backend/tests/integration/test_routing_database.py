import json
import sys
from datetime import datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from test_calculation_database import import_data

from app.core.config import Settings
from app.database import schema as db
from app.routing import __main__ as cli
from app.routing.models import Period, QueryRequest
from app.routing.planner import load_issuers, plan_query
from app.routing.service import execute_query

pytestmark = pytest.mark.integration
PERIOD = Period(basis="annual", period_end="2024-12-31")


def test_fact_calculation_routing_never_loads_document_models(pg):
    import_data(pg)
    result = execute_query(
        pg,
        Settings(_env_file=None),
        QueryRequest(question="TEST revenue and net margin", period=PERIOD),
        documents=lambda *_: pytest.fail("structured query must not call documents"),
    )
    assert result["status"] == "AVAILABLE"
    assert Decimal(result["retrieved_facts"][0]["result"]["value"]) == 200
    assert Decimal(result["calculated_values"][0]["result"]["value"]) == 10
    assert result["generated_explanation"] is None
    with pg.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(db.financial_metrics)) == 1


def test_clarification_prevents_all_execution_and_issuer_aliases_come_from_db(pg):
    import_data(pg)
    result = execute_query(pg, Settings(_env_file=None), QueryRequest(question="TEST revenue"))
    assert result["status"] == "NEEDS_CLARIFICATION" and not result["retrieved_facts"]
    with pg.connect() as connection:
        registry = load_issuers(connection)
        assert (
            plan_query(QueryRequest(question="Test issuer revenue", period=PERIOD), registry).status
            == "READY"
        )
        assert connection.scalar(sa.select(sa.func.count()).select_from(db.financial_metrics)) == 0


def test_unimplemented_routes_are_explicit_and_do_not_erase_facts(pg):
    import_data(pg)
    result = execute_query(
        pg,
        Settings(_env_file=None),
        QueryRequest(question="TEST revenue and stock price and risk score", period=PERIOD),
    )
    assert result["status"] == "PARTIAL"
    assert len(result["unavailable"]) == 2 and result["retrieved_facts"]
    assert result["market_data"] == [] and result["model_outputs"] == []


def test_document_failure_and_historical_cutoff_preserve_calculations(pg):
    import_data(pg)
    request = QueryRequest(
        question="Explain TEST net margin",
        period=PERIOD,
        form="10-K",
        accession="0000000001-25-000001",
    )
    # Fixture companyfacts do not specify filing report_period; set known test metadata.
    with pg.begin() as connection:
        connection.execute(db.filings.update().values(report_period=PERIOD.period_end))

    def fail(*_):
        raise RuntimeError("must never expose private endpoint details")

    result = execute_query(pg, Settings(_env_file=None), request, documents=fail)
    assert result["status"] == "PARTIAL" and result["calculated_values"]
    assert result["unavailable"][0]["reason"] == "document_service_unavailable"
    assert "private endpoint" not in json.dumps(result, default=str)
    historical = request.model_copy(
        update={"as_of": datetime.fromisoformat("2025-03-01T00:00:00+00:00")}
    )
    result = execute_query(pg, Settings(_env_file=None), historical, documents=fail)
    assert result["unavailable"][0]["reason"] == "historical_document_cutoff_not_supported"


def test_document_scope_is_checked_before_provider(pg):
    import_data(pg)
    request = QueryRequest(
        question="Explain TEST net margin",
        period=PERIOD,
        form="10-K",
        accession="0000000001-25-000001",
    )
    result = execute_query(
        pg,
        Settings(_env_file=None),
        request,
        documents=lambda *_: pytest.fail("period does not match"),
    )
    assert result["unavailable"][0]["reason"] == "document_period_does_not_match_metric_period"
    with pg.begin() as connection:
        connection.execute(db.filings.update().values(report_period=PERIOD.period_end))
    result = execute_query(
        pg,
        Settings(_env_file=None),
        request,
        documents=lambda *_: {
            "status": "EVIDENCE_ONLY",
            "retrieved_evidence": [{"text": "test evidence"}],
        },
    )
    assert result["status"] == "AVAILABLE" and result["document_evidence"]


def test_cli_plan_and_ask(pg, monkeypatch, capsys):
    import_data(pg)
    monkeypatch.setattr(cli, "database_engine", lambda *_: pg)
    for command in ["plan", "ask"]:
        monkeypatch.setattr(
            sys,
            "argv",
            ["routing", command, "TEST revenue", "--basis", "annual", "--period-end", "2024-12-31"],
        )
        assert cli.main() == 0
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == ("READY" if command == "plan" else "AVAILABLE")
    monkeypatch.setattr(sys, "argv", ["routing", "plan", "TEST revenue", "--basis", "annual"])
    assert cli.main() == 1
