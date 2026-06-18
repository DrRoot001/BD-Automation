import sys
from pathlib import Path
import asyncio

# ensure backend package is importable
HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parent
sys.path.insert(0, str(BACKEND_DIR))

from module2.adapters import get_adapter
from module2.normalization import normalize_batch
from module2.normalization.schemas import NormalizedJob

from app.routers import module2_routes
from module2.storage.job_store import JobStore
from module2.storage.event_publisher import EventPublisher


def test_end_to_end_pipeline(tmp_path):
    # prepare storage and event publisher
    db_file = tmp_path / "e2e_jobs.db"
    js = JobStore(db_path=db_file)
    ep = EventPublisher()

    received = []

    def on_job_inserted(payload):
        received.append(payload)

    ep.subscribe("job_inserted", on_job_inserted)

    module2_routes.job_store = js
    module2_routes.event_publisher = ep

    async def run_pipeline():
        adapter = get_adapter("mock")()
        raw_jobs = await adapter.discover_jobs({})
        assert len(raw_jobs) > 0

        normalized: list[NormalizedJob] = normalize_batch(raw_jobs)
        assert len(normalized) == len(raw_jobs)

        # insert via router
        for i, nj in enumerate(normalized):
            payload = {
                "id": f"e2e-{i}-{abs(hash(nj.url)) % 100000}",
                "title": nj.title,
                "company": nj.company,
                "location": nj.location,
                "url": nj.url,
                "canonical_url": nj.canonical_url,
                "posted_at": nj.posted_at.isoformat(),
                "salary_min": nj.salary_min,
                "salary_max": nj.salary_max,
                "pay_period": nj.pay_period,
                "skills": nj.skills,
                "source": nj.source,
            }

            resp = module2_routes.create_job(module2_routes.JobPayload(**payload))
            assert resp.get("status") == "ok"

        # verify storage
        rows = js.list_jobs()
        assert len(rows) >= len(normalized)

        # verify events
        assert len(received) >= len(normalized)

    asyncio.run(run_pipeline())
