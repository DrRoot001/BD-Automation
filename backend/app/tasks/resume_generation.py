import asyncio
import logging
from typing import Optional
from app.celery_app import celery_app
from module3.orchestrator import orchestrate_application_package

logger = logging.getLogger(__name__)

@celery_app.task(name="task:prepare_application_package")
def prepare_application_package(
    candidate_id: str,
    job_id: str,
    existing_app_id: Optional[str] = None,
    prefetched_match_result: Optional[dict] = None,
):
    """Trigger Module 3 orchestration for a candidate+job.

    prefetched_match_result: serialised MatchResult dict from the matching pipeline.
    When provided, the orchestrator skips its own LLM score call (avoids a
    duplicate Gemini round-trip for jobs already scored in matching.py).
    """
    logger.info(f"[CELERY] task:prepare_application_package started for candidate={candidate_id}, job={job_id}")

    match_result = None
    if prefetched_match_result:
        try:
            from module3.scoring.fit_scorer import MatchResult
            match_result = MatchResult(**prefetched_match_result)
        except Exception as e:
            logger.warning(f"[CELERY] Could not deserialise prefetched_match_result: {e} — will re-score")

    result = asyncio.run(
        orchestrate_application_package(
            candidate_id=str(candidate_id),
            job_id=str(job_id),
            existing_app_id=existing_app_id,
            prefetched_match_result=match_result,
        )
    )

    logger.info(f"[CELERY] task:prepare_application_package completed for candidate={candidate_id}, job={job_id}")
    return result
