import hashlib
import json
import sys
from decimal import Decimal
from uuid import UUID

import pytest
import sqlalchemy as sa
from conftest import migration_config
from test_financial_database import observation, snapshot

from alembic import command
from app.database import __main__ as cli
from app.database import schema as db
from app.services.calculation_query import CalculationRequest, read_calculation, run_calculation
from app.services.financial_import import import_snapshot

pytestmark = pytest.mark.integration


def import_data(pg, restated=False):
    concepts = {}
    for concept, values in {
        "Revenues": [100, 200],
        "NetIncomeLoss": [10, 40 if restated else 20],
        "NetCashProvidedByUsedInOperatingActivities": [30, 60],
        "PaymentsToAcquirePropertyPlantAndEquipment": [10, 20],
        "Assets": [100, 200],
        "StockholdersEquity": [50, 100],
        "AssetsCurrent": [50, 100],
        "LiabilitiesCurrent": [25, 50],
    }.items():
        entries = []
        for year, value in zip([2023, 2024], values, strict=True):
            entry = observation(value, start=f"{year}-01-01", end=f"{year}-12-31")
            if concept in {"Assets", "StockholdersEquity", "AssetsCurrent", "LiabilitiesCurrent"}:
                del entry["start"]
            if restated:
                entry.update(accn="0000000001-25-000002", filed="2025-04-01", form="10-K/A")
            entries.append(entry)
        concepts[concept] = {"units": {"USD": entries}}
    body = json.dumps(
        {"cik": 1, "entityName": "Test issuer", "facts": {"us-gaap": concepts}}
    ).encode()
    source = {
        "content_hash": hashlib.sha256(body).hexdigest(),
        "source_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
        "first_fetched_at": "2025-04-02T00:00:00+00:00"
        if restated
        else "2025-02-02T00:00:00+00:00",
    }
    with pg.begin() as connection:
        import_snapshot(connection, body, source, tickers=["TEST"])


def request(metric="net_margin", **updates):
    return CalculationRequest(
        **(
            {
                "ticker": "TEST",
                "metric": metric,
                "basis": "annual",
                "period_end": "2024-12-31",
                "as_of": "2025-03-01T00:00:00Z",
            }
            | updates
        )
    )


def test_calculation_persistence_replay_and_foreign_key_lineage(pg):
    import_data(pg)
    result = run_calculation(pg, request())
    assert Decimal(result["value"]) == 10
    assert result == run_calculation(pg, request())
    assert read_calculation(pg, UUID(result["run_id"])) == result
    with pg.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(db.financial_metrics)) == 1
        links = connection.execute(sa.select(db.metric_inputs)).mappings().all()
        assert len(links) == 2 and all(row["fact_id"] for row in links)
        for row in links:
            value = connection.scalar(
                sa.select(db.financial_facts.c.value).where(
                    db.financial_facts.c.id == row["fact_id"]
                )
            )
            assert value == Decimal(result["operands"][row["operand_name"]]["value"])


def test_compound_fcf_growth_has_metric_and_fact_dependencies(pg):
    import_data(pg)
    result = run_calculation(pg, request("fcf_growth", comparison_end="2023-12-31"))
    assert Decimal(result["value"]) == 100
    with pg.connect() as connection:
        links = connection.execute(sa.select(db.metric_inputs)).mappings().all()
        assert sum(r["input_metric_id"] is not None for r in links) == 2
        assert sum(r["fact_id"] is not None for r in links) == 4
    assert read_calculation(pg, UUID(result["run_id"])) == result
    with pg.begin() as connection:
        with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
            connection.execute(
                db.metric_inputs.insert().values(
                    metric_id=UUID(result["run_id"]), operand_name="invalid"
                )
            )
        with (
            pytest.raises(RuntimeError, match="derived metric dependencies"),
            connection.begin_nested(),
        ):
            command.downgrade(migration_config(connection), "714683a5d9fb")
    assert read_calculation(pg, UUID(result["run_id"])) == result


def test_restatement_and_observation_cutoff_change_calculation_not_history(pg):
    import_data(pg)
    original = run_calculation(pg, request())
    import_data(pg, restated=True)
    assert Decimal(run_calculation(pg, request())["value"]) == 10
    latest = run_calculation(pg, request(as_of="2025-05-01T00:00:00Z"))
    assert Decimal(latest["value"]) == 20
    assert latest["run_id"] != original["run_id"]
    assert read_calculation(pg, UUID(original["run_id"])) == original
    absent = run_calculation(pg, request(as_of="2025-01-01T00:00:00Z"))
    assert absent["value"] is None and absent["reason"] == "missing_input"


@pytest.mark.parametrize(
    "metric,updates,expected",
    [
        ("roe", {}, "26.666666666666666666666666666666666667"),
        ("roa", {}, "13.333333333333333333333333333333333333"),
        ("current_ratio", {"basis": "instant"}, "2"),
        ("ebitda", {}, None),
        ("debt_equity", {"basis": "instant"}, None),
    ],
)
def test_selects_correct_balance_dates_and_refuses_missing_components(
    pg, metric, updates, expected
):
    import_data(pg)
    result = run_calculation(pg, request(metric, **updates))
    assert result["value"] == expected
    assert read_calculation(pg, UUID(result["run_id"])) == result


def test_unknown_company_does_not_create_false_issuer(pg):
    result = run_calculation(pg, request(ticker="UNKNOWN"))
    assert result["status"] == "UNAVAILABLE" and result["run_id"] is None
    result = run_calculation(
        pg, request("fcf_growth", ticker="UNKNOWN", comparison_end="2023-12-31")
    )
    assert result["run_id"] is None


def test_growth_and_discrete_quarter_select_explicit_periods(pg):
    import_data(pg)
    result = run_calculation(pg, request("yoy", comparison_end="2023-12-31"))
    assert Decimal(result["value"]) == 100
    body, source = snapshot(
        [observation(80, end="2024-06-30"), observation(30, end="2024-03-31")],
        observed="2025-02-03T00:00:00+00:00",
    )
    with pg.begin() as connection:
        import_snapshot(connection, body, source, tickers=["TEST"])
    result = run_calculation(
        pg,
        request(
            "discrete_quarter", basis="ytd_6m", period_end="2024-06-30", comparison_end="2024-03-31"
        ),
    )
    assert result["value"] == "50" and result["period_start"] == "2024-04-01"
    assert read_calculation(pg, UUID(result["run_id"])) == result


def test_cli_calculate_and_show(pg, monkeypatch, capsys):
    import_data(pg)
    monkeypatch.setattr(cli, "database_engine", lambda settings: pg)
    monkeypatch.setattr(
        sys,
        "argv",
        ["database", "calculate", "TEST", "fcf", "--basis", "annual", "--period-end", "2024-12-31"],
    )
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["value"] == "40"
    monkeypatch.setattr(sys, "argv", ["database", "show-calculation", result["run_id"]])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out) == result
