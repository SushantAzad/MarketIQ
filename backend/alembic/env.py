"""Credentials are loaded from Settings, never persisted in alembic.ini."""

from alembic import context
from app.core.config import Settings
from app.database.connection import database_engine
from app.database.schema import metadata

connection = context.config.attributes.get("connection")
if connection is not None:
    context.configure(connection=connection, target_metadata=metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = database_engine(Settings())
    try:
        if context.is_offline_mode():
            context.configure(url=engine.url, target_metadata=metadata, literal_binds=True)
            with context.begin_transaction():
                context.run_migrations()
        else:
            with engine.connect() as connection:
                context.configure(
                    connection=connection, target_metadata=metadata, compare_type=True
                )
                with context.begin_transaction():
                    context.run_migrations()
    finally:
        engine.dispose()
