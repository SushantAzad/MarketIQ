"""Discovery and durable primary-document processing. Financial normalization is Phase 3."""

import re
from typing import Any

from app.ingestion.parser import PARSER_VERSION, parse_document
from app.ingestion.store import IngestionStore
from app.providers.sec.client import SecClient, SecError
from app.providers.sec.models import Filing, normalize_cik, parse_filings

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


class SecPipeline:
    def __init__(self, client: SecClient, store: IngestionStore) -> None:
        self.client = client
        self.store = store

    def resolve(self, tickers: list[str]) -> dict[str, str]:
        payload = self.client.json(TICKERS_URL)
        try:
            registry = {
                str(row["ticker"]).upper(): normalize_cik(row["cik_str"])
                for row in payload.values()
            }
            return {ticker: registry[ticker] for ticker in tickers}
        except (KeyError, TypeError, ValueError) as exc:
            self.store.invalidate(TICKERS_URL, "Invalid ticker registry or unknown ticker")
            raise SecError("Invalid SEC ticker registry or unknown requested ticker") from exc

    def discover(self, cik: str, *, backfill: bool = False) -> list[Filing]:
        cik = normalize_cik(cik)
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        payload = self.client.json(url)
        try:
            if normalize_cik(payload["cik"]) != cik:
                raise ValueError("SEC issuer identity mismatch")
            records = parse_filings(cik, payload["filings"]["recent"])
            if backfill:
                for page in payload["filings"].get("files", []):
                    name = page["name"]
                    if not re.fullmatch(rf"CIK{cik}-submissions-\d+\.json", name):
                        raise ValueError("Invalid historical submissions filename")
                    page_url = f"https://data.sec.gov/submissions/{name}"
                    historical = self.client.json(page_url)
                    try:
                        records.extend(parse_filings(cik, historical))
                    except (ValueError, TypeError, KeyError):
                        self.store.invalidate(page_url, "Invalid historical submissions schema")
                        raise
            unique = {filing.accession_number: filing for filing in records}
            records = sorted(
                unique.values(), key=lambda f: (f.accepted_at, f.accession_number), reverse=True
            )
        except (ValueError, TypeError, KeyError) as exc:
            self.store.invalidate(url, "Invalid SEC submissions schema")
            raise SecError("Invalid SEC submissions schema") from exc
        for filing in records:
            self.store.discover(filing)
        return records

    def process(self, filing: Filing) -> str:
        state = self.store.filing_state(filing)
        try:
            if state["stage"] == "parsed" and state["parser_version"] == PARSER_VERSION:
                # Validate durable artifacts before declaring an idempotent skip.
                self.store.read(state["raw_hash"])
                self.store.read(state["parsed_hash"])
                return "skipped"
            self.store.stage(filing, "processing")
            if state["raw_hash"]:
                content = self.store.read(state["raw_hash"])
                digest = state["raw_hash"]
            else:
                content, digest = self.client.fetch(filing.url, "document")
                self.store.stage(filing, "downloaded", raw_hash=digest)
            parsed = parse_document(content, filing.url)
            parsed["filing"] = filing.model_dump(mode="json")
            parsed["raw_hash"] = digest
            parsed_hash = self.store.save_json(parsed)
            self.store.stage(
                filing,
                "parsed",
                parsed_hash=parsed_hash,
                parser_version=PARSER_VERSION,
            )
            return "parsed"
        except (ValueError, OSError, SecError) as exc:
            # Type-only error avoids leaking document contents or local credentials.
            self.store.stage(filing, "failed", error=type(exc).__name__)
            if isinstance(exc, ValueError):
                self.store.invalidate(filing.url, "Document validation failed")
                self.store.clear_artifacts(filing)
            elif isinstance(exc, OSError):
                self.store.clear_artifacts(filing)
            raise

    def companyfacts(self, cik: str) -> None:
        cik = normalize_cik(cik)
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        payload = self.client.json(url)
        try:
            valid = normalize_cik(payload.get("cik", "")) == cik and isinstance(
                payload.get("facts"), dict
            )
        except ValueError:
            valid = False
        if not valid:
            self.store.invalidate(url, "Invalid companyfacts identity or schema")
            raise SecError("Invalid companyfacts identity or schema")

    def run(
        self,
        tickers: list[str],
        *,
        limit: int,
        backfill: bool = False,
        forms: set[str] | None = None,
    ) -> dict[str, Any]:
        issuers = self.resolve(tickers)
        report: dict[str, Any] = {"companies": {}, "errors": []}
        for cik in dict.fromkeys(issuers.values()):
            result = {"discovered": 0, "parsed": 0, "skipped": 0, "failed": 0}
            report["companies"][cik] = result
            try:
                filings = self.discover(cik, backfill=backfill)
                result["discovered"] = len(filings)
            except (SecError, ValueError) as exc:
                report["errors"].append({"cik": cik, "stage": "discovery", "error": str(exc)})
                continue
            pending = 0
            for filing in filings:
                if forms is not None and filing.form_type not in forms:
                    continue
                state = self.store.filing_state(filing)
                complete = state["stage"] == "parsed" and state["parser_version"] == PARSER_VERSION
                if not complete:
                    if pending >= limit:
                        continue
                    pending += 1
                try:
                    result[self.process(filing)] += 1
                except (SecError, ValueError, OSError):
                    result["failed"] += 1
            try:
                self.companyfacts(cik)
            except (SecError, ValueError) as exc:
                report["errors"].append({"cik": cik, "stage": "companyfacts", "error": str(exc)})
        return report
