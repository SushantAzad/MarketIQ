from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_calculation_database import import_data

from app.api import create_app
from app.core.config import Settings
from app.database import schema as db

pytestmark = pytest.mark.integration


def test_company_financials_filings_and_unavailable_routes(pg):
    import_data(pg)
    with TestClient(create_app(Settings(_env_file=None), pg)) as client:
        assert client.get("/api/health").json()["database"] == "AVAILABLE"
        assert (
            client.get("/api/companies", params={"q": "test"}).json()["items"][0]["ticker"]
            == "TEST"
        )
        assert client.get("/api/companies/test").json()["cik"] == "0000000001"
        assert client.get("/api/companies/missing").status_code == 404
        assert client.get("/api/companies", params={"limit": 101}).status_code == 422
        result = client.get(
            "/api/companies/TEST/financials",
            params={
                "metric": "revenue",
                "basis": "annual",
                "period_end": "2024-12-31",
                "as_of": "2025-03-01T00:00:00Z",
            },
        )
        assert result.status_code == 200 and result.json()["value"] == "200"
        assert result.json()["source"]["content_hash"]
        assert client.get("/api/companies/TEST/filings").json()["items"]
        assert client.get("/api/data-freshness").json()["upstream_refreshed"] is False
        for route in ("risk", "quote", "history"):
            result = client.get(f"/api/companies/TEST/{route}")
            assert result.status_code == 503 and result.json()["status"] == "UNAVAILABLE"
        assert "/api/research" in client.get("/openapi.json").json()["paths"]


def test_research_persistence_resume_and_comparison_validation(pg):
    import_data(pg)
    with TestClient(create_app(Settings(_env_file=None), pg)) as client:
        request = {
            "question": "TEST revenue and net margin",
            "tickers": ["TEST"],
            "period": {"basis": "annual", "period_end": "2024-12-31"},
        }
        result = client.post("/api/research", json=request)
        assert result.status_code == 200
        job = result.json()
        assert job["status"] == "COMPLETED"
        assert job["result"]["retrieved_facts"][0]["result"]["value"] == "200"
        assert client.get(f"/api/research/{job['job_id']}").json() == job
        assert client.post(f"/api/research/{job['job_id']}/run").json() == job
        assert client.get(f"/api/research/{uuid4()}").status_code == 404
        assert client.post("/api/compare", json=request).status_code == 422
        assert (
            client.post("/api/research", json={"question": "TEST revenue"}).json()["status"]
            == "NEEDS_CLARIFICATION"
        )
        with pg.begin() as connection:
            connection.execute(db.workflow_jobs.update().values(expires_at=sa.func.now()))
        assert client.get(f"/api/research/{job['job_id']}").status_code == 410


def test_authentication_rate_limit_cors_and_safe_validation(pg):
    settings = Settings(_env_file=None, api_key="test-secret", api_requests_per_minute=2)
    with TestClient(create_app(settings, pg)) as client:
        assert client.get("/api/health").status_code == 401
        assert (
            client.get("/api/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
        )
        headers = {"Authorization": "Bearer test-secret", "Origin": "http://127.0.0.1:5173"}
        response = client.get("/api/health", headers=headers)
        assert response.headers["access-control-allow-origin"] == headers["Origin"]
        response = client.post(
            "/api/research", headers=headers, json={"question": "x", "secret": "do-not-echo"}
        )
        assert response.status_code == 422 and "do-not-echo" not in response.text
        assert client.get("/api/health", headers=headers).status_code == 429
        response = client.options(
            "/api/research",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 400
    with pytest.raises(ValueError, match="API_KEY"):
        create_app(Settings(_env_file=None, app_env="production"), pg)


def test_database_failure_is_sanitized(pg, monkeypatch):
    with TestClient(create_app(Settings(_env_file=None), pg)) as client:

        def broken():
            raise sa.exc.OperationalError("credential-secret", {}, Exception("password"))

        monkeypatch.setattr(pg, "connect", broken)
        response = client.get("/api/health")
        assert response.status_code == 503
        assert "password" not in response.text and "credential-secret" not in response.text
