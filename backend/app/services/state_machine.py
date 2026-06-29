from fastapi import HTTPException
from app.schemas.application import ApplicationStatus

VALID_TRANSITIONS: dict[str, list[str]] = {
    "FOUND":               ["ANALYZED", "QUEUED", "FAILED"],
    "ANALYZED":            ["MATCHED", "APPLICATION_STARTED", "FAILED"],
    "MATCHED":             ["RESUME_UPDATED", "APPLICATION_STARTED", "QUEUED", "FAILED"],
    "RESUME_UPDATED":      ["COVER_LETTER_CREATED", "FORM_COMPLETED", "APPLICATION_STARTED", "QUEUED", "FAILED"],
    "COVER_LETTER_CREATED":["QUEUED", "FAILED"],
    "QUEUED":              ["APPLICATION_STARTED", "FORM_COMPLETED", "SUBMITTED", "FAILED", "ANALYZED"],
    "APPLICATION_STARTED": ["FORM_COMPLETED", "QUEUED", "ANALYZED", "SUBMITTED", "FAILED", "BLOCKED"],
    "FORM_COMPLETED":      ["SUBMITTED", "QUEUED", "FAILED"],
    "SUBMITTED":           ["CONFIRMED", "REJECTED", "GHOSTED"],
    "CONFIRMED":           ["INTERVIEW_R1", "REJECTED", "WITHDRAWN"],
    "INTERVIEW_R1":        ["INTERVIEW_R2", "REJECTED", "WITHDRAWN", "OFFER"],
    "INTERVIEW_R2":        ["OFFER", "REJECTED", "WITHDRAWN"],
    # Terminal or pseudo-terminal states
    "FAILED":              [],
    "BLOCKED":             ["QUEUED"],   # Blocked apps can be retried
    "REJECTED":            [],
    "OFFER":               [],
    "GHOSTED":             ["QUEUED"],   # Can retry if desired
    "WITHDRAWN":           [],           # Terminal
}

class InvalidTransitionError(Exception):
    pass

def validate_transition(current: str, target: str) -> bool:
    allowed = VALID_TRANSITIONS.get(current, [])
    if target not in allowed and target != current:
        raise InvalidTransitionError(
            f"Invalid transition: {current} → {target}. Allowed: {allowed}"
        )
    return True


import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.application import Application
from app.models.application_history import ApplicationHistory

logger = logging.getLogger(__name__)

async def recover_stuck_applications_async(session: AsyncSession) -> None:
    """
    Finds applications stuck in QUEUED, APPLICATION_STARTED, or FORM_COMPLETED
    for more than their respective thresholds and marks them as FAILED.
    Uses a single grouped query instead of N+1 history lookups.

    ANALYZED is intentionally excluded: it is a valid terminal state (fit score
    below threshold, tailored docs saved) and waits for manual action. It must
    never be killed by the watchdog.

    The URL validity check was removed: it made synchronous HTTP requests for
    every active job inside the matching pipeline, blocking the pipeline for
    tens of seconds per run and causing false-positive JOB_EXPIRED kills on
    transient 404s.  Job expiry is now handled opportunistically by the browser
    automation layer when it actually tries to load the page.
    """
    from sqlalchemy import func, or_, text

    now = datetime.now(timezone.utc)
    
    # 1. Fetch stuck apps and their latest history timestamp in a single grouped query
    stmt = text("""
        SELECT a.id, a.status, COALESCE(MAX(h.created_at), a.created_at) as last_updated
        FROM applications a
        LEFT JOIN application_history h ON h.application_id = a.id
        WHERE a.status IN ('QUEUED', 'APPLICATION_STARTED', 'FORM_COMPLETED')
        GROUP BY a.id, a.status, a.created_at
    """)
    
    result = await session.execute(stmt)
    rows = result.fetchall()
    
    stuck_ids_and_statuses = []
    
    for row in rows:
        app_id, status, last_updated = row
        
        if last_updated.tzinfo is None:
            last_updated = last_updated.replace(tzinfo=timezone.utc)
            
        if status == "QUEUED":
            # LLM scoring + resume tailoring can take 5–20 min;
            # 60 min prevents watchdog from killing apps before they reach APPLICATION_STARTED.
            threshold_min = 60
        else:
            threshold_min = 30
            
        time_limit = now - timedelta(minutes=threshold_min)
        
        if last_updated < time_limit:
            stuck_ids_and_statuses.append((app_id, status, last_updated))
            
    if not stuck_ids_and_statuses:
        return
        
    stuck_ids = [row[0] for row in stuck_ids_and_statuses]
    
    # Fetch the actual Application objects to update
    apps_stmt = select(Application).where(Application.id.in_(stuck_ids))
    apps_to_update = (await session.execute(apps_stmt)).scalars().all()
    
    # Map them for easy access
    app_map = {app.id: app for app in apps_to_update}
    
    stuck_count = 0
    for app_id, status, last_updated in stuck_ids_and_statuses:
        app = app_map.get(app_id)
        if not app:
            continue
            
        old_status = app.status
        app.status = "FAILED"
        app.failure_reason = "INFRA_ERROR"
        app.error_message = "automation timeout — worker died or never picked up task"
        
        history = ApplicationHistory(
            application_id=app.id,
            from_status=old_status,
            to_status="FAILED",
            meta_data={"info": f"Marked FAILED by watchdog due to automation timeout (stuck in {old_status} since {last_updated})"}
        )
        session.add(history)
        stuck_count += 1
        
        try:
            from app.tasks.browser_automation import publish_status_changed
            await publish_status_changed(
                application_id=str(app.id),
                from_status=old_status,
                to_status="FAILED"
            )
        except Exception as ev_err:
            logger.error(f"[Watchdog] failed to publish status changed event for {app.id}: {ev_err}")
            
    if stuck_count > 0:
        await session.commit()
        logger.info(f"[Watchdog] Successfully recovered {stuck_count} stuck applications.")