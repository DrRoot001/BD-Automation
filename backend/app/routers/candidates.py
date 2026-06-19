import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.candidate import Candidate
from app.models.resume import Resume
from app.schemas.candidate import CandidateCreate, CandidateResponse
from app.schemas.resume import ResumeResponse

router = APIRouter(prefix="/api/candidates", tags=["candidates"])

@router.post("", response_model=CandidateResponse, status_code=201)
async def create_candidate(candidate: CandidateCreate, db: AsyncSession = Depends(get_db)):
    db_candidate = Candidate(**candidate.model_dump())
    db.add(db_candidate)
    await db.commit()
    await db.refresh(db_candidate)
    return db_candidate

@router.get("/{candidate_id}", response_model=CandidateResponse)
async def get_candidate(candidate_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Candidate).where(Candidate.id == candidate_id))
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate

@router.post("/{candidate_id}/resumes", response_model=ResumeResponse, status_code=201)
async def upload_candidate_resume(
    candidate_id: str,
    file: UploadFile = File(...),
    is_base: str = Form("true"),
    db: AsyncSession = Depends(get_db)
):
    # Verify candidate exists
    result = await db.execute(select(Candidate).where(Candidate.id == candidate_id))
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # Define storage path
    local_dir = "/Users/sabihhaider/Documents/BD-Automator-Agent/backend/data/resumes"
    os.makedirs(local_dir, exist_ok=True)
    
    # Generate unique filename to avoid duplicates/collisions
    file_ext = os.path.splitext(file.filename)[1] if file.filename else ".pdf"
    new_filename = f"{candidate_id}_{uuid.uuid4()}{file_ext}"
    full_path = os.path.join(local_dir, new_filename)

    # Save file content locally
    try:
        with open(full_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")

    # Parse boolean
    is_base_bool = is_base.lower() == "true"

    # Find the maximum version for this candidate's resumes to set next version
    version_result = await db.execute(
        select(Resume.version)
        .where(Resume.candidate_id == candidate_id)
        .order_by(Resume.version.desc())
        .limit(1)
    )
    max_version = version_result.scalar_one_or_none()
    next_version = (max_version or 0) + 1

    # Create Resume DB record
    db_resume = Resume(
        candidate_id=candidate_id,
        version=next_version,
        file_url=full_path,
        is_base=is_base_bool,
        parsed_json=None
    )
    db.add(db_resume)
    await db.commit()
    await db.refresh(db_resume)
    return db_resume