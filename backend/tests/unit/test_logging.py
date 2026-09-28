import json
import logging

from app.core.logging import JsonFormatter


def test_json_logging_keeps_only_safe_extras() -> None:
    record = logging.LogRecord("sec", logging.INFO, "", 0, "sec_fetch", (), None)
    record.url = "https://www.sec.gov/files/company_tickers.json"
    record.secret = "must-not-appear"
    output = JsonFormatter().format(record)
    assert json.loads(output)["event"] == "sec_fetch"
    assert "must-not-appear" not in output
