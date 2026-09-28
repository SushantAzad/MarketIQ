import json
import sys
from pathlib import Path

import httpx
import pytest

from app.ingestion import __main__ as cli
from app.providers.sec.client import SecError


@pytest.fixture
def cli_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_STATE_PATH", str(tmp_path / "state"))
    monkeypatch.setenv("RAW_STORAGE_PATH", str(tmp_path / "raw"))
    monkeypatch.setenv("SEC_ENABLED", "false")
    monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")
    # Logging is tested separately; don't replace pytest's capture handlers here.
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)


def test_status_requires_no_network(
    cli_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["ingestion", "status"])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["resources"] == []


def test_disabled_access_exits_cleanly(
    cli_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["ingestion", "run"])
    assert cli.main() == 1
    assert "disabled" in capsys.readouterr().out


def test_invalid_identification_exits_without_contact(
    cli_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "private@example.com")
    monkeypatch.setattr(sys, "argv", ["ingestion", "run", "--enable-sec"])
    assert cli.main() == 2
    assert "private@example.com" not in capsys.readouterr().out


def test_invalid_limit(cli_environment: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["ingestion", "run", "--limit", "0"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_cli_run_uses_identified_transport(
    cli_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_client = httpx.Client

    def response(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"] == "Test test@example.com"
        if request.url.path.endswith("company_tickers.json"):
            return httpx.Response(200, json={"0": {"ticker": "TEST", "cik_str": 1}})
        if "companyfacts" in request.url.path:
            return httpx.Response(200, json={"cik": 1, "facts": {}})
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

    def factory(**kwargs: object) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(response)
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    monkeypatch.setattr(sys, "argv", ["ingestion", "run", "--enable-sec", "--tickers", "TEST"])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["errors"] == []


def test_watch_recovers_from_registry_failure(
    cli_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing_run(*args: object, **kwargs: object) -> None:
        raise SecError("SEC HTTP 503")

    def interrupt(delay: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.SecPipeline, "run", failing_run)
    monkeypatch.setattr(cli.time, "sleep", interrupt)
    monkeypatch.setattr(sys, "argv", ["ingestion", "watch", "--enable-sec"])
    assert cli.main() == 130
    assert "503" in capsys.readouterr().out
