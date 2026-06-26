from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
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
    if status:
        query = query.where(Application.status == status.value)

    query = query.order_by(Application.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()

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
    
    # 4. Write audit history
    history = ApplicationHistory(
        application_id=application_id,
        from_status=old_status,
        to_status=update.status.value,
        metadata=update.metadata or {}
    )
    db.add(history)
    await db.commit()
    await db.refresh(app)
    
    # 5. Publish event for Module 5 WebSocket
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
    api_base_url = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api").rstrip("/")
    if api_base_url.endswith("/api"):
        api_base_url = api_base_url[: -len("/api")]

    try:
        result = await prepare_package_for_live_application(
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            needs_cover_letter=request.needs_cover_letter,
            screening_questions=request.screening_questions,
            api_base_url=api_base_url,
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