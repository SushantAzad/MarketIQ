import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from app.core.config import Settings
from app.database import __main__ as cli
from app.services.financial_import import import_snapshot
from app.services.financial_query import financial_value

pytestmark = pytest.mark.integration


def test_sync_refreshes_provider_then_imports(
    pg: sa.Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        _env_file=None,
        sec_enabled=True,
        sec_user_agent="Test test@example.com",
        sec_state_path=tmp_path / "state",
        raw_storage_path=tmp_path / "raw",
    )
    original_client = httpx.Client
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("company_tickers.json"):
            return httpx.Response(200, json={"0": {"ticker": "TEST", "cik_str": 1}})
        if "companyfacts" in request.url.path:
            return httpx.Response(200, json={"cik": 1, "entityName": "Test issuer", "facts": {}})
        return httpx.Response(
            200,
            json={
                "cik": 1,
                "filings": {
                    "recent": {
                        "accessionNumber": [],
                        "form": [],
                        "filingDate": [],
                        "acceptanceDateTime": [],
                        "primaryDocument": [],
                    }
                },
            },
        )

    def http_factory(**kwargs: object) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(respond)
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", http_factory)
    monkeypatch.setattr(cli, "Settings", lambda **kwargs: settings)
    monkeypatch.setattr(cli, "database_engine", lambda settings: pg)
    monkeypatch.setattr(sys, "argv", ["database", "sync", "--enable-sec", "--tickers", "TEST"])
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["upstream_refreshed"] is True
    assert len(seen) == 3
    assert result["imports"][0]["accepted"] == 0


def test_fractional_numeric_round_trip(pg: sa.Engine) -> None:
    body = b"""{"cik":1,"entityName":"Test issuer","facts":{"us-gaap":{"Revenues":{"units":{
    "USD":[{"val":9007199254740993.123456789,"accn":"0000000001-25-000001","form":"10-K",
    "filed":"2025-02-01","start":"2024-01-01","end":"2024-12-31","fy":2025,"fp":"FY"}]}}}}}"""
    source = {
        "source_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
        "content_hash": hashlib.sha256(body).hexdigest(),
        "first_fetched_at": "2025-02-02T00:00:00Z",
    }
    with pg.begin() as connection:
        import_snapshot(connection, body, source, tickers=["TEST"])
        assert (
            financial_value(connection, "TEST", "revenue", basis="annual")["value"]
            == "9007199254740993.123456789"
        )
