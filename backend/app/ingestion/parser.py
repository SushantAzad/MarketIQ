"""Safe text extraction; no HTML execution, external requests, or invented pages."""

import re
from typing import Any

from bs4 import BeautifulSoup

PARSER_VERSION = "sec-html-v1"


def parse_document(content: bytes, source_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(content, "html.parser")
    for element in soup.find_all(["script", "style", "noscript", "head", "ix:header"]):
        element.decompose()
    for element in soup.select('[hidden], [aria-hidden="true"]'):
        element.decompose()
    for element in soup.find_all(style=True):
        if element.attrs is None:
            continue
        style = str(element.get("style", "")).replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            element.decompose()
    text = "\n".join(line for raw in soup.get_text("\n").splitlines() if (line := raw.strip()))
    if len(text) < 80:
        raise ValueError("Filing contains insufficient extractable text")
    if (
        "undeclared automated tool" in text.lower()
        or "request rate threshold exceeded" in text.lower()
    ):
        raise ValueError("SEC access-denial page is not a filing")
    headings = []
    for match in re.finditer(r"(?im)^item\s+\d+[a-z]?\.?[^\n]{0,160}", text):
        headings.append({"heading": match.group(), "start": match.start(), "end": match.end()})
    tables = [table.get_text(" | ", strip=True) for table in soup.find_all("table")]
    return {
        "parser_version": PARSER_VERSION,
        "source_url": source_url,
        "text": text,
        "headings": headings,
        "tables": tables,
        "page": None,
        "offset_basis": "normalized_text",
        "section_detection": "heuristic",
    }
