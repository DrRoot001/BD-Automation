from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from uuid import UUID
from datetime import datetime
from typing import List, Optional, Dict
from pydantic import BaseModel

from app.database import get_db
from app.redis_client import redis_client
from app.models.application import Application
from app.models.application_history import ApplicationHistory
from app.models.candidate import Candidate
from app.schemas.application import StatusUpdateRequest, ApplicationResponse, ApplicationCreate, ApplicationStatus
from app.services.state_machine import validate_transition, InvalidTransitionError
from app.services.events import publish_event
from app.routers.auth import get_current_user
from app.models.user import User, UserRole


class ApplicationHistoryResponse(BaseModel):
    id: UUID
    application_id: UUID
    from_status: Optional[str]
    to_status: str
    meta_data: Optional[dict]
    created_at: datetime

    model_config = {"from_attributes": True}

router = APIRouter(prefix="/api/applications", tags=["applications"])

@router.post("", response_model=ApplicationResponse, status_code=201)
async def create_application(
    application: ApplicationCreate,
    db: AsyncSession = Depends(get_db)
):
    # Check if application already exists for this candidate and job
    result = await db.execute(
        select(Application).where(
            (Application.candidate_id == application.candidate_id) &
            (Application.job_id == application.job_id)
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing
        
    db_application = Application(**application.model_dump())
    db.add(db_application)
    await db.commit()
    await db.refresh(db_application)
    
    # Audit history log
    history = ApplicationHistory(
        application_id=db_application.id,
        from_status=None,
        to_status=db_application.status,
        metadata={"info": "Application created via API"}
    )
    db.add(history)
    await db.commit()
    
    # Publish event for Module 5 WebSocket so it appears instantly
    await publish_event("application.created", {
        "application_id": str(db_application.id),
        "candidate_id": str(db_application.candidate_id),
        "job_id": str(db_application.job_id),
        "status": db_application.status,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    return db_application

@router.get("", response_model=List[ApplicationResponse])
async def list_applications(
    candidate_id: Optional[UUID] = None,
    job_id: Optional[UUID] = None,
    status: Optional[ApplicationStatus] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List applications.

    - **Admin**: sees all applications (optionally filtered by candidate_id / status).
    - **BD user**: only sees applications belonging to their own candidates.
    """
    query = select(Application)

    # Ownership filter for non-admin users
    if current_user.role != UserRole.admin:
        owned_candidates_subq = select(Candidate.id).where(
            Candidate.user_id == current_user.id
        )
        query = query.where(Application.candidate_id.in_(owned_candidates_subq))

    if candidate_id:
        query = query.where(Application.candidate_id == candidate_id)
    if job_id:
        query = query.where(Application.job_id == job_id)
    if status:
        query = query.where(Application.status == status.value)

    query = query.order_by(Application.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    apps = result.scalars().all()

    # Resolve resume_url for each application
    from app.models.resume import Resume
    for app in apps:
        if app.resume_id:
            res = await db.get(Resume, app.resume_id)
            if res and res.file_url:
                setattr(app, "resume_url", res.file_url)
        if not getattr(app, "resume_url", None):
            res_stmt = (
                select(Resume)
                .where(Resume.candidate_id == app.candidate_id)
                .order_by(Resume.is_base.asc(), Resume.version.desc())
                .limit(1)
            )
            res = (await db.execute(res_stmt)).scalars().first()
            if res and res.file_url:
                setattr(app, "resume_url", res.file_url)

    return apps

@router.get("/{application_id}", response_model=ApplicationResponse)
async def get_application(
    application_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Application).where(Application.id == application_id)
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(404, "Application not found")

    # Resolve resume_url if resume_id is set or from candidate's latest resume
    from app.models.resume import Resume
    if app.resume_id:
        res = await db.get(Resume, app.resume_id)
        if res and res.file_url:
            setattr(app, "resume_url", res.file_url)
    if not getattr(app, "resume_url", None):
        res_stmt = (
            select(Resume)
            .where(Resume.candidate_id == app.candidate_id)
            .order_by(Resume.is_base.asc(), Resume.version.desc())
            .limit(1)
        )
        res = (await db.execute(res_stmt)).scalars().first()
        if res and res.file_url:
            setattr(app, "resume_url", res.file_url)

    return app

@router.get("/{application_id}/history", response_model=List[ApplicationHistoryResponse])
async def get_application_history(
    application_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(ApplicationHistory)
        .where(ApplicationHistory.application_id == application_id)
        .order_by(ApplicationHistory.created_at.asc())
    )
    return result.scalars().all()


@router.patch("/{application_id}/status", response_model=ApplicationResponse)
async def update_status(
    application_id: UUID,
    update: StatusUpdateRequest,
    db: AsyncSession = Depends(get_db)
):
    # 1. Fetch current application
    result = await db.execute(
        select(Application).where(Application.id == application_id)
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(404, "Application not found")
    
    # 2. Validate transition
    try:
        validate_transition(app.status, update.status.value)
    except InvalidTransitionError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    
    old_status = app.status
    
    # 3. Update application
    app.status = update.status.value
    if update.status.value == "SUBMITTED":
        app.submitted_at = datetime.utcnow()
    if update.status.value == "QUEUED":
        app.retry_count = 0
        
    if update.resume_id is not None:
        app.resume_id = update.resume_id
    if update.cover_letter_url is not None:
        app.cover_letter_url = update.cover_letter_url
    if update.fit_score is not None:
        app.fit_score = update.fit_score
    if update.ats_score is not None:
        app.ats_score = update.ats_score
    if update.combined_score is not None:
        app.combined_score = update.combined_score
    if update.failure_reason is not None:
        app.failure_reason = update.failure_reason.value if hasattr(update.failure_reason, 'value') else update.failure_reason

    # Store screenshot_url and error_message in local DB if present in metadata
    meta = update.metadata or {}
    if "screenshot_url" in meta and meta["screenshot_url"] is not None:
        app.screenshot_url = meta["screenshot_url"]
    if "error_message" in meta and meta["error_message"] is not None:
        app.error_message = meta["error_message"]
    
    # 4. Write audit history only if status actually changed
    if old_status != update.status.value:
        history = ApplicationHistory(
            application_id=application_id,
            from_status=old_status,
            to_status=update.status.value,
            metadata=update.metadata or {}
        )
        db.add(history)
    await db.commit()
    await db.refresh(app)
    
    # 5. Publish event for Module 5 WebSocket (only when status actually changed)
    if old_status != update.status.value:
        await publish_event("application.status_changed", {
            "application_id": str(application_id),
            "candidate_id": str(app.candidate_id),
            "from_status": old_status,
            "to_status": update.status.value,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    return app


class PreparePackageRequest(BaseModel):
    candidate_id: str
    job_id: str
    needs_cover_letter: bool
    screening_questions: List[str]
    skip_gate: Optional[bool] = False

class PreparePackageResponse(BaseModel):
    should_apply: bool
    reason: Optional[str] = None
    resume_pdf_url: Optional[str] = None
    cover_letter_pdf_url: Optional[str] = None
    screening_answers: Optional[Dict[str, str]] = None

@router.post("/prepare-package", response_model=PreparePackageResponse)
async def prepare_package(request: PreparePackageRequest):
    """
    Synchronous endpoint for M4 to request custom tailored resume,
    cover letter (if needed), and screening question answers.
    """
    import os
    from module3.orchestrator import prepare_package_for_live_application
    # Derive the API base URL from the server's own base address so that
    # prepare_package_for_live_application can make internal API calls correctly.
    api_base_url = os.getenv("M1_API_BASE_URL", "http://127.0.0.1:8000/api").rstrip("/")
    if api_base_url.endswith("/api"):
        api_base_url = api_base_url[: -len("/api")]

    try:
        result = await prepare_package_for_live_application(
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            needs_cover_letter=request.needs_cover_letter,
            screening_questions=request.screening_questions,
            api_base_url=api_base_url,
            skip_gate=request.skip_gate
        )
        return PreparePackageResponse(**result)
    except ValueError as ve:
        if "No base resume" in str(ve):
            raise HTTPException(
                status_code=422,
                detail={"error": "no_base_resume", "message": str(ve)}
            )
        raise HTTPException(
            status_code=400,
            detail=str(ve)
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error preparing application package: {str(e)}"
        )

@router.post("/{application_id}/retry", response_model=ApplicationResponse)
async def retry_application(
    application_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    # 1. Fetch current application
    result = await db.execute(
        select(Application).where(Application.id == application_id)
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(404, "Application not found")
        
    # 2. Fetch associated job and candidate
    from app.models.job import Job
    from app.models.candidate import Candidate
    from app.models.resume import Resume
    
    job = await db.get(Job, app.job_id)
    if not job:
        raise HTTPException(404, "Job not found")
        
    candidate = await db.get(Candidate, app.candidate_id)
    if not candidate:
        raise HTTPException(404, "Candidate not found")
        
    # 3. Retrieve tailored resume URL
    resume_url = ""
    if app.resume_id:
        resume = await db.get(Resume, app.resume_id)
        if resume:
            resume_url = resume.file_url
            
    # 4. Fetch screening answers from history
    screening_answers = {}
    history_stmt = (
        select(ApplicationHistory)
        .where(
            ApplicationHistory.application_id == application_id,
            ApplicationHistory.to_status == "QUEUED"
        )
        .order_by(ApplicationHistory.created_at.desc())
        .limit(1)
    )
    history_rec = (await db.execute(history_stmt)).scalars().first()
    if history_rec:
        meta = getattr(history_rec, "meta_data", None) or getattr(history_rec, "metadata", None)
        if isinstance(meta, dict):
            screening_answers = meta.get("screening_answers") or {}
            
    old_status = app.status
    
    # 5. Reset application status and details
    app.status = "QUEUED"
    app.retry_count = 0
    app.error_message = None
    app.failure_reason = None
    
    # 6. Write history
    history = ApplicationHistory(
        application_id=application_id,
        from_status=old_status,
        to_status="QUEUED",
        meta_data={"screening_answers": screening_answers, "info": "Retry triggered manually from dashboard"}
    )
    db.add(history)
    await db.commit()
    await db.refresh(app)
    
    # 7. Publish websocket event for UI update
    await publish_event("application.status_changed", {
        "application_id": str(application_id),
        "candidate_id": str(app.candidate_id),
        "from_status": old_status,
        "to_status": "QUEUED",
        "timestamp": datetime.utcnow().isoformat()
    })
    
    # 8. Dispatch celery task
    from app.tasks.browser_automation import execute_application
    package = {
        "application_id": str(application_id),
        "candidate_id": str(app.candidate_id),
        "job_id": str(app.job_id),
        "job_url": job.source_url or "",
        "platform": (job.source or "").lower(),
        "ats_type": job.job_type or "",
        "resume_url": resume_url,
        "cover_letter_url": app.cover_letter_url or "",
        "screening_answers": screening_answers
    }
    execute_application.apply_async(args=[package], queue="queue:application_execution")

    return app


@router.post("/admin/fail-stuck")
async def admin_fail_stuck_applications(
    hours: int = 5,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Admin endpoint: force-fail all non-terminal applications created in the last N hours."""
    if current_user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Admin only")

    from app.services.state_machine import fail_applications_in_window_async
    count = await fail_applications_in_window_async(db, hours=hours)
    return {"failed_count": count, "hours": hours}


@router.delete("/admin/reset-candidate/{candidate_id}")
async def admin_reset_candidate_applications(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete all applications (and their history) for a candidate so the
    pipeline can re-process them from scratch. Intended for dev/testing only."""
    from app.models.application_history import ApplicationHistory

    # Fetch application IDs for this candidate first
    id_rows = (await db.execute(
        select(Application.id).where(Application.candidate_id == candidate_id)
    )).scalars().all()

    if not id_rows:
        return {"deleted": 0, "candidate_id": str(candidate_id)}

    # Delete history rows first (FK constraint)
    await db.execute(
        delete(ApplicationHistory).where(ApplicationHistory.application_id.in_(id_rows))
    )
    # Delete the applications
    await db.execute(
        delete(Application).where(Application.candidate_id == candidate_id)
    )
    await db.commit()

    return {"deleted": len(id_rows), "candidate_id": str(candidate_id)}