import uuid
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
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
    try:
        resume_uuid = uuid.UUID(resume_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid resume UUID")
    result = await db.execute(select(Resume).where(Resume.id == resume_uuid))
    db_resume = result.scalar_one_or_none()
    if not db_resume:
        raise HTTPException(status_code=404, detail="Resume not found")
    for field, value in update.model_dump(exclude_none=True).items():
        setattr(db_resume, field, value)
    await db.commit()
    await db.refresh(db_resume)
    return db_resume


@router.get("/{resume_id}/view")
async def view_resume(resume_id: str, db: AsyncSession = Depends(get_db)):
    """Return a signed URL (or redirect) for viewing a resume PDF."""
    try:
        resume_uuid = uuid.UUID(resume_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid resume UUID")
    result = await db.execute(select(Resume).where(Resume.id == resume_uuid))
    db_resume = result.scalar_one_or_none()
    if not db_resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    file_url: str = db_resume.file_url or ""

    if not file_url:
        raise HTTPException(status_code=404, detail="No file URL stored for this resume. Please re-upload via the UI.")

    # For private Supabase buckets (/object/public/ URLs need signing)
    if "supabase.co/storage" in file_url and "/object/public/" in file_url:
        signed = await _create_supabase_signed_url(file_url)
        return RedirectResponse(url=signed, status_code=302)

    # Already a signed or direct https URL
    if file_url.startswith("http://") or file_url.startswith("https://"):
        return RedirectResponse(url=file_url, status_code=302)

    # Legacy local path
    if file_url.startswith("/files/"):
        from fastapi.responses import FileResponse
        import os
        from pathlib import Path
        
        # Determine the root directory of the backend
        backend_dir = Path(__file__).resolve().parent.parent.parent
        local_path = backend_dir / file_url.lstrip("/")
        
        if os.path.exists(local_path):
            return FileResponse(path=local_path, media_type="application/pdf", filename=f"resume_{resume_id}.pdf")
        
    raise HTTPException(
        status_code=404,
        detail="This resume was saved locally and is no longer accessible. Please re-upload via the UI."
    )


async def _create_supabase_signed_url(public_url: str, expires_in: int = 3600) -> str:
    """Exchange a Supabase public object URL for a signed URL (works for private buckets)."""
    import httpx
    from module3.utils.storage import SUPABASE_URL, SUPABASE_KEY

    if not SUPABASE_URL or not SUPABASE_KEY:
        return public_url

    # Extract bucket and object path from the public URL
    # Format: .../storage/v1/object/public/{bucket}/{path}
    try:
        after = public_url.split("/storage/v1/object/public/", 1)[1]
        bucket, *parts = after.split("/")
        object_path = "/".join(parts)
    except (IndexError, ValueError):
        return public_url

    sign_url = f"{SUPABASE_URL}/storage/v1/object/sign/{bucket}/{object_path}"
    headers = {"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(sign_url, json={"expiresIn": expires_in}, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                signed_path = data.get("signedURL") or data.get("signedUrl") or ""
                if signed_path:
                    base = SUPABASE_URL.rstrip("/")
                    return signed_path if signed_path.startswith("http") else f"{base}{signed_path}"
    except Exception:
        pass
    return public_url


@router.get("/{candidate_id}")
async def get_resumes(candidate_id: str, is_base: bool = None, db: AsyncSession = Depends(get_db)):
    try:
        candidate_uuid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")
    query = select(Resume).where(Resume.candidate_id == candidate_uuid)
    if is_base is not None:
        query = query.where(Resume.is_base == is_base)
    result = await db.execute(query)
    return result.scalars().all()