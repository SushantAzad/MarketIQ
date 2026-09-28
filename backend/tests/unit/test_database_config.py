import json
import sys

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.database import __main__ as cli
from app.database.connection import database_engine


def test_database_has_no_silent_fallback() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        database_engine(Settings(_env_file=None))


def test_connection_selects_psycopg_without_connecting() -> None:
    engine = database_engine(
        Settings(
            _env_file=None, database_url=SecretStr("postgresql://test:private@localhost:5432/test")
        )
    )
    try:
        assert engine.dialect.driver == "psycopg"
        assert "private" not in str(engine.url)
    finally:
        engine.dispose()


def test_database_cli_failure_does_not_print_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["database", "status"])

    def fail(settings: Settings) -> None:
        raise ValueError("private credential")

    monkeypatch.setattr(cli, "database_engine", fail)
    assert cli.main() == 1
    output = capsys.readouterr().out
    assert "private credential" not in output
    assert json.loads(output)["error"] == "ValueError"
