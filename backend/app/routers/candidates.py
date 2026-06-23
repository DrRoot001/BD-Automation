import uuid
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List
from pydantic import BaseModel

from app.database import get_db
from app.models.candidate import Candidate
from app.models.resume import Resume
from app.schemas.candidate import CandidateCreate, CandidateResponse, CandidateUpdate
from app.schemas.resume import ResumeResponse

router = APIRouter(prefix="/api/candidates", tags=["candidates"])

@router.get("", response_model=List[CandidateResponse])
async def list_candidates(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Candidate))
    return result.scalars().all()

@router.post("", response_model=CandidateResponse, status_code=201)
async def create_candidate(candidate: CandidateCreate, db: AsyncSession = Depends(get_db)):
    # Check if candidate exists by email or name
    from sqlalchemy import or_
    conditions = []
    if candidate.email:
        conditions.append(Candidate.email == candidate.email)
    if candidate.name:
        conditions.append(Candidate.name == candidate.name)
        
    if conditions:
        result = await db.execute(select(Candidate).where(or_(*conditions)))
        existing_candidate = result.scalars().first()
        if existing_candidate:
            return existing_candidate

    db_candidate = Candidate(**candidate.model_dump())
    db.add(db_candidate)
    try:
        await db.commit()
        await db.refresh(db_candidate)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Database error: {str(e)}")
    return db_candidate

@router.get("/{candidate_id}", response_model=CandidateResponse)
async def get_candidate(candidate_id: str, db: AsyncSession = Depends(get_db)):
    try:
        candidate_uuid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")
    result = await db.execute(select(Candidate).where(Candidate.id == candidate_uuid))
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate

@router.put("/{candidate_id}", response_model=CandidateResponse)
async def update_candidate(candidate_id: str, candidate_update: CandidateUpdate, db: AsyncSession = Depends(get_db)):
    try:
        candidate_uuid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")
    result = await db.execute(select(Candidate).where(Candidate.id == candidate_uuid))
    db_candidate = result.scalar_one_or_none()
    if not db_candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
        
    update_data = candidate_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_candidate, key, value)
        
    try:
        await db.commit()
        await db.refresh(db_candidate)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"Database error: {str(e)}")
    return db_candidate

@router.post("/{candidate_id}/resumes", response_model=ResumeResponse, status_code=201)
async def upload_candidate_resume(
    candidate_id: str,
    file: UploadFile = File(...),
    is_base: str = Form("true"),
    db: AsyncSession = Depends(get_db)
):
    try:
        candidate_uuid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")
    # Verify candidate exists
    result = await db.execute(select(Candidate).where(Candidate.id == candidate_uuid))
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # Upload directly to Supabase "resume" bucket (no local saving)
    import tempfile
    from module3.utils.storage import upload_file_to_supabase as _upload

    content = await file.read()
    supabase_name = f"{candidate_id}_base_{uuid.uuid4().hex[:8]}.pdf"
    file_url: str | None = None

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(content)
        tmp.flush()
        try:
            uploaded_url = await _upload(tmp.name, "resume", supabase_name)
            if uploaded_url and uploaded_url.startswith("http"):
                file_url = uploaded_url
        except Exception:
            pass

    if not file_url:
        raise HTTPException(status_code=500, detail="Failed to upload resume to Supabase. Check server logs for details.")

    # Parse boolean
    is_base_bool = is_base.lower() == "true"

    # Find the maximum version for this candidate's resumes to set next version
    version_result = await db.execute(
        select(Resume.version)
        .where(Resume.candidate_id == candidate_uuid)
        .order_by(Resume.version.desc())
        .limit(1)
    )
    max_version = version_result.scalar_one_or_none()
    next_version = (max_version or 0) + 1

    # Create Resume DB record
    db_resume = Resume(
        candidate_id=candidate_uuid,
        version=next_version,
        file_url=file_url,
        is_base=is_base_bool,
        parsed_json=None
    )
    db.add(db_resume)
    await db.commit()
    await db.refresh(db_resume)
    return db_resume

class ApplyRequest(BaseModel):
    max_apps: int = 10

@router.post("/{candidate_id}/apply")
async def trigger_apply(candidate_id: str, request: ApplyRequest, db: AsyncSession = Depends(get_db)):
    try:
        candidate_uuid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")
    # Verify candidate exists
    result = await db.execute(select(Candidate).where(Candidate.id == candidate_uuid))
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
        
    from app.tasks.dynamic_apply import _run
    import asyncio
    
    # Bypass Celery due to Upstash Redis limitations. Run directly in background.
    def run_in_background():
        try:
            asyncio.run(_run(candidate_id, request.max_apps))
        except Exception as e:
            import logging
            logging.error(f"Failed background apply: {e}")

    import threading
    threading.Thread(target=run_in_background, daemon=True).start()
    
    return {"status": "queued", "candidate_id": candidate_id, "max_apps": request.max_apps}

from app.config import get_settings
import httpx

settings = get_settings()

class GoogleCallbackRequest(BaseModel):
    code: str

@router.get("/{candidate_id}/google/auth-url")
async def get_google_auth_url(candidate_id: str, db: AsyncSession = Depends(get_db)):
    try:
        candidate_uuid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")
    result = await db.execute(select(Candidate).where(Candidate.id == candidate_uuid))
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
        
    client_id = settings.google_client_id
    if not client_id:
        return {
            "auth_url": "",
            "is_mock": True
        }
        
    from urllib.parse import urlencode
    params = {
        "client_id": client_id,
        "redirect_uri": "http://localhost:3000/candidates/oauth-callback",
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
        "access_type": "offline",
        "prompt": "consent",
        "state": candidate_id
    }
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return {
        "auth_url": auth_url,
        "is_mock": False
    }

@router.post("/{candidate_id}/google/callback")
async def google_callback(candidate_id: str, request: GoogleCallbackRequest, db: AsyncSession = Depends(get_db)):
    try:
        candidate_uuid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")
    result = await db.execute(select(Candidate).where(Candidate.id == candidate_uuid))
    db_candidate = result.scalar_one_or_none()
    if not db_candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
        
    client_id = settings.google_client_id
    client_secret = settings.google_client_secret
    
    if not client_id or not client_secret:
        db_candidate.google_refresh_token = f"mock_refresh_token_for_{candidate_id}"
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=400, detail=f"Database error: {str(e)}")
        return {"status": "success", "message": "Simulated Google OAuth connected successfully", "mock": True}
        
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": request.code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": "http://localhost:3000/candidates/oauth-callback",
                "grant_type": "authorization_code",
            }
        )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to exchange Google authorization code: {resp.text}"
            )
        
        token_data = resp.json()
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            raise HTTPException(
                status_code=400,
                detail="No refresh token returned by Google. Try removing access and connecting again."
            )
            
        db_candidate.google_refresh_token = refresh_token
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=400, detail=f"Database error: {str(e)}")
        return {"status": "success", "message": "Google OAuth connected successfully", "mock": False}