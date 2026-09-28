import hashlib
import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from conftest import migration_config

from alembic import command
from app.core.config import Settings
from app.database import __main__ as dbcli
from app.database import schema as db
from app.ingestion.store import IngestionStore
from app.services.financial_import import import_snapshot
from app.services.financial_query import financial_value

pytestmark = pytest.mark.integration


def observation(value: int = 100, **updates: object) -> dict[str, object]:
    return {
        "start": "2024-01-01",
        "end": "2024-12-31",
        "val": value,
        "accn": "0000000001-25-000001",
        "form": "10-K",
        "filed": "2025-02-01",
        "fy": 2025,
        "fp": "FY",
        **updates,
    }


def snapshot(
    entries: list[dict[str, object]], observed: str = "2025-02-02T00:00:00+00:00"
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "cik": 1,
            "entityName": "Test issuer",
            "facts": {"us-gaap": {"Revenues": {"units": {"USD": entries}}}},
        }
    ).encode()
    return body, {
        "content_hash": hashlib.sha256(body).hexdigest(),
        "source_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
        "first_fetched_at": observed,
    }


def test_migration_round_trip_and_schema_matches(pg: sa.Engine) -> None:
    with pg.begin() as connection:
        assert compare_metadata(MigrationContext.configure(connection), db.metadata) == []
        command.downgrade(migration_config(connection), "base")
        assert sa.inspect(connection).get_table_names() == ["alembic_version"]
        command.upgrade(migration_config(connection), "head")
        assert set(db.metadata.tables) <= set(sa.inspect(connection).get_table_names())


def test_exact_values_idempotency_and_lineage(pg: sa.Engine) -> None:
    body, source = snapshot([observation(9007199254740993)])
    with pg.begin() as connection:
        first = import_snapshot(connection, body, source, tickers=["TEST"])
        again = import_snapshot(connection, body, source, tickers=["TEST"])
        assert first["status"] == "complete"
        assert again["status"] == "unchanged"
        assert connection.scalar(sa.select(sa.func.count()).select_from(db.financial_facts)) == 1
        result = financial_value(connection, "TEST", "revenue", basis="annual")
        assert Decimal(result["value"]) == Decimal(9007199254740993)
        assert result["period_end"] == date(2024, 12, 31)
        assert result["filing_fiscal_year"] == 2025
        assert result["source"]["content_hash"] == source["content_hash"]
        assert result["source"]["source_locator"] == "/facts/us-gaap/Revenues/units/USD/0"


def test_restatement_and_observation_cutoff_prevent_future_leakage(pg: sa.Engine) -> None:
    first, src1 = snapshot([observation(100)])
    second, src2 = snapshot(
        [
            observation(100),
            observation(120, accn="0000000001-25-000002", form="10-K/A", filed="2025-04-01"),
        ],
        "2025-04-02T00:00:00+00:00",
    )
    with pg.begin() as connection:
        import_snapshot(connection, first, src1, tickers=["TEST"])
        import_snapshot(connection, second, src2, tickers=["TEST"])
        early = financial_value(
            connection, "TEST", "revenue", basis="annual", as_of=datetime(2025, 1, 1, tzinfo=UTC)
        )
        old = financial_value(
            connection, "TEST", "revenue", basis="annual", as_of=datetime(2025, 3, 1, tzinfo=UTC)
        )
        latest = financial_value(connection, "TEST", "revenue", basis="annual")
        assert early["value"] is None
        assert old["value"] == "100"
        assert latest["value"] == "120"


def test_ytd_not_mistaken_for_quarter_and_missing_unit_is_unavailable(pg: sa.Engine) -> None:
    body, source = snapshot([observation(60, end="2024-06-30")])
    with pg.begin() as connection:
        import_snapshot(connection, body, source, tickers=["TEST"])
        assert financial_value(connection, "TEST", "revenue", basis="quarter")["value"] is None
        assert financial_value(connection, "TEST", "revenue", basis="ytd_6m")["value"] == "60"
        assert (
            financial_value(connection, "TEST", "revenue", basis="ytd_6m", unit="EUR")["value"]
            is None
        )


def test_conflicts_are_not_arbitrarily_selected(pg: sa.Engine) -> None:
    body, source = snapshot([observation(100), observation(101)])
    with pg.begin() as connection:
        import_snapshot(connection, body, source, tickers=["TEST"])
        result = financial_value(connection, "TEST", "revenue", basis="annual")
        assert result["value"] is None
        assert "Conflicting" in result["reason"]


