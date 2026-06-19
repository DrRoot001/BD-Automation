from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.database import get_db
from app.models.job import Job
from app.schemas.job import JobCreate, JobResponse

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

@router.post("", response_model=List[JobResponse], status_code=status.HTTP_201_CREATED)
async def create_jobs(
    jobs: List[JobCreate],
    db: AsyncSession = Depends(get_db)
):
    created = []
    for job_data in jobs:
        job_dict = job_data.model_dump(exclude_unset=True)
        # Remove embedding if None to avoid pgvector error
        if job_dict.get('embedding') is None:
            job_dict.pop('embedding', None)
        
        job = Job(**job_dict)
        db.add(job)
        created.append(job)
    
    await db.commit()
    
    for job in created:
        await db.refresh(job)
    
    return created

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