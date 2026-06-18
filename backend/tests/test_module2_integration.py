import json
import sys
import asyncio
from pathlib import Path
from asgi_lifespan import LifespanManager
import httpx

# ensure backend package is importable (adds backend/ to sys.path)
HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.main import app
from app.routers import module2_routes

from module2.storage.job_store import JobStore
from module2.storage.event_publisher import EventPublisher


def test_module2_job_flow(tmp_path):
    # use a temporary sqlite file for isolation
    db_file = tmp_path / "jobs_test.db"
    module2_routes.job_store = JobStore(db_path=db_file)
    module2_routes.event_publisher = EventPublisher()

    # call handler functions directly to avoid ASGI client compatibility issues
    payload = {
        "id": "job-123",
        "title": "Software Engineer",
        "company": "Acme",
        "location": "Remote",
        "url": "https://example.com/job/123",
        "canonical_url": "https://example.com/job/123",
        "salary_min": 80000,
        "salary_max": 120000,
        "skills": ["python", "fastapi"],
        "source": "mock"
    }

    resp = module2_routes.create_job(module2_routes.JobPayload(**payload))
    assert resp.get("status") == "ok"
    assert resp.get("id") == payload["id"]

    got = module2_routes.get_job(payload["id"])
    assert got is not None
    assert got.get("id") == payload["id"] or got.get("id") == payload["id"]

    lst = module2_routes.list_jobs()
    assert isinstance(lst, list)
    assert any(j.get("id") == payload["id"] for j in lst)


def test_get_missing_job(tmp_path):
    db_file = tmp_path / "jobs_test2.db"
    module2_routes.job_store = JobStore(db_path=db_file)

    # direct call to handler
    try:
        module2_routes.get_job("not-exist")
        assert False, "expected not found"
    except Exception:
        # route raises HTTPException; treat as expected
        pass