def test_rejected_contexts_are_auditable(pg: sa.Engine) -> None:
    body, source = snapshot([observation(100), observation(80, start="2025-01-01")])
    with pg.begin() as connection:
        result = import_snapshot(connection, body, source, tickers=["TEST"])
        assert result == {"cik": "0000000001", "status": "partial", "accepted": 1, "rejected": 1}
        assert connection.scalar(sa.select(sa.func.count()).select_from(db.rejected_facts)) == 1


def test_transaction_failure_does_not_leave_partial_import(pg: sa.Engine) -> None:
    body, source = snapshot([observation()])
    with pytest.raises(RuntimeError), pg.begin() as connection:
        import_snapshot(connection, body, source, tickers=["TEST"])
        raise RuntimeError("simulated process failure")
    with pg.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(db.financial_facts)) == 0
        assert connection.scalar(sa.select(sa.func.count()).select_from(db.companies)) == 0


def test_removed_observation_does_not_leak_from_old_snapshot(pg: sa.Engine) -> None:
    first, src1 = snapshot([observation(100)])
    second, src2 = snapshot(
        [observation(50, start="2024-01-01", end="2024-06-30")], "2025-04-01T00:00:00+00:00"
    )
    with pg.begin() as connection:
        import_snapshot(connection, first, src1, tickers=["TEST"])
        import_snapshot(connection, second, src2, tickers=["TEST"])
        assert financial_value(connection, "TEST", "revenue", basis="annual")["value"] is None


def test_reappearing_source_version_uses_latest_observation(pg: sa.Engine) -> None:
    first, src1 = snapshot([observation(100)])
    second, src2 = snapshot([observation(120)], "2025-04-01T00:00:00+00:00")
    with pg.begin() as connection:
        import_snapshot(connection, first, src1, tickers=["TEST"])
        import_snapshot(connection, second, src2, tickers=["TEST"])
        import_snapshot(connection, first, {**src1, "observed_at": "2025-05-01T00:00:00+00:00"})
        assert financial_value(connection, "TEST", "revenue", basis="annual")["value"] == "100"
        assert (
            financial_value(
                connection,
                "TEST",
                "revenue",
                basis="annual",
                as_of=datetime(2025, 4, 2, tzinfo=UTC),
            )["value"]
            == "120"
        )


def test_cli_journal_bridge_and_freshness(
    pg: sa.Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        _env_file=None, sec_state_path=tmp_path / "state", raw_storage_path=tmp_path / "raw"
    )
    store = IngestionStore(settings.sec_state_path, settings.raw_storage_path)
    body, source = snapshot([observation(125)])
    try:
        registry = b'{"0":{"ticker":"TEST","cik_str":1}}'
        for url, content in [
            ("https://www.sec.gov/files/company_tickers.json", registry),
            (source["source_url"], body),
        ]:
            attempted = store.attempted(url)
            store.fetched(url, attempted, 0, 2, digest=store.save(content), http_status=200)
        # Preserve known acceptance time and primary document from a discovered filing.
        from app.providers.sec.models import Filing

        store.discover(
            Filing(
                cik="0000000001",
                accession_number="0000000001-25-000001",
                form_type="10-K",
                filing_date=date(2025, 2, 1),
                accepted_at=datetime(2025, 2, 1, 17, tzinfo=UTC),
                report_period=date(2024, 12, 31),
                primary_document="annual.htm",
            )
        )
    finally:
        store.close()
    monkeypatch.setattr(dbcli, "Settings", lambda: settings)
    monkeypatch.setattr(dbcli, "database_engine", lambda settings: pg)
    for args in (["migrate"], ["status"], ["import-journal"], ["import-journal"]):
        monkeypatch.setattr(sys, "argv", ["database", *args])
        assert dbcli.main() == 0
        output = json.loads(capsys.readouterr().out)
        assert "error" not in output
    monkeypatch.setattr(sys, "argv", ["database", "query", "TEST", "revenue", "--basis", "annual"])
    assert dbcli.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["value"] == "125"
    assert output["status"] == "RECENT"
    assert output["source"]["filing_url"].endswith("annual.htm")
    with pg.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(db.data_fetch_logs)) == 2
    with pg.begin() as connection:
        connection.execute(
            db.data_source_state.update().values(status="STALE", error_message="503")
        )
        assert financial_value(connection, "TEST", "revenue", basis="annual")["status"] == "STALE"


def test_query_invalid_inputs_and_absent_company(pg: sa.Engine) -> None:
    with pg.connect() as connection:
        assert financial_value(connection, "MISSING", "revenue", basis="annual")["value"] is None
        with pytest.raises(ValueError):
            financial_value(connection, "TEST", "unknown", basis="annual")
        with pytest.raises(ValueError):
            financial_value(connection, "TEST", "revenue", basis="other")
        with pytest.raises(ValueError):
            financial_value(
                connection, "TEST", "revenue", basis="annual", as_of=datetime(2025, 1, 1)
            )
