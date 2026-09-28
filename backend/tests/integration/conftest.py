import os
import re
from collections.abc import Iterator
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from app.core.config import REPOSITORY_ROOT


def migration_config(connection: sa.Connection) -> Config:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


@pytest.fixture
def pg() -> Iterator[sa.Engine]:
    url = os.environ.get("MARKETIQ_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set MARKETIQ_TEST_DATABASE_URL for real PostgreSQL integration tests")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("Integration tests require PostgreSQL, not a substitute")
    namespace = "miq_test_" + uuid4().hex
    assert re.fullmatch(r"miq_test_[0-9a-f]{32}", namespace)
    admin = sa.create_engine(parsed.set(drivername="postgresql+psycopg"), hide_parameters=True)
    with admin.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{namespace}"'))
    engine = sa.create_engine(
        parsed.set(drivername="postgresql+psycopg"),
        hide_parameters=True,
        connect_args={"options": f"-csearch_path={namespace}", "connect_timeout": 10},
    )
    try:
        with engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        yield engine
    finally:
        engine.dispose()
        # Only remove the randomly generated test schema, never public or application tables.
        with admin.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{namespace}" CASCADE'))
        admin.dispose()
