"""Celery task for matching a single candidate.

This allows the daily job matching process to fan out and process all candidates
in parallel across multiple Celery workers.
"""
from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from app.celery_app import celery_app
from app.database import task_session
from app.services.matching import run_matching_for_candidate

logger = logging.getLogger(__name__)

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

@celery_app.task(name="task:match_single_candidate", bind=True, max_retries=3)
def match_single_candidate(self, candidate_id: str, is_beat_task: bool = False):
    """
    Isolated Celery task to run matching for a single candidate.
    """
    logger.info("[MatchSingle] Starting match for candidate_id=%s", candidate_id)

    async def _match():
        cid = UUID(candidate_id)
        async with task_session() as session:
            return await run_matching_for_candidate(cid, session, is_beat_task=is_beat_task)

    try:
        result = _run(_match())
        logger.info("[MatchSingle] Match complete for candidate_id=%s: enqueued=%s", 
                    candidate_id, result.get("enqueued_count", 0))
        return result
    except Exception as exc:
        logger.error("[MatchSingle] Match failed for candidate_id=%s: %s", candidate_id, exc)
        raise self.retry(exc=exc, countdown=60)
