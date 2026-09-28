import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.ingestion.parser import parse_document
from app.ingestion.pipeline import TICKERS_URL, SecPipeline
from app.ingestion.store import IngestionStore
from app.providers.sec.client import SecClient, SecError, retry_after, validate_url
from app.providers.sec.models import normalize_cik, parse_filings

# Synthetic fixtures are confined to automated tests, never runtime data.
CIK = "0000000001"
SUBMISSIONS = f"https://data.sec.gov/submissions/CIK{CIK}.json"
FACTS = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json"
HTML = b"""<html><head><title>Ignored</title></head><body>
<script>secret script</script><div style="display: none">hidden value</div>
<h2>Item 1A. Risk Factors</h2><p>A test issuer describes operational uncertainty
and dependence on external suppliers in this synthetic test-only document.</p>
<table><tr><td>Label</td><td>Test value</td></tr></table></body></html>"""


def columns() -> dict[str, list[str]]:
    return {
        "accessionNumber": ["0000000001-25-000002", "0000000001-25-000001"],
        "form": ["10-Q", "10-K"],
        "filingDate": ["2025-06-01", "2025-03-01"],
        "acceptanceDateTime": ["2025-06-01T12:00:00Z", "2025-03-01T12:00:00Z"],
        "primaryDocument": ["quarter.htm", "annual.htm"],
        "reportDate": ["2025-03-31", "2024-12-31"],
    }


@pytest.fixture
def store(tmp_path: Path) -> Iterator[IngestionStore]:
    instance = IngestionStore(tmp_path / "state", tmp_path / "raw")
    yield instance
    instance.close()


def settings(**overrides: object) -> Settings:
    values = {"sec_enabled": True, "sec_user_agent": "Test test@example.com", **overrides}
    return Settings(_env_file=None, **values)


def test_parser_omits_executable_hidden_content_and_preserves_tables() -> None:
    parsed = parse_document(HTML, "https://www.sec.gov/test")
    assert "secret script" not in parsed["text"]
    assert "hidden value" not in parsed["text"]
    assert parsed["page"] is None
    assert parsed["tables"] == ["Label | Test value"]
    heading = parsed["headings"][0]
    assert parsed["text"][heading["start"] : heading["end"]] == heading["heading"]


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"<html>short</html>",
        b"Your request originates from an undeclared automated tool. " * 3,
    ],
)
def test_invalid_documents_are_rejected(body: bytes) -> None:
    with pytest.raises(ValueError):
        parse_document(body, "test")


def test_latest_filing_selection_and_amendments() -> None:
    data = columns()
    data["form"][1] = "10-K/A"
    filings = parse_filings(CIK, data)
    assert filings[0].form_type == "10-Q"
    assert filings[1].form_type == "10-K/A"
    assert filings[0].url.endswith("/000000000125000002/quarter.htm")


@pytest.mark.parametrize("change", ["length", "missing", "path", "timezone", "period"])
def test_invalid_submissions(change: str) -> None:
    data = columns()
    if change == "length":
        data["form"].pop()
    elif change == "missing":
        del data["form"]
    elif change == "path":
        data["primaryDocument"][0] = "../../outside.htm"
    elif change == "timezone":
        data["acceptanceDateTime"][0] = "2025-06-01T12:00:00"
    else:
        data["reportDate"].pop()
    with pytest.raises(ValueError):
        parse_filings(CIK, data)


def test_unsupported_forms_excluded() -> None:
    data = columns()
    data["form"][0] = "4"
    assert len(parse_filings(CIK, data)) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/submissions/CIK0000000001.json",
        "http://data.sec.gov/submissions/CIK0000000001.json",
        "https://data.sec.gov:8080/submissions/CIK0000000001.json",
        "https://user:password@data.sec.gov/submissions/CIK0000000001.json",
        "https://www.sec.gov/Archives/edgar/data/1/../secret",
        "https://www.sec.gov/Archives/edgar/data/1/%2e%2e/secret",
        SUBMISSIONS + "?redirect=elsewhere",
    ],
)
def test_outbound_allowlist(url: str) -> None:
    with pytest.raises(SecError):
        validate_url(url)


