from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.check_config import main
from app.core.config import Settings


def test_defaults_disable_external_access() -> None:
    settings = Settings(_env_file=None)
    assert not settings.sec_enabled
    assert settings.market_data_provider == "disabled"
    assert settings.database_url is None


def test_example_is_valid_without_credentials() -> None:
    root = Path(__file__).resolve().parents[3]
    settings = Settings(_env_file=root / ".env.example")
    assert settings.market_data_provider == "disabled"


def test_environment_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("API_PORT=8001\n", encoding="utf-8")
    monkeypatch.setenv("API_PORT", "9000")
    assert Settings(_env_file=env).api_port == 9000


@pytest.mark.parametrize(
    "name,value",
    [
        ("API_PORT", "0"),
        ("SEC_REQUESTS_PER_SECOND", "11"),
        ("SEC_REQUESTS_PER_SECOND", "0"),
        ("REQUEST_TIMEOUT_SECONDS", "-1"),
        ("MARKET_DATA_PROVIDER", "unknown"),
        ("DATABASE_URL", "sqlite:///test.db"),
        ("REDIS_URL", "https://example.com"),
        ("QDRANT_URL", "https://user:password@example.com"),
        ("CORS_ORIGINS", '["*"]'),
        ("CORS_ORIGINS", '["https://example.com/path"]'),
    ],
)
def test_invalid_settings_fail(name: str, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_sec_requires_identification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_ENABLED", "true")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("SEC_USER_AGENT", "MarketIQ developer@example.com")
    assert Settings(_env_file=None).sec_enabled


def test_market_requires_keys_and_honest_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "alpaca")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("MARKET_API_KEY", "test-key")
    monkeypatch.setenv("MARKET_API_SECRET", "test-secret")
    monkeypatch.setenv("MARKET_DATA_FEED", "delayed_sip")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("MARKET_DECLARED_DELAY_SECONDS", "900")
    assert Settings(_env_file=None).market_declared_delay_seconds == 900


def test_credentials_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKET_API_KEY", "private-sentinel")
    settings = Settings(_env_file=None)
    assert "private-sentinel" not in repr(settings)
    assert "private-sentinel" not in settings.model_dump_json()


def test_production_debug_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_cli_errors_do_not_expose_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "invalid://secret-sentinel")
    assert main() == 1
    output = capsys.readouterr().out
    assert "secret-sentinel" not in output
    assert '"configuration": "invalid"' in output
