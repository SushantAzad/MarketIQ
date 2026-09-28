"""Validated configuration. Importing this module performs no I/O or provider calls."""

from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Phase-one settings; integration credentials remain optional until enabled."""

    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    request_timeout_seconds: float = Field(default=20, gt=0, le=120)

    database_url: SecretStr | None = None
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: SecretStr | None = None
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_revision: str = Field(
        default="1110a243fdf4706b3f48f1d95db1a4f5529b4d41", pattern=r"^[0-9a-f]{40}$"
    )
    embedding_cache_path: Path = REPOSITORY_ROOT / "data" / "models"
    embedding_batch_size: int = Field(default=32, ge=1, le=256)
    chunk_tokens: int = Field(default=220, ge=16, le=8192)
    chunk_overlap: int = Field(default=32, ge=0, le=1024)
    raw_storage_path: Path = REPOSITORY_ROOT / "data" / "raw"

    llm_provider: Literal["disabled", "compatible"] = "disabled"
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = Field(default=60, gt=0, le=300)
    llm_max_output_tokens: int = Field(default=512, ge=64, le=4096)
    rag_top_k: int = Field(default=6, ge=1, le=12)
    rag_min_score: float = Field(default=0.3, ge=-1, le=1)
    rag_max_context_chars: int = Field(default=12000, ge=1000, le=50000)

    sec_enabled: bool = False
    sec_user_agent: str = ""
    sec_requests_per_second: float = Field(default=5, gt=0, le=10)
    sec_poll_seconds: int = Field(default=300, ge=60)
    sec_max_document_bytes: int = Field(default=50_000_000, gt=0)
    sec_state_path: Path = REPOSITORY_ROOT / "data" / "sec"

    market_data_provider: Literal["disabled", "alpaca"] = "disabled"
    market_data_feed: Literal["iex", "sip", "delayed_sip"] = "iex"
    market_api_key: SecretStr | None = None
    market_api_secret: SecretStr | None = None
    market_requests_per_minute: int = Field(default=200, ge=1)
    market_declared_delay_seconds: int = Field(default=0, ge=0)
    quote_cache_ttl_seconds: int = Field(default=5, ge=1, le=60)
    provider_max_retries: int = Field(default=4, ge=0, le=10)

    @field_validator("cors_origins")
    @classmethod
    def validate_origins(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("CORS entries must be explicit HTTP(S) origins without paths")
        return origins

    @field_validator("database_url")
    @classmethod
    def validate_database(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            parsed = urlsplit(value.get_secret_value())
            if parsed.scheme not in {"postgresql", "postgresql+psycopg"} or not parsed.hostname:
                raise ValueError("DATABASE_URL must be a PostgreSQL connection URL")
        return value

    @field_validator("redis_url")
    @classmethod
    def validate_redis(cls, value: SecretStr) -> SecretStr:
        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("REDIS_URL must be a Redis connection URL")
        return value

    @field_validator("qdrant_url")
    @classmethod
    def validate_qdrant(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("QDRANT_URL must be HTTP(S), with credentials in QDRANT_API_KEY")
        return value

    @model_validator(mode="after")
    def validate_integrations(self) -> Self:
        if self.llm_base_url is not None:
            parsed = urlsplit(self.llm_base_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("LLM_BASE_URL must be HTTP(S) without credentials or query")
            if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Remote model endpoints require HTTPS")
        if self.llm_provider != "disabled" and (
            not self.llm_base_url or not self.llm_model or not self.llm_model.strip()
        ):
            raise ValueError("Enabled LLM requires LLM_BASE_URL and LLM_MODEL")
        if self.sec_enabled and (
            "@" not in self.sec_user_agent or len(self.sec_user_agent.split()) < 2
        ):
            raise ValueError("Enabled SEC access requires an application name and contact email")
        if self.market_data_provider == "alpaca":
            if not all(
                value and value.get_secret_value().strip()
                for value in (self.market_api_key, self.market_api_secret)
            ):
                raise ValueError("Alpaca requires MARKET_API_KEY and MARKET_API_SECRET")
            if self.market_data_feed == "delayed_sip" and self.market_declared_delay_seconds < 900:
                raise ValueError("Delayed SIP requires a declared delay of at least 900 seconds")
        if self.app_env == "production" and self.log_level == "DEBUG":
            raise ValueError("Production DEBUG logging is disabled to limit sensitive diagnostics")
        return self


def load_settings() -> Settings:
    """Load once at each future process entry point, rather than at import time."""
    return Settings()
