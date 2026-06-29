"""Background tasks for data cleanup and retention."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from app.celery_app import celery_app
from app.database import task_session
from app.models.resume import Resume

logger = logging.getLogger(__name__)

def _run(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

@celery_app.task(name="task:cleanup_old_resumes")
def cleanup_old_resumes():
    """
    Delete tailored resume versions older than 30 days.
    Keeps base resumes and potentially the 5 most recent per candidate.
    """
    logger.info("[Cleanup] Starting old resumes cleanup...")

    async def _cleanup():
        deleted_count = 0
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)
        
        async with task_session() as session:
            # Find tailored resumes older than 30 days
            stmt = select(Resume).where(
                Resume.is_base == False,
                Resume.created_at < cutoff_date
            )
            result = await session.execute(stmt)
            old_resumes = result.scalars().all()

            # We could optionally enforce keeping the last N, but for simplicity
            # we just delete anything tailored > 30 days old.
            for resume in old_resumes:
                try:
                    # If you use Supabase storage, you would delete the file here:
                    # await storage_client.delete_file(resume.file_url)
                    
                    await session.delete(resume)
                    deleted_count += 1
                except Exception as e:
                    logger.error("[Cleanup] Failed to delete resume %s: %s", resume.id, e)

            if deleted_count > 0:
                await session.commit()
                
        return deleted_count

    try:
        count = _run(_cleanup())
        logger.info("[Cleanup] Deleted %d old tailored resumes", count)
        return {"deleted": count}
    except Exception as exc:
        logger.error("[Cleanup] Task failed: %s", exc)
        raise celery_app.retry(exc=exc, countdown=300)
