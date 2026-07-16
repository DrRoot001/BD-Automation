import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.rate_limit import limiter
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.resume import Resume
from app.models.user import User, UserRole
from app.routers.auth import get_current_user, require_admin
from app.schemas.candidate import CandidateCreate, CandidateResponse, CandidateUpdate
from app.schemas.resume import ResumeResponse
from app.services.crypto import encrypt_token

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
        err_msg = str(e)
        if "candidates_email_key" in err_msg or "UniqueViolationError" in err_msg:
            raise HTTPException(status_code=400, detail="A candidate with this email address already exists.")
        raise HTTPException(status_code=400, detail=f"Database error: {err_msg}")
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

    # Credential-wipe guard: the API never RETURNS the stored portal credentials
    # (password is response-excluded for security), so a frontend edit form
    # starts BLANK for them. Saving that form re-sends password="" / gmail="",
    # and via exclude_unset those empties would overwrite the real values —
    # silently wiping the login used for every account-walled apply (Dice,
    # Glassdoor, iCIMS, Workday, …) and making them fail with "requires
    # credentials". Treat an empty/whitespace credential as "leave unchanged";
    # only a real, non-empty value is allowed to overwrite the stored one.
    for _cred in ("password", "gmail"):
        if _cred in update_data and not (str(update_data.get(_cred) or "")).strip():
            update_data.pop(_cred, None)

    if "email" in update_data and update_data["email"]:
        new_email = update_data["email"].strip()
        update_data["email"] = new_email
        email_check = await db.execute(
            select(Candidate).where(
                Candidate.email == new_email,
                Candidate.id != candidate_uuid
            )
        )
        if email_check.scalars().first():
            raise HTTPException(
                status_code=400,
                detail=f"A candidate with email '{new_email}' already exists."
            )

    for key, value in update_data.items():
        setattr(db_candidate, key, value)

    try:
        await db.commit()
        await db.refresh(db_candidate)
    except Exception as e:
        await db.rollback()
        err_msg = str(e)
        if "candidates_email_key" in err_msg or "UniqueViolationError" in err_msg:
            raise HTTPException(status_code=400, detail="A candidate with this email address already exists.")
        raise HTTPException(status_code=400, detail=f"Database error: {err_msg}")
    return db_candidate

class CandidateAssignRequest(BaseModel):
    user_id: Optional[uuid.UUID] = None


