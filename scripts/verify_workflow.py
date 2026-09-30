"""Verify fresh-process recovery and real filing citation validation on local services."""

import json
import subprocess
import sys

from app.core.config import REPOSITORY_ROOT, Settings
from app.database.connection import database_engine
from app.routing.models import Period, QueryRequest
from app.workflow.service import create_job, read_job, run_job


def main() -> None:
    settings = Settings()
    engine = database_engine(settings)
    period = Period(basis="annual", period_end="2026-01-25")
    try:
        job_id = create_job(
            engine, settings, QueryRequest(question="NVDA revenue and net margin", period=period)
        )
        stopped = run_job(engine, settings, job_id, stop_after="execute")
        assert stopped["state"]["next_node"] == "validate"
        child = subprocess.run(
            [sys.executable, "-m", "app.workflow", "run", str(job_id)],
            cwd=REPOSITORY_ROOT / "backend",
            capture_output=True,
            text=True,
            check=True,
        )
        recovered = json.loads(child.stdout)
        assert recovered["status"] == "COMPLETED"
        assert recovered["state"]["attempts"] == {
            "plan": 1,
            "execute": 1,
            "validate": 1,
            "finalize": 1,
        }
        assert (
            recovered["state"]["response"]["retrieved_facts"]
            == stopped["state"]["response"]["retrieved_facts"]
        )
        evidence_id = create_job(
            engine,
            settings,
            QueryRequest(
                question="Explain NVDA net margin",
                period=period,
                form="10-K",
                accession="0001045810-26-000021",
            ),
        )
        evidence = run_job(engine, settings, evidence_id)
        assert evidence["status"] == "COMPLETED", evidence.get("last_error")
        response = evidence["state"]["response"]
        assert response["status"] == "AVAILABLE", response["unavailable"]
        assert response["citation_validation"] == "passed" and response["document_evidence"]
        assert read_job(engine, evidence_id) == evidence
        report = {
            "restart_job": str(job_id),
            "evidence_job": str(evidence_id),
            "fresh_process_resume": True,
            "completed_nodes_replayed": False,
            "canonical_citations_validated": True,
            "upstream_refreshed": False,
            "response": response,
        }
        path = settings.raw_storage_path.parent / "workflow-verification.json"
        path.write_text(json.dumps(report, default=str, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "response"}, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
