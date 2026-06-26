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


@celery_app.task(name="task:scan_single_inbox", bind=True, max_retries=2)
def scan_single_inbox(self, candidate_id: str):
    """
    Isolated Celery task to scan a single candidate's inbox.
    """
    async def _scan():
        from app.database import AsyncSessionLocal
        from module5.scanner import scan_candidate_inbox as _scan_inbox
        async with AsyncSessionLocal() as db:
            return await _scan_inbox(candidate_id, db)

    try:
        count = _run(_scan())
        logger.info("[EmailScan] Processed %d emails for candidate %s", count, candidate_id)
        return {"processed": count, "candidate_id": candidate_id}
    except Exception as exc:
        logger.error("[EmailScan] Failed for candidate %s: %s", candidate_id, exc)
        raise self.retry(exc=exc, countdown=60)


@celery_app.task(name="task:scan_candidate_inbox", bind=True)
def scan_candidate_inbox(self):
    """
    Poll Gmail for new emails. Dispatches one task per connected candidate.
    Triggered every 15 minutes by Celery Beat.
    """
    async def _fetch_connected_candidates():
        from app.database import AsyncSessionLocal
        from sqlalchemy import text
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text("SELECT id FROM candidates WHERE google_refresh_token IS NOT NULL")
            )
            return [str(row[0]) for row in result.fetchall()]

    try:
        candidate_ids = _run(_fetch_connected_candidates())
        logger.info("[EmailScan] Dispatching email scan for %d candidates", len(candidate_ids))
        
        for cid in candidate_ids:
            scan_single_inbox.delay(cid)
            
        return {"status": "dispatched", "candidate_count": len(candidate_ids)}
    except Exception as exc:
        logger.error(f"[EmailScan] Dispatch failed: {exc}")
        raise self.retry(exc=exc, countdown=60)


@celery_app.task(name="task:refresh_analytics", bind=True)
def refresh_analytics(self):
    """
    Recompute and cache dashboard analytics every hour.
    (Currently a no-op — dashboard endpoints query live; add Redis caching here if needed.)
    """
    logger.info("[Analytics] refresh_analytics task called — live queries in use")
    return {"status": "ok"}
