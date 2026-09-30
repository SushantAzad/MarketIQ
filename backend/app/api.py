"""HTTP adapters for stored financial data and durable research workflows."""

import secrets
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime

from app.core.config import Settings
from app.database import schema as db
from app.database.connection import database_engine
from app.routing.models import Basis, Form, QueryRequest
from app.services.financial_query import QUERY_CONCEPTS, financial_value
from app.workflow.service import create_job, read_job, run_job


def create_app(settings: Settings | None = None, engine: sa.Engine | None = None) -> FastAPI:
    settings = settings or Settings()
    key = settings.api_key.get_secret_value() if settings.api_key else None
    if (
        settings.app_env == "production"
        or settings.api_host not in {"127.0.0.1", "localhost", "::1"}
    ) and not key:
        raise ValueError("API_KEY is required for production or non-loopback API binding")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = engine or database_engine(settings)
        try:
            yield
        finally:
            if engine is None:
                app.state.engine.dispose()

    calls: deque[float] = deque()
    lock = threading.Lock()
    bearer = HTTPBearer(auto_error=False)

    def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if key and (
            credentials is None or not secrets.compare_digest(credentials.credentials, key)
        ):
            raise HTTPException(401, "Invalid API key", headers={"WWW-Authenticate": "Bearer"})
        with lock:
            now = time.monotonic()
            while calls and calls[0] <= now - 60:
                calls.popleft()
            if len(calls) >= settings.api_requests_per_minute:
                raise HTTPException(429, "API request limit reached", headers={"Retry-After": "60"})
            calls.append(now)

    app = FastAPI(
        title="MarketIQ", version="0.1.0", lifespan=lifespan, dependencies=[Depends(authorize)]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "status": "INVALID_REQUEST",
                "errors": [{"location": list(e["loc"]), "type": e["type"]} for e in exc.errors()],
            },
        )

    @app.exception_handler(sa.exc.SQLAlchemyError)
    async def database_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=503, content={"status": "UNAVAILABLE", "reason": "database_unavailable"}
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500, content={"status": "UNAVAILABLE", "reason": "internal_error"}
        )

    def connection_engine() -> sa.Engine:
        return app.state.engine  # type: ignore[no-any-return]

    def issuer(ticker: str) -> dict[str, Any]:
        with connection_engine().connect() as connection:
            row = (
                connection.execute(
                    sa.select(
                        db.companies.c.id,
                        db.companies.c.cik,
                        db.companies.c.legal_name,
                        db.securities.c.ticker,
                    )
                    .join(db.securities, db.securities.c.company_id == db.companies.c.id)
                    .where(db.securities.c.ticker == ticker.upper())
                )
                .mappings()
                .first()
            )
        if row is None:
            raise HTTPException(404, "Company not found")
        return dict(row)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        with connection_engine().connect() as connection:
            connection.execute(sa.text("SELECT 1"))
        return {
            "status": "AVAILABLE",
            "database": "AVAILABLE",
            "risk_inference": "UNAVAILABLE",
            "market_data": "UNAVAILABLE",
            "upstream_connectivity_tested": False,
        }

    @app.get("/api/companies")
    def companies(
        q: Annotated[str, Query(max_length=100)] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    ) -> dict[str, Any]:
        query = (
            sa.select(db.companies.c.cik, db.companies.c.legal_name, db.securities.c.ticker)
            .join(db.securities, db.securities.c.company_id == db.companies.c.id)
            .where(
                sa.or_(
                    db.companies.c.legal_name.icontains(q, autoescape=True),
                    db.securities.c.ticker.icontains(q, autoescape=True),
                )
            )
            .order_by(db.securities.c.ticker)
            .limit(limit)
            .offset(offset)
        )
        with connection_engine().connect() as connection:
            rows = [dict(r) for r in connection.execute(query).mappings()]
        return {"items": rows, "limit": limit, "offset": offset}

    @app.get("/api/companies/{ticker}")
    def company(ticker: str) -> dict[str, Any]:
        return issuer(ticker)

    @app.get("/api/companies/{ticker}/financials")
    def financials(
        ticker: str,
        metric: str,
        basis: Basis,
        period_end: date | None = None,
        as_of: AwareDatetime | None = None,
        unit: Annotated[str, Query(pattern="^[A-Z]{3}$")] = "USD",
    ) -> dict[str, Any]:
        issuer(ticker)
        if metric not in QUERY_CONCEPTS:
            raise HTTPException(422, "Unknown financial metric")
        with (
            connection_engine()
            .connect()
            .execution_options(isolation_level="REPEATABLE READ") as connection
        ):
            return financial_value(
                connection,
                ticker,
                metric,
                basis=basis,
                period_end=period_end,
                as_of=as_of,
                unit=unit,
            )

    @app.get("/api/companies/{ticker}/filings")
    def filings(
        ticker: str,
        form: Form | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    ) -> dict[str, Any]:
        company_id = issuer(ticker)["id"]
        query = sa.select(
            db.filings.c.accession_number,
            db.filings.c.form_type,
            db.filings.c.filing_date,
            db.filings.c.report_period,
            db.filings.c.source_url,
        ).where(db.filings.c.company_id == company_id)
        if form:
            query = query.where(db.filings.c.form_type == form)
        query = (
            query.order_by(db.filings.c.filing_date.desc(), db.filings.c.accession_number)
            .limit(limit)
            .offset(offset)
        )
        with connection_engine().connect() as connection:
            rows = [dict(r) for r in connection.execute(query).mappings()]
        return {"items": rows, "limit": limit, "offset": offset, "upstream_refreshed": False}

    @app.get("/api/companies/{ticker}/risk", status_code=503)
    def risk(ticker: str) -> dict[str, Any]:
        issuer(ticker)
        return {
            "status": "UNAVAILABLE",
            "reason": "approved_risk_model_unavailable",
            "probability": None,
            "explanation": None,
        }

    @app.get("/api/companies/{ticker}/quote", status_code=503)
    @app.get("/api/companies/{ticker}/history", status_code=503)
    def market(ticker: str) -> dict[str, Any]:
        issuer(ticker)
        return {"status": "UNAVAILABLE", "reason": "market_service_not_implemented"}

    @app.get("/api/data-freshness")
    def freshness() -> dict[str, Any]:
        now = datetime.now(UTC)
        with connection_engine().connect() as connection:
            latest = connection.scalar(
                sa.select(sa.func.max(db.snapshot_observations.c.observed_at))
            )
        return {
            "as_of": now,
            "last_snapshot_observed_at": latest,
            "status": "UNAVAILABLE"
            if latest is None
            else ("RECENT" if (now - latest).total_seconds() <= 600 else "STALE"),
            "scope": "latest_stored_snapshot_only",
            "upstream_refreshed": False,
        }

    def job_response(job: dict[str, Any] | None) -> dict[str, Any]:
        if job is None or job["status"] == "NOT_FOUND":
            raise HTTPException(404, "Research job not found")
        if job["status"] == "EXPIRED":
            raise HTTPException(410, "Research job expired")
        if job["status"] == "BUSY":
            raise HTTPException(409, "Research job is running")
        return {
            "job_id": job["id"],
            "status": job["status"],
            "result": job.get("state", {}).get("response") or None,
            "clarifications": job.get("state", {}).get("query_plan", {}).get("clarifications", []),
            "error": job.get("last_error"),
            "expires_at": job.get("expires_at"),
        }

    @app.post("/api/research")
    def research(request: QueryRequest) -> dict[str, Any]:
        job_id = create_job(connection_engine(), settings, request)
        return job_response(run_job(connection_engine(), settings, job_id))

    @app.post("/api/compare")
    def compare(request: QueryRequest) -> dict[str, Any]:
        if len(request.tickers) < 2:
            raise HTTPException(422, "Comparison requires at least two distinct tickers")
        return research(request)

    @app.get("/api/research/{job_id}")
    def research_job(job_id: UUID) -> dict[str, Any]:
        return job_response(read_job(connection_engine(), job_id))

    @app.post("/api/research/{job_id}/run")
    def resume_job(job_id: UUID) -> dict[str, Any]:
        return job_response(run_job(connection_engine(), settings, job_id))

    return app


if __name__ == "__main__":
    import uvicorn

    configuration = Settings()
    uvicorn.run(create_app(configuration), host=configuration.api_host, port=configuration.api_port)