@pytest.mark.parametrize("value", ["../1", "0", "12345678901", "company"])
def test_invalid_cik(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_cik(value)


def test_duplicate_detection_survives_reopen(tmp_path: Path) -> None:
    filing = parse_filings(CIK, columns())[0]
    for expected in (True, False):
        state = IngestionStore(tmp_path / "state", tmp_path / "raw")
        assert state.discover(filing) is expected
        state.close()


def test_storage_hash_validation(store: IngestionStore) -> None:
    digest = store.save(HTML)
    assert store.save(HTML) == digest
    assert store.read(digest) == HTML
    with pytest.raises(ValueError):
        store.read("../bad")
    (store.raw_directory / digest[:2] / digest).write_bytes(b"corrupted")
    with pytest.raises(ValueError):
        store.read(digest)
    with pytest.raises(ValueError):
        store.save(HTML)


def test_retry_logging_and_fallback_freshness(store: IngestionStore) -> None:
    calls = []
    sleeps = []
    responses = [429, 200, 503, 503]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        code = responses.pop(0)
        return httpx.Response(code, json={"cik": 1}, headers={"Retry-After": "2"})

    client = SecClient(
        settings(provider_max_retries=1),
        store,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    try:
        client.json(SUBMISSIONS)
        assert any(delay >= 2 for delay in sleeps)
        assert calls[0].headers["User-Agent"] == "Test test@example.com"
        original = store.summary(600)["resources"][0]
        with pytest.raises(SecError, match="503"):
            client.json(SUBMISSIONS)
        fallback = store.summary(600)["resources"][0]
        assert fallback["status"] == "STALE"
        assert fallback["data_version"] == original["data_version"]
        assert fallback["last_successful_fetch"] == original["last_successful_fetch"]
        assert store.db.execute("SELECT COUNT(*) FROM fetch_logs").fetchone()[0] == 4
    finally:
        client.close()


@pytest.mark.parametrize(
    "code,media,body",
    [
        (403, "text/html", b"blocked"),
        (302, "text/html", b"redirect"),
        (200, "text/html", b"<html>not json</html>"),
        (200, "application/json", b"not json"),
        (200, "application/json", b"[]"),
        (200, "application/json", b""),
    ],
)
def test_bad_responses_are_not_successes(
    store: IngestionStore,
    code: int,
    media: str,
    body: bytes,
) -> None:
    client = SecClient(
        settings(),
        store,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(code, content=body, headers={"Content-Type": media})
        ),
        sleep=lambda _: None,
    )
    try:
        with pytest.raises(SecError):
            client.json(SUBMISSIONS)
        resource = store.summary(600)["resources"][0]
        assert resource["status"] == "UNAVAILABLE"
        assert resource["data_version"] is None
    finally:
        client.close()


def test_size_cap(store: IngestionStore) -> None:
    client = SecClient(
        settings(sec_max_document_bytes=5),
        store,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"large": "body"})),
    )
    try:
        with pytest.raises(SecError, match="size limit"):
            client.json(SUBMISSIONS)
    finally:
        client.close()


def test_disabled_client(store: IngestionStore) -> None:
    with pytest.raises(SecError, match="disabled"):
        SecClient(Settings(_env_file=None), store)


def test_retry_after_formats() -> None:
    assert retry_after("3") == 3
    assert retry_after("invalid") == 0
    assert retry_after(None) == 0
    assert retry_after("Thu, 01 Jan 1970 00:00:00 GMT") == 0


def test_pipeline_end_to_end_and_idempotence(store: IngestionStore) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        if url == TICKERS_URL:
            return httpx.Response(200, json={"0": {"ticker": "TEST", "cik_str": 1}})
        if url == SUBMISSIONS:
            return httpx.Response(200, json={"cik": 1, "filings": {"recent": columns()}})
        if url == FACTS:
            return httpx.Response(200, json={"cik": 1, "facts": {}})
        return httpx.Response(200, content=HTML, headers={"Content-Type": "text/html"})

    client = SecClient(
        settings(), store, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    try:
        pipeline = SecPipeline(client, store)
        first = pipeline.run(["TEST"], limit=2)
        second = pipeline.run(["TEST"], limit=2)
        assert first["companies"][CIK]["parsed"] == 2
        assert second["companies"][CIK]["skipped"] == 2
        assert requests.count(SUBMISSIONS) == 2  # Always recheck the upstream source.
        assert sum("Archives" in url for url in requests) == 2
        state = store.filing_state(parse_filings(CIK, columns())[0])
        parsed = json.loads(store.read(state["parsed_hash"]))
        assert parsed["filing"]["accession_number"] == "0000000001-25-000002"
        assert parsed["source_url"].endswith("quarter.htm")
    finally:
        client.close()


def test_backfill_and_issuer_identity(store: IngestionStore) -> None:
    page = f"CIK{CIK}-submissions-001.json"
    payload = {"cik": 1, "filings": {"recent": columns(), "files": [{"name": page}]}}
    client = SecClient(
        settings(),
        store,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=payload if str(request.url) == SUBMISSIONS else columns()
            )
        ),
        sleep=lambda _: None,
    )
    try:
        assert len(SecPipeline(client, store).discover(CIK, backfill=True)) == 2
        payload["cik"] = 2
        with pytest.raises(SecError):
            SecPipeline(client, store).discover(CIK)
        assert store.summary(600)["resources"][1]["status"] == "UNAVAILABLE"
    finally:
        client.close()


