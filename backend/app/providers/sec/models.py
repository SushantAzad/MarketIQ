"""Validate EDGAR identities before constructing archive URLs or local paths."""

import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

FORMS = {"10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A"}
INITIAL_TICKERS = ("NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA")


class Filing(BaseModel):
    cik: str = Field(pattern=r"^\d{10}$")
    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    form_type: str
    filing_date: date
    accepted_at: datetime
    report_period: date | None = None
    primary_document: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

    @field_validator("primary_document")
    @classmethod
    def safe_document(cls, value: str) -> str:
        if ".." in value or not value.lower().endswith((".htm", ".html", ".txt")):
            raise ValueError("Unsupported primary document name")
        return value

    @field_validator("accepted_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Acceptance timestamp must include timezone")
        return value

    @property
    def url(self) -> str:
        accession = self.accession_number.replace("-", "")
        return (
            f"https://www.sec.gov/Archives/edgar/data/{int(self.cik)}/"
            f"{accession}/{self.primary_document}"
        )


def normalize_cik(value: str | int) -> str:
    text = str(value)
    if not re.fullmatch(r"\d{1,10}", text) or int(text) == 0:
        raise ValueError("Invalid SEC CIK")
    return text.zfill(10)


def parse_filings(cik: str, columns: dict[str, Any]) -> list[Filing]:
    if not isinstance(columns, dict):
        raise ValueError("SEC submissions must contain column arrays")
    required = ("accessionNumber", "form", "filingDate", "acceptanceDateTime", "primaryDocument")
    if any(not isinstance(columns.get(key), list) for key in required):
        raise ValueError("SEC submissions arrays are missing")
    count = len(columns["accessionNumber"])
    if any(len(columns[key]) != count for key in required):
        raise ValueError("SEC submissions arrays have inconsistent lengths")
    periods = columns.get("reportDate", [""] * count)
    if not isinstance(periods, list) or len(periods) != count:
        raise ValueError("SEC report dates have inconsistent lengths")
    filings = []
    for index in range(count):
        if columns["form"][index] not in FORMS:
            continue
        filings.append(
            Filing(
                cik=cik,
                accession_number=columns["accessionNumber"][index],
                form_type=columns["form"][index],
                filing_date=columns["filingDate"][index],
                accepted_at=columns["acceptanceDateTime"][index],
                primary_document=columns["primaryDocument"][index],
                report_period=periods[index] or None,
            )
        )
    return sorted(filings, key=lambda item: (item.accepted_at, item.accession_number), reverse=True)
