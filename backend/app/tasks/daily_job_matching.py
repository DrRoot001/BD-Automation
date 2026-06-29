"""Celery tasks for daily job matching (Component 1)."""
from __future__ import annotations

import asyncio
import logging

from app.celery_app import celery_app
from app.database import task_session
from app.tasks.match_single_candidate import match_single_candidate
from celery import chord

logger = logging.getLogger(__name__)

def _run(coro):
    """Run an async coroutine from a sync Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

@celery_app.task(name="task:aggregate_matching_results")
def aggregate_matching_results(results):
    """
    Chord callback that aggregates the results of all match_single_candidate tasks.
    """
    summary = {}
    total_enqueued = 0
    for res in results:
        if isinstance(res, dict) and "candidate_id" in res:
            cid = res["candidate_id"]
            summary[cid] = {
                "jobs_scanned": res.get("jobs_scanned", 0),
                "pgvector_passed": res.get("pgvector_passed", 0),
                "enqueued_count": res.get("enqueued_count", 0)
            }
            total_enqueued += res.get("enqueued_count", 0)
    
    logger.info("[DailyMatching] Aggregation complete: %d total applications enqueued across %d candidates", 
                total_enqueued, len(summary))
    return {"total_enqueued": total_enqueued, "summary": summary}


@celery_app.task(name="task:daily_job_matching", bind=True, max_retries=1)
def daily_job_matching(self):
    """
    Daily Celery Beat task that automatically runs matching for every candidate.
    Dispatches one task per candidate in parallel.
    """
    logger.info("[DailyMatching] Starting daily_job_matching beat task...")
    
    async def _fetch_candidates():
        from sqlalchemy import text
        async with task_session() as session:
            result = await session.execute(text("SELECT id FROM candidates"))
            return [str(row[0]) for row in result.fetchall()]

    try:
        candidate_ids = _run(_fetch_candidates())
        logger.info("[DailyMatching] Found %d candidates to match. Dispatching fan-out...", len(candidate_ids))
        
        if not candidate_ids:
            return {"status": "no_candidates"}

        # Create a signature for each candidate
        tasks = [match_single_candidate.s(cid, is_beat_task=True) for cid in candidate_ids]
        
        # Chord runs all tasks in parallel, then calls aggregate_matching_results
        workflow = chord(tasks)(aggregate_matching_results.s())
        
        return {"status": "dispatched", "task_count": len(tasks), "chord_id": workflow.id}
        
    except Exception as exc:
        logger.error("[DailyMatching] Daily matching task dispatch failed: %s", exc)
        raise self.retry(exc=exc, countdown=60)
