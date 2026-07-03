import uuid
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List
from pydantic import BaseModel

from app.database import get_db
from app.models.candidate import Candidate
from app.models.resume import Resume
from app.models.application import Application
from app.schemas.candidate import CandidateCreate, CandidateResponse, CandidateUpdate
from app.schemas.resume import ResumeResponse

from app.routers.auth import get_current_user
from app.models.user import User, UserRole
from app.services.crypto import encrypt_token, decrypt_token
from app.middleware.rate_limit import limiter

router = APIRouter(prefix="/api/candidates", tags=["candidates"])

@router.get("", response_model=List[CandidateResponse])
async def list_candidates(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = select(Candidate).order_by(Candidate.created_at.desc())
    if current_user.role != UserRole.admin:
        query = query.where(Candidate.user_id == current_user.id)
        
    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()

@router.post("", response_model=CandidateResponse, status_code=201)
async def create_candidate(
    candidate: CandidateCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Check if candidate exists by email
    if candidate.email:
        result = await db.execute(select(Candidate).where(Candidate.email == candidate.email))
        existing_candidate = result.scalars().first()
        if existing_candidate:
            if existing_candidate.user_id != current_user.id:
                existing_candidate.user_id = current_user.id
                await db.commit()
                await db.refresh(existing_candidate)
            return existing_candidate

    db_candidate = Candidate(**candidate.model_dump())
    db_candidate.user_id = current_user.id
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

    import os
    # Create temp file, close it immediately to release Windows file lock before uploading
    fd, temp_path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(content)
        # Upload now that the file handle is closed
        uploaded_url = await _upload(temp_path, "resume", supabase_name, clean_local=True)
        if uploaded_url and uploaded_url.startswith("http"):
            file_url = uploaded_url
    finally:
        # Ensure cleanup in case upload failed or didn't delete the file
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    if not file_url:
        raise HTTPException(status_code=500, detail="Failed to upload resume to Supabase. Check server logs for details.")

    # Parse boolean
    is_base_bool = is_base.lower() == "true"

    if is_base_bool:
        from sqlalchemy import update
        await db.execute(
            update(Resume)
            .where(Resume.candidate_id == candidate_uuid, Resume.is_base == True)
            .values(is_base=False)
        )

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


@router.get("/{candidate_id}/applications")
async def list_candidate_applications(
    candidate_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Internal endpoint: list all applications for a candidate (no auth required).
    
    Used by the dynamic_apply background task to check which jobs have already
    been applied to, avoiding duplicate application submissions.
    """
    try:
        candidate_uuid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")
    result = await db.execute(
        select(Application).where(Application.candidate_id == candidate_uuid)
    )
    applications = result.scalars().all()
    return [
        {
            "id": str(a.id),
            "candidate_id": str(a.candidate_id),
            "job_id": str(a.job_id),
            "status": a.status,
            "failure_reason": a.failure_reason,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in applications
    ]


class ApplyRequest(BaseModel):
    max_apps: int = 10

@router.post("/{candidate_id}/apply")
async def trigger_apply(request: Request, candidate_id: str, request_body: ApplyRequest, db: AsyncSession = Depends(get_db)):
    import logging as _bg_logging
    _bg_log = _bg_logging.getLogger("dynamic_apply.trigger")

    try:
        candidate_uuid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")

    result = await db.execute(select(Candidate).where(Candidate.id == candidate_uuid))
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    from app.tasks.dynamic_apply import dynamic_apply

    try:
        dynamic_apply.apply_async(args=[candidate_id, request_body.max_apps])
        _bg_log.info(f"[BG] Auto-apply task dispatched to Celery: candidate={candidate_id} max_apps={request_body.max_apps}")
    except Exception as e:
        _bg_log.error(f"[Apply] Failed to dispatch Celery task for candidate={candidate_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start apply pipeline: {e}")

    return {"status": "queued", "candidate_id": candidate_id, "max_apps": request_body.max_apps}

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
        # Store mock token (plaintext is fine for development mock)
        db_candidate.google_refresh_token = encrypt_token(f"mock_refresh_token_for_{candidate_id}")
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
            
        db_candidate.google_refresh_token = encrypt_token(refresh_token)
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=400, detail=f"Database error: {str(e)}")
        return {"status": "success", "message": "Google OAuth connected successfully", "mock": False}


@router.post("/{candidate_id}/google/disconnect")
async def disconnect_google(candidate_id: str, db: AsyncSession = Depends(get_db)):
    from uuid import UUID
    from fastapi import HTTPException
    from app.models.candidate import Candidate
    
    try:
        cand_uuid = UUID(candidate_id) if isinstance(candidate_id, str) else candidate_id
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")
        
    db_candidate = await db.get(Candidate, cand_uuid)
    if not db_candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
        
    db_candidate.google_refresh_token = None
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
        
    return {"status": "success", "message": "Google OAuth disconnected successfully"}


@router.post("/{candidate_id}/run-matching")
@limiter.limit("5/minute")
async def run_matching_endpoint(
    request: Request,
    candidate_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    from uuid import UUID
    from app.services.matching import run_matching_for_candidate
    
    try:
        cand_uuid = UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")
        
    candidate = await db.get(Candidate, cand_uuid)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
        
    try:
        result = await run_matching_for_candidate(cand_uuid, db)
        if "error" in result:
            if result["error"] == "no_base_resume":
                raise HTTPException(status_code=422, detail="Candidate has no base resume in database.")
            elif result["error"] == "no_resume_embedding":
                raise HTTPException(status_code=422, detail="Candidate base resume does not have vector embeddings. Please generate embeddings first.")
            else:
                raise HTTPException(status_code=400, detail=f"Matching run failed: {result['error']}")
        return result
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal error running matching pipeline: {str(e)}")