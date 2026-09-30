import json
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from test_calculation_database import import_data

from app.core.config import Settings
from app.database import schema as db
from app.routing.models import Period, QueryRequest
from app.workflow import __main__ as cli
from app.workflow import service

pytestmark = pytest.mark.integration


def request(question="TEST revenue and net margin", **updates):
    return QueryRequest(
        question=question, period=Period(basis="annual", period_end="2024-12-31"), **updates
    )


def test_resume_completed_boundaries_and_terminal_replay(pg, monkeypatch):
    import_data(pg)
    settings = Settings(_env_file=None)
    job_id = service.create_job(pg, settings, request())
    first = service.run_job(pg, settings, job_id, stop_after="execute")
    assert first["status"] == "RUNNING" and first["state"]["next_node"] == "validate"
    frozen = first["state"]["response"]["structured_as_of"]
    monkeypatch.setattr(
        service, "execute_query", lambda *_a, **_k: pytest.fail("must not replay executed stage")
    )
    pg.dispose()  # Drop pooled connections; resume from PostgreSQL, not Python graph memory.
    resumed = service.run_job(pg, settings, job_id)
    assert resumed["status"] == "COMPLETED"
    assert resumed["state"]["response"]["status"] == "AVAILABLE"
    assert resumed["state"]["response"]["structured_as_of"] == frozen
    assert service.run_job(pg, settings, job_id) == resumed


def test_failed_stage_is_sanitized_and_retries_are_bounded(pg, monkeypatch):
    import_data(pg)
    settings = Settings(_env_file=None)
    job_id = service.create_job(pg, settings, request())
    calls = []

    def fail(*_a, **_k):
        calls.append(1)
        raise RuntimeError("private credentials must not be persisted")

    monkeypatch.setattr(service, "execute_query", fail)
    for _ in range(4):
        result = service.run_job(pg, settings, job_id)
        assert result["status"] == "FAILED"
    assert len(calls) == 3 and result["last_error"] == "attempt_limit_reached"
    assert "private credentials" not in json.dumps(result, default=str)
    assert result["state"]["attempts"]["plan"] == 1


def test_failed_stage_can_recover(pg, monkeypatch):
    import_data(pg)
    settings = Settings(_env_file=None)
    job_id = service.create_job(pg, settings, request())
    execute = service.execute_query

    def fail(*_a, **_k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(service, "execute_query", fail)
    assert service.run_job(pg, settings, job_id)["status"] == "FAILED"
    monkeypatch.setattr(service, "execute_query", execute)
    result = service.run_job(pg, settings, job_id)
    assert result["status"] == "COMPLETED" and result["last_error"] is None
    assert result["state"]["attempts"]["execute"] == 2


def test_process_exit_after_calculation_commit_before_checkpoint(pg, monkeypatch):
    import_data(pg)
    settings = Settings(_env_file=None)
    job_id = service.create_job(pg, settings, request())
    execute = service.execute_query

    def crash(*args, **kwargs):
        execute(*args, **kwargs)
        raise SystemExit("simulated process exit before checkpoint")

    monkeypatch.setattr(service, "execute_query", crash)
    with pytest.raises(SystemExit):
        service.run_job(pg, settings, job_id)
    assert service.read_job(pg, job_id)["state"]["next_node"] == "execute"
    monkeypatch.setattr(service, "execute_query", execute)
    assert service.run_job(pg, settings, job_id)["status"] == "COMPLETED"
    with pg.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(db.financial_metrics)) == 1


def test_clarification_and_insufficient_evidence_paths(pg):
    import_data(pg)
    settings = Settings(_env_file=None)
    job_id = service.create_job(pg, settings, QueryRequest(question="TEST revenue in 2024"))
    result = service.run_job(pg, settings, job_id)
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert "execute" not in result["state"]["attempts"]
    job_id = service.create_job(pg, settings, request("TEST EBITDA"))
    result = service.run_job(pg, settings, job_id)
    assert result["status"] == "COMPLETED"
    assert result["state"]["response"]["status"] == "INSUFFICIENT_EVIDENCE"


def test_lock_expiry_version_and_unknown_job(pg):
    settings = Settings(_env_file=None)
    job_id = service.create_job(pg, settings, request())
    key = int.from_bytes(job_id.bytes[:8], "big", signed=True)
    with pg.connect() as connection:
        connection.execute(sa.text("SELECT pg_advisory_lock(:key)"), {"key": key})
        connection.commit()
        assert service.run_job(pg, settings, job_id)["status"] == "BUSY"
        connection.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        connection.commit()
    with pg.begin() as connection:
        connection.execute(db.workflow_jobs.update().values(version="future-version"))
    assert service.run_job(pg, settings, job_id)["status"] == "INCOMPATIBLE_VERSION"
    with pg.begin() as connection:
        connection.execute(
            db.workflow_jobs.update().values(expires_at=datetime.now(UTC) - timedelta(days=1))
        )
    assert service.run_job(pg, settings, job_id)["status"] == "EXPIRED"
    assert service.purge_expired(pg) == 1
    assert service.read_job(pg, job_id) is None
    assert service.run_job(pg, settings, uuid4())["status"] == "NOT_FOUND"


def test_invalid_document_evidence_is_removed_without_losing_facts(pg):
    import_data(pg)
    settings = Settings(_env_file=None)
    with pg.begin() as connection:
        connection.execute(db.filings.update().values(report_period="2024-12-31"))
    job_id = service.create_job(
        pg,
        settings,
        request("Explain TEST net margin", form="10-K", accession="0000000001-25-000001"),
    )

    def fabricated(*_):
        return {"retrieved_evidence": [{"chunk_id": "invalid"}], "generation": "invalid"}

    result = service.run_job(pg, settings, job_id, documents=fabricated)
    response = result["state"]["response"]
    assert result["status"] == "COMPLETED" and response["status"] == "PARTIAL"
    assert response["calculated_values"] and not response["document_evidence"]
    assert response["unavailable"][0]["reason"] == "citation_validation_failed"


def test_workflow_cli_submit_show_run_and_purge(pg, monkeypatch, capsys):
    import_data(pg)
    monkeypatch.setattr(cli, "database_engine", lambda *_: pg)
    monkeypatch.setattr(
        sys,
        "argv",
        ["workflow", "submit", "TEST revenue", "--basis", "annual", "--period-end", "2024-12-31"],
    )
    assert cli.main() == 0
    job_id = json.loads(capsys.readouterr().out)["job_id"]
    for command, status in [("show", "QUEUED"), ("run", "COMPLETED")]:
        monkeypatch.setattr(sys, "argv", ["workflow", command, job_id])
        assert cli.main() == 0
        assert json.loads(capsys.readouterr().out)["status"] == status
    monkeypatch.setattr(sys, "argv", ["workflow", "submit", "TEST revenue", "--basis", "annual"])
    assert cli.main() == 1
    capsys.readouterr()
    with pg.begin() as connection:
        connection.execute(
            db.workflow_jobs.update().values(expires_at=datetime.now(UTC) - timedelta(days=1))
        )
    monkeypatch.setattr(sys, "argv", ["workflow", "purge-expired"])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["deleted_expired_jobs"] == 1
