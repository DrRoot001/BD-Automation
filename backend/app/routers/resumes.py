from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.resume import Resume
from app.schemas.resume import ResumeCreate, ResumeResponse

router = APIRouter(prefix="/api/resumes", tags=["resumes"])


class ResumeUpdate(BaseModel):
    parsed_json: Optional[Dict[str, Any]] = None
    file_url: Optional[str] = None


@router.post("", response_model=ResumeResponse, status_code=201)
async def create_resume(resume: ResumeCreate, db: AsyncSession = Depends(get_db)):
    db_resume = Resume(**resume.model_dump())
    db.add(db_resume)
    await db.commit()
    await db.refresh(db_resume)
    return db_resume


@router.patch("/{resume_id}", response_model=ResumeResponse)
async def update_resume(resume_id: str, update: ResumeUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Resume).where(Resume.id == resume_id))
    db_resume = result.scalar_one_or_none()
    if not db_resume:
        raise HTTPException(status_code=404, detail="Resume not found")
    for field, value in update.model_dump(exclude_none=True).items():
        setattr(db_resume, field, value)
    await db.commit()
    await db.refresh(db_resume)
    return db_resume


@router.get("/{candidate_id}")
async def get_resumes(candidate_id: str, is_base: bool = None, db: AsyncSession = Depends(get_db)):
    query = select(Resume).where(Resume.candidate_id == candidate_id)
    if is_base is not None:
        query = query.where(Resume.is_base == is_base)
    result = await db.execute(query)
    return result.scalars().all()