def test_old_success_is_stale(store: IngestionStore) -> None:
    attempted = store.attempted(SUBMISSIONS)
    store.fetched(SUBMISSIONS, attempted, 0, 1, digest="abc")
    store.db.execute("UPDATE resources SET last_successful_fetch='2000-01-01T00:00:00+00:00'")
    assert store.summary(600)["resources"][0]["status"] == "STALE"


def test_download_resumes_after_parse_failure(store: IngestionStore) -> None:
    bodies = [b"not a filing", HTML]
    client = SecClient(
        settings(),
        store,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=bodies.pop(0), headers={"Content-Type": "text/html"}
            )
        ),
        sleep=lambda _: None,
    )
    filing = parse_filings(CIK, columns())[0]
    store.discover(filing)
    pipeline = SecPipeline(client, store)
    try:
        with pytest.raises(ValueError):
            pipeline.process(filing)
        assert store.filing_state(filing)["stage"] == "failed"
        assert pipeline.process(filing) == "parsed"
    finally:
        client.close()


def test_downloaded_stage_resumes_without_network(store: IngestionStore) -> None:
    filing = parse_filings(CIK, columns())[0]
    store.discover(filing)
    store.stage(filing, "downloaded", raw_hash=store.save(HTML))
    client = SecClient(
        settings(),
        store,
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Downloaded content must be reused")
        ),
    )
    try:
        assert SecPipeline(client, store).process(filing) == "parsed"
    finally:
        client.close()


def test_long_retry_after_persists_across_clients(store: IngestionStore) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    for _ in range(2):
        client = SecClient(settings(), store, transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(SecError, match="cooldown"):
                client.json(SUBMISSIONS)
        finally:
            client.close()
    assert len(calls) == 1


def test_companyfacts_validation_marks_unavailable(store: IngestionStore) -> None:
    client = SecClient(
        settings(),
        store,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"facts": {}})),
    )
    try:
        with pytest.raises(SecError):
            SecPipeline(client, store).companyfacts(CIK)
        assert store.summary(600)["resources"][0]["status"] == "UNAVAILABLE"
    finally:
        client.close()


def test_transport_retry_budget(store: IngestionStore) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("network failure")

    client = SecClient(
        settings(provider_max_retries=1),
        store,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    try:
        with pytest.raises(SecError, match="ConnectError"):
            client.json(SUBMISSIONS)
        assert len(calls) == 2
    finally:
        client.close()


def test_limited_runs_drain_backlog_and_filter_forms(store: IngestionStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == TICKERS_URL:
            return httpx.Response(200, json={"0": {"ticker": "TEST", "cik_str": 1}})
        if url == SUBMISSIONS:
            return httpx.Response(200, json={"cik": 1, "filings": {"recent": columns()}})
        if url == FACTS:
            return httpx.Response(200, json={"cik": 1, "facts": {}})
        return httpx.Response(200, content=HTML, headers={"Content-Type": "text/html"})

    client = SecClient(
        settings(), store, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    try:
        pipeline = SecPipeline(client, store)
        pipeline.run(["TEST"], limit=1, forms={"10-K"})
        filings = parse_filings(CIK, columns())
        assert store.filing_state(filings[1])["stage"] == "parsed"
        assert store.filing_state(filings[0])["stage"] == "discovered"
        pipeline.run(["TEST"], limit=1)
        assert store.filing_state(filings[0])["stage"] == "parsed"
        assert store.summary(600)["stages"] == {"parsed": 2}
    finally:
        client.close()


def test_nested_hidden_nodes() -> None:
    html = HTML.replace(
        b"<h2>", b'<div style="display:none"><p style="color:red">Hide</p></div><h2>'
    )
    assert "Hide" not in parse_document(html, "test")["text"]


def test_corrupt_completed_artifact_is_not_silently_skipped(store: IngestionStore) -> None:
    filing = parse_filings(CIK, columns())[0]
    store.discover(filing)
    digest = store.save(HTML)
    store.stage(filing, "parsed", raw_hash=digest, parsed_hash=digest, parser_version="sec-html-v1")
    (store.raw_directory / digest[:2] / digest).write_bytes(b"corruption")
    client = SecClient(
        settings(),
        store,
        transport=httpx.MockTransport(lambda request: pytest.fail("No network access expected")),
    )
    try:
        with pytest.raises(ValueError, match="checksum"):
            SecPipeline(client, store).process(filing)
        assert store.filing_state(filing)["stage"] == "failed"
    finally:
        client.close()


def test_source_versions_keep_immutable_provenance(store: IngestionStore) -> None:
    for content in (b'{"version":1}', b'{"version":2}', b'{"version":2}'):
        attempted = store.attempted(SUBMISSIONS)
        digest = store.save(content)
        store.fetched(SUBMISSIONS, attempted, 0, 1, digest=digest)
    versions = store.db.execute("SELECT url,content_hash FROM source_objects").fetchall()
    assert len(versions) == 2
    assert {json.loads(store.read(row["content_hash"]))["version"] for row in versions} == {1, 2}
