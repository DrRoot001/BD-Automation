"""Celery tasks for email scanning pipeline (Module 5)."""
from __future__ import annotations

import asyncio
import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run(coro):
    """Run an async coroutine from a sync Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="task:scan_candidate_inbox", bind=True, max_retries=2)
def scan_candidate_inbox(self, candidate_id: str = None):
    """
    Poll Gmail for new emails for one (or all) candidates.
    Triggered every 15 minutes by Celery Beat.
    """
    async def _scan(cid: str):
        from app.database import AsyncSessionLocal
        from module5.scanner import scan_candidate_inbox as _scan_inbox
        async with AsyncSessionLocal() as db:
            return await _scan_inbox(cid, db)

    async def _scan_all():
        from app.database import AsyncSessionLocal
        from sqlalchemy import text
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text("SELECT id FROM candidates WHERE google_refresh_token IS NOT NULL")
            )
            candidate_ids = [str(r.id) for r in result.fetchall()]
        total = 0
        for cid in candidate_ids:
            try:
                count = await _scan(cid)
                total += count
            except Exception as e:
                logger.error(f"[EmailScan] Failed for candidate {cid}: {e}")
        return total

    try:
        if candidate_id:
            count = _run(_scan(candidate_id))
        else:
            count = _run(_scan_all())
        logger.info(f"[EmailScan] Processed {count} emails")
        return {"processed": count}
    except Exception as exc:
        logger.error(f"[EmailScan] Task failed: {exc}")
        raise self.retry(exc=exc, countdown=60)


@celery_app.task(name="task:refresh_analytics", bind=True)
def refresh_analytics(self):
    """
    Recompute and cache dashboard analytics every hour.
    (Currently a no-op — dashboard endpoints query live; add Redis caching here if needed.)
    """
    logger.info("[Analytics] refresh_analytics task called — live queries in use")
    return {"status": "ok"}
