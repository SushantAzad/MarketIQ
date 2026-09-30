"""Reproducible candidate sampling with explicit selection and survivorship limits."""

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from filelock import FileLock

from app.core.config import Settings
from app.ingestion.pipeline import TICKERS_URL
from app.ingestion.store import IngestionStore
from app.providers.sec.client import SecClient
from app.providers.sec.models import normalize_cik

VERSION = "nonfinancial-us-cohort-v1"
US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)


def classify(payload: dict[str, Any]) -> str | None:
    try:
        sic = int(payload.get("sic", 0))
    except (TypeError, ValueError):
        return "missing_sic"
    if not 100 <= sic <= 9999:
        return "missing_sic"
    if 6000 <= sic <= 6999:
        return "financial_sector"
    if payload.get("stateOfIncorporation") not in US_STATES:
        return "not_confirmed_us_incorporated"
    if "10-K" not in payload.get("filings", {}).get("recent", {}).get("form", []):
        return "no_recent_10k"
    return None


def collect(
    settings: Settings, *, limit: int = 80, historical_ciks: list[str] | None = None
) -> dict[str, Any]:
    if not 1 <= limit <= 200:
        raise ValueError("Candidate request must be bounded to 1..200")
    settings.sec_state_path.mkdir(parents=True, exist_ok=True)
    with FileLock(settings.sec_state_path / "ingestion.lock", timeout=0):
        store = IngestionStore(settings.sec_state_path, settings.raw_storage_path)
        client = SecClient(settings, store)
        try:
            registry = client.json(TICKERS_URL)
            candidates = {normalize_cik(row["cik_str"]): row for row in registry.values()}
            chosen = sorted(
                candidates, key=lambda cik: hashlib.sha256((VERSION + cik).encode()).hexdigest()
            )[:limit]
            historical = {normalize_cik(cik) for cik in (historical_ciks or [])}
            chosen = list(dict.fromkeys(chosen + sorted(historical)))
            if len(chosen) > 250:
                raise ValueError("Total candidate limit exceeded")
            rows = []
            for cik in chosen:
                url = f"https://data.sec.gov/submissions/CIK{cik}.json"
                payload = client.json(url)
                if normalize_cik(payload["cik"]) != cik:
                    raise ValueError("SEC issuer mismatch")
                resource = dict(
                    store.db.execute("SELECT * FROM resources WHERE url=?", (url,)).fetchone()
                )
                rows.append(
                    {
                        "cik": cik,
                        "name": payload.get("name"),
                        "sic": payload.get("sic"),
                        "tickers": payload.get("tickers", []),
                        "exclusion": classify(payload),
                        "candidate_origin": "explicit_historical_cik"
                        if cik in historical
                        else "current_ticker_registry",
                        "source_url": url,
                        "content_hash": resource["data_version"],
                        "observed_at": resource["last_successful_fetch"],
                    }
                )
            registry_hash = store.db.execute(
                "SELECT data_version FROM resources WHERE url=?", (TICKERS_URL,)
            ).fetchone()[0]
            report = {
                "version": VERSION,
                "recorded_at": datetime.now(UTC).isoformat(),
                "registry_hash": registry_hash,
                "sampling": "deterministic SHA256 order, unique CIK",
                "historical_inactive_coverage_verified": False,
                "limitations": [
                    "Current registry selection has survivorship bias",
                    "SIC and incorporation describe current metadata, not historical membership",
                    "Explicit historical CIKs do not by themselves verify inactive-issuer coverage",
                ],
                "candidates": rows,
                "eligible_candidates": sum(r["exclusion"] is None for r in rows),
                "exclusions": dict(Counter(r["exclusion"] for r in rows if r["exclusion"])),
                "sic_major_groups": dict(
                    Counter(str(int(r["sic"]) // 1000) for r in rows if r["exclusion"] is None)
                ),
            }
            target = settings.raw_storage_path.parent / "risk/cohort.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, indent=2), encoding="utf-8")
            return report
        finally:
            client.close()
            store.db.close()
