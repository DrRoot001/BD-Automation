from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from module2.storage.job_store import JobStore
from module2.storage.event_publisher import EventPublisher

router = APIRouter()

job_store = JobStore()
event_publisher = EventPublisher()


class JobPayload(BaseModel):
    id: str
    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    url: Optional[str] = None
    canonical_url: Optional[str] = None
    posted_at: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    pay_period: Optional[str] = None
    skills: Optional[List[str]] = None
    source: Optional[str] = None


@router.post("/api/module2/jobs", status_code=201)
def create_job(payload: JobPayload):
    # convert to a simple object expected by JobStore
    class _J:
        pass

    j = _J()
    j.job_id = payload.id
    j.title = payload.title
    j.company = payload.company
    j.location = payload.location
    j.url = payload.url
    j.canonical_url = payload.canonical_url
    j.posted_at = None
    j.salary_min = payload.salary_min
    j.salary_max = payload.salary_max
    j.pay_period = payload.pay_period
    j.skills = payload.skills
    j.source = payload.source

    try:
        job_store.insert_job(j)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # publish event for downstream consumers
    event_publisher.publish("job_inserted", {"id": payload.id})

    return {"status": "ok", "id": payload.id}


@router.get("/api/module2/jobs/{job_id}")
def get_job(job_id: str):
    res = job_store.get_job(job_id)
    if not res:
        raise HTTPException(status_code=404, detail="not found")
    return res


@router.get("/api/module2/jobs")
def list_jobs(limit: int = 100):
    return job_store.list_jobs(limit=limit)
