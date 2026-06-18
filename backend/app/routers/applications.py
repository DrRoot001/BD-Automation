from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from uuid import UUID
from datetime import datetime

from app.database import get_db
from app.redis_client import redis_client
from app.models.application import Application
from app.models.application_history import ApplicationHistory
from app.schemas.application import StatusUpdateRequest, ApplicationResponse
from app.services.state_machine import validate_transition, InvalidTransitionError
from app.services.events import publish_event

router = APIRouter(prefix="/api/applications", tags=["applications"])

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
        "from_status": old_status,
        "to_status": update.status.value,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    return app