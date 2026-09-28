"""Refresh upstream metadata/facts before a financial import; no filing downloads required."""

from typing import Any

from filelock import FileLock

from app.core.config import Settings
from app.ingestion.pipeline import SecPipeline
from app.ingestion.store import IngestionStore
from app.providers.sec.client import SecClient


def refresh_journal(settings: Settings, tickers: list[str]) -> dict[str, Any]:
    settings.sec_state_path.mkdir(parents=True, exist_ok=True)
    with FileLock(settings.sec_state_path / "ingestion.lock", timeout=0):
        store = IngestionStore(settings.sec_state_path, settings.raw_storage_path)
        try:
            client = SecClient(settings, store)
            try:
                return SecPipeline(client, store).run(tickers, limit=0)
            finally:
                client.close()
        finally:
            store.close()
