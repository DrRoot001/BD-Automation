import json
from pathlib import Path

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.database import get_db
from app.models.job import Job
from app.schemas.job import JobCreate, JobResponse

DATA_LOG_PATH = Path(__file__).resolve().parents[1] / "jobs_data.json"
router = APIRouter(prefix="/api/jobs", tags=["jobs"])

@router.post("", status_code=status.HTTP_201_CREATED, response_model=List[JobResponse])
async def create_jobs(
    jobs: List[JobCreate],
    db: AsyncSession = Depends(get_db)
):
    created = []
    source_urls = [job.source_url for job in jobs if job.source_url]
    existing_urls = set()

    if source_urls:
        result = await db.execute(select(Job.source_url).where(Job.source_url.in_(source_urls)))
        existing_urls = {row[0] for row in result.fetchall()}

    for job_data in jobs:
        job_dict = job_data.model_dump(exclude_unset=True)
        if job_dict.get('embedding') is None:
            job_dict.pop('embedding', None)

        if job_dict.get('source_url') in existing_urls:
            continue

        job = Job(**job_dict)
        db.add(job)
        created.append(job)

    if not created:
        return []

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return []

    for job in created:
        await db.refresh(job)

    await _append_jobs_to_json_log(created)

    return created


async def _append_jobs_to_json_log(created_jobs: List[Job]) -> None:
    try:
        existing_data = []
        if DATA_LOG_PATH.exists():
            with DATA_LOG_PATH.open("r", encoding="utf-8") as file:
                existing_data = json.load(file)
        new_entries = [
            {
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "source": job.source,
                "source_url": job.source_url,
                "canonical_url": job.canonical_url,
                "description": job.description,
                "skills": job.skills,
                "salary_min": job.salary_min,
                "salary_max": job.salary_max,
                "pay_period": job.pay_period,
                "job_type": job.job_type,
                "posted_at": job.posted_at.isoformat() if job.posted_at else None,
                "embedding": job.embedding,
                "id": str(job.id),
                "created_at": job.created_at.isoformat() if job.created_at else None,
            }
            for job in created_jobs
        ]
        existing_data.extend(new_entries)
        DATA_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DATA_LOG_PATH.open("w", encoding="utf-8") as file:
            json.dump(existing_data, file, indent=2, ensure_ascii=False)
    except Exception as exc:
        # Keep ingestion working even if JSON logging fails.
        print(f"Warning: failed to append jobs to JSON log: {exc}")


@router.get("", response_model=List[JobResponse])
async def get_jobs(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import select
    result = await db.execute(select(Job).offset(skip).limit(limit))
    return result.scalars().all()

@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import select
    from fastapi import HTTPException
    from uuid import UUID
    try:
        job_uuid = UUID(job_id) if isinstance(job_id, str) else job_id
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job UUID")
        
    result = await db.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job