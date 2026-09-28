"""Bounded, identified SEC requests with durable fetch history and no redirects."""

import json
import logging
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings
from app.ingestion.store import IngestionStore

logger = logging.getLogger(__name__)


class SecError(RuntimeError):
    """Safe, bounded error message; never includes request headers."""


def validate_url(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "%" in parts.path
        or ".." in parts.path
    ):
        raise SecError("Invalid SEC URL")
    valid = (
        parts.hostname == "data.sec.gov"
        and re.fullmatch(
            r"/(?:submissions/[A-Za-z0-9_.-]+\.json|api/xbrl/companyfacts/CIK\d{10}\.json)",
            parts.path,
        )
    ) or (
        parts.hostname == "www.sec.gov"
        and (
            parts.path == "/files/company_tickers.json"
            or re.fullmatch(r"/Archives/edgar/data/\d+/\d{18}/[A-Za-z0-9_.-]+", parts.path)
        )
    )
    if not valid:
        raise SecError("URL is outside supported SEC endpoints")


def retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


class SecClient:
    def __init__(
        self,
        settings: Settings,
        store: IngestionStore,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not settings.sec_enabled:
            raise SecError("SEC access is disabled; use SEC_ENABLED=true or CLI --enable-sec")
        self.settings = settings
        self.store = store
        self.sleep = sleep
        self.last_request = 0.0
        self.http = httpx.Client(
            headers={"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()

    def fetch(self, url: str, kind: Literal["json", "document"]) -> tuple[bytes, str]:
        validate_url(url)
        for attempt in range(self.settings.provider_max_retries + 1):
            cooldown = self.store.cooldown() - time.time()
            if cooldown > 60:
                raise SecError(f"SEC cooldown active; retry after {cooldown:.0f} seconds")
            if cooldown > 0:
                self.sleep(cooldown)
            wait = 1 / self.settings.sec_requests_per_second - (
                time.monotonic() - self.last_request
            )
            if wait > 0:
                self.sleep(wait)
            attempted = self.store.attempted(url)
            started = time.monotonic()
            self.last_request = started
            status: int | None = None
            retry = False
            delay = 0.0
            try:
                with self.http.stream("GET", url) as response:
                    status = response.status_code
                    retry = status == 429 or status >= 500
                    delay = retry_after(response.headers.get("Retry-After"))
                    if retry and delay > 0:
                        self.store.defer(time.time() + delay)
                    if status != 200:
                        raise SecError(f"SEC HTTP {status}")
                    media = response.headers.get("Content-Type", "").split(";")[0].lower()
                    expected = (
                        {"application/json"}
                        if kind == "json"
                        else {
                            "text/html",
                            "text/plain",
                            "application/xhtml+xml",
                        }
                    )
                    if media not in expected:
                        raise SecError("Unexpected SEC content type")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > self.settings.sec_max_document_bytes:
                            raise SecError("SEC response exceeds size limit")
                    content = bytes(body)
                    if not content.strip():
                        raise SecError("Empty SEC response")
                    if kind == "json":
                        try:
                            payload = json.loads(content)
                        except (ValueError, UnicodeError) as exc:
                            raise SecError("Invalid SEC JSON") from exc
                        if not isinstance(payload, dict):
                            raise SecError("SEC JSON must be an object")
                    digest = self.store.save(content)
                    self.store.fetched(
                        url,
                        attempted,
                        attempt,
                        (time.monotonic() - started) * 1000,
                        digest=digest,
                        http_status=status,
                        source_timestamp=response.headers.get("Last-Modified"),
                    )
                    logger.info(
                        "sec_fetch", extra={"url": url, "status": status, "attempt": attempt}
                    )
                    return content, digest
            except (SecError, httpx.TransportError) as exc:
                if isinstance(exc, httpx.TransportError):
                    retry = True
                    error = f"SEC transport error: {type(exc).__name__}"
                else:
                    error = str(exc)
                self.store.fetched(
                    url,
                    attempted,
                    attempt,
                    (time.monotonic() - started) * 1000,
                    error=error,
                    http_status=status,
                )
                logger.warning("sec_fetch_failed", extra={"url": url, "error_code": error})
                if not retry or attempt == self.settings.provider_max_retries:
                    raise SecError(error) from exc
                # Long server cooldowns are surfaced, never shortened into an early retry.
                if delay > 60:
                    raise SecError(
                        f"SEC cooldown requested; retry after {delay:.0f} seconds"
                    ) from exc
                self.sleep(max(delay, min(30, 2**attempt + random.uniform(0, 1))))
        raise SecError("SEC retry budget exhausted")

    def json(self, url: str) -> dict[str, Any]:
        body, _ = self.fetch(url, "json")
        result: dict[str, Any] = json.loads(body)
        return result