@router.patch("/{candidate_id}/assign", response_model=CandidateResponse)
async def assign_candidate(
    candidate_id: str,
    payload: CandidateAssignRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Admin-only: assign a candidate to a BD user (user_id=null returns it to the system pool)."""
    try:
        candidate_uuid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")

    result = await db.execute(select(Candidate).where(Candidate.id == candidate_uuid))
    db_candidate = result.scalar_one_or_none()
    if not db_candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    if payload.user_id is not None:
        user_result = await db.execute(select(User).where(User.id == payload.user_id))
        if not user_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="User not found")

    db_candidate.user_id = payload.user_id
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
    time_filter: Optional[str] = None
    platform: Optional[str] = None

@router.post("/{candidate_id}/apply")
async def trigger_apply(
    request: Request,
    candidate_id: str,
    request_body: ApplyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
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

    # Enforce that the candidate has a base resume
    resume_stmt = select(Resume).where(Resume.candidate_id == candidate_uuid, Resume.is_base == True)
    resume_result = await db.execute(resume_stmt)
    base_resume = resume_result.scalars().first()
    if not base_resume:
        raise HTTPException(
            status_code=400,
            detail="Candidate has no base resume. Please upload a resume first."
        )

    # Enforce ownership: BD users can only trigger apply for their own candidates
    if current_user.role != UserRole.admin:
        if candidate.user_id is None or str(candidate.user_id) != str(current_user.id):
            raise HTTPException(
                status_code=403,
                detail="Access denied: this candidate is not assigned to your account."
            )

    # Pass the triggering user's supabase_user_id so Celery workers can scope
    # WebSocket events to only the correct BD user's browser session.
    bd_user_id = str(current_user.supabase_user_id) if current_user.supabase_user_id else None

    from app.tasks.dynamic_apply import dynamic_apply
    import asyncio, functools

    try:
        # apply_async is a synchronous blocking call (it opens a Redis connection
        # to enqueue the task). Running it directly in the async endpoint would
        # freeze the entire asyncio event loop while Redis connects, preventing
        # uvicorn from sending the response → ECONNRESET. Offload to a thread so
        # the event loop stays alive and the except block can actually fire.
        dispatch = functools.partial(
            dynamic_apply.apply_async,
            args=[candidate_id, request_body.max_apps, bd_user_id],
            kwargs={"time_filter": request_body.time_filter, "platform": request_body.platform},
        )
        await asyncio.to_thread(dispatch)
        _bg_log.info(
            f"[BG] Auto-apply task dispatched to Celery: candidate={candidate_id} "
            f"max_apps={request_body.max_apps} time_filter={request_body.time_filter} "
            f"platform={request_body.platform} triggered_by={current_user.email}"
        )
    except Exception as e:
        _bg_log.error(f"[Apply] Failed to dispatch Celery task for candidate={candidate_id}: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Could not queue apply pipeline — broker unavailable: {e}",
        )

    return {"status": "queued", "candidate_id": candidate_id, "max_apps": request_body.max_apps}


# ── Pipeline stop / resume (BG-08) ────────────────────────────────────────────
# Statuses the pipeline stop can safely hold. APPLICATION_STARTED/FORM_COMPLETED
# are excluded: a browser session is actively driving those forms and flagging
# them wouldn't stop the browser — it would only hide them from the watchdog.
# They finish their current run; everything not yet in a browser freezes.
PIPELINE_PAUSABLE_STATUSES = (
    "FOUND", "MATCHED", "RESUME_UPDATED", "COVER_LETTER_CREATED", "QUEUED",
)


async def _get_owned_candidate(candidate_id: str, db: AsyncSession, current_user: User) -> Candidate:
    try:
        candidate_uuid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")

    result = await db.execute(select(Candidate).where(Candidate.id == candidate_uuid))
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    if current_user.role != UserRole.admin:
        if candidate.user_id is None or str(candidate.user_id) != str(current_user.id):
            raise HTTPException(
                status_code=403,
                detail="Access denied: this candidate is not assigned to your account.",
            )
    return candidate


@router.post("/{candidate_id}/pipeline/pause")
async def pause_pipeline(
    candidate_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stop the whole apply pipeline for a candidate (BG-08).

    Sets candidate.automation_paused (blocks new matching runs and halts an
    active run at its next job boundary) and bulk-pauses every in-flight
    application (paused=2) so none of them run or count toward the active cap.
    Apps already inside a live browser run are left to finish and reported back.
    """
    from datetime import datetime as _dt

    from app.models.application_history import ApplicationHistory
    from app.services.events import publish_event

    candidate = await _get_owned_candidate(candidate_id, db, current_user)
    candidate.automation_paused = 1

    apps_result = await db.execute(
        select(Application).where(
            Application.candidate_id == candidate.id,
            Application.status.in_(PIPELINE_PAUSABLE_STATUSES),
            Application.paused == 0,
        )
    )
    apps = apps_result.scalars().all()
    for app in apps:
        app.paused = 2  # pipeline-paused; distinguishes from an individual row pause
        db.add(ApplicationHistory(
            application_id=app.id,
            from_status=app.status,
            to_status=app.status,
            meta_data={"info": "Paused via pipeline stop (candidate-level)"},
        ))

    # Count in-browser rows we deliberately can't hold, so the UI can say
    # "N applications already in a browser run will finish".
    in_browser_result = await db.execute(
        select(Application.id).where(
            Application.candidate_id == candidate.id,
            Application.status.in_(("APPLICATION_STARTED", "FORM_COMPLETED")),
        )
    )
    in_browser_count = len(in_browser_result.scalars().all())

    await db.commit()

    await publish_event("application.paused", {
        "candidate_id": str(candidate.id),
        "paused": True,
        "pipeline": True,
        "count": len(apps),
        "timestamp": _dt.utcnow().isoformat(),
    })

    return {
        "status": "pipeline_paused",
        "candidate_id": str(candidate.id),
        "paused_applications": len(apps),
        "in_browser_finishing": in_browser_count,
    }


@router.post("/{candidate_id}/pipeline/resume")
async def resume_pipeline(
    candidate_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Resume a stopped pipeline: clear automation_paused and release only the
    apps the pipeline stop paused (paused=2), re-dispatching QUEUED ones.
    Individually-paused apps (paused=1) stay held."""
    from datetime import datetime as _dt

    from app.models.application_history import ApplicationHistory
    from app.routers.applications import _dispatch_execute_application
    from app.services.events import publish_event

    candidate = await _get_owned_candidate(candidate_id, db, current_user)
    candidate.automation_paused = 0

    apps_result = await db.execute(
        select(Application).where(
            Application.candidate_id == candidate.id,
            Application.paused == 2,
        )
    )
    apps = apps_result.scalars().all()
    for app in apps:
        app.paused = 0
        db.add(ApplicationHistory(
            application_id=app.id,
            from_status=app.status,
            to_status=app.status,
            meta_data={"info": "Resumed via pipeline resume (candidate-level)"},
        ))

    await db.commit()

    # QUEUED rows' original Celery messages were consumed and skipped while
    # paused — re-dispatch them. Other states are re-driven by the pipeline.
    redispatched = 0
    for app in apps:
        if app.status == "QUEUED":
            await _dispatch_execute_application(db, app)
            redispatched += 1

    await publish_event("application.paused", {
        "candidate_id": str(candidate.id),
        "paused": False,
        "pipeline": True,
        "count": len(apps),
        "timestamp": _dt.utcnow().isoformat(),
    })

    return {
        "status": "pipeline_resumed",
        "candidate_id": str(candidate.id),
        "resumed_applications": len(apps),
        "redispatched_queued": redispatched,
    }


import httpx

from app.config import get_settings

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


class RunMatchingRequest(BaseModel):
    # Optional explicit job targets (Jobs-Feed per-job "Apply" button). When
    # provided, matching skips discovery filters and executes on exactly these
    # jobs; when absent the endpoint behaves exactly as before.
    job_ids: Optional[List[uuid.UUID]] = None


@router.post("/{candidate_id}/run-matching")
@limiter.limit("5/minute")
async def run_matching_endpoint(
    request: Request,
    candidate_id: str,
    payload: Optional[RunMatchingRequest] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    from uuid import UUID

    from app.services.matching import get_active_application_count, run_matching_for_candidate

    try:
        cand_uuid = UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate UUID")

    candidate = await db.get(Candidate, cand_uuid)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # Explicit job targeting: raise the manual limit above the current inflight
    # count so the manual-limit gate (remaining = manual_limit - inflight) never
    # blocks an operator-requested apply.
    target_job_ids: Optional[List[str]] = None
    manual_limit: Optional[int] = None
    if payload and payload.job_ids:
        target_job_ids = [str(j) for j in payload.job_ids]
        inflight = await get_active_application_count(cand_uuid, db, inflight_only=True)
        manual_limit = inflight + len(target_job_ids)

    try:
        result = await run_matching_for_candidate(
            cand_uuid, db, target_job_ids=target_job_ids, manual_limit=manual_limit
        )
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
