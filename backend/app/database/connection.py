"""One configured PostgreSQL connection factory; never silently falls back to SQLite."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url

from app.core.config import Settings


def database_engine(settings: Settings) -> Engine:
    if settings.database_url is None:
        raise ValueError("DATABASE_URL is required for PostgreSQL commands")
    url = make_url(settings.database_url.get_secret_value()).set(drivername="postgresql+psycopg")
    return create_engine(
        url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 10, "options": "-c timezone=UTC"},
        pool_size=3,
        max_overflow=2,
    )
