"""LLM Score Cache — Redis-backed TTL cache for (candidate_id, job_id) scores.

Why this matters:
  The daily matching loop evaluates every candidate against every new job using
  an LLM call (Gemini). If the same (candidate, job) pair is evaluated twice
  (e.g. Monday 72h lookback re-visits jobs from the weekend), the LLM call is
  redundant and wastes API budget. This cache eliminates that waste.

TTL is set to 24 hours by default — scores expire after one day so a job that
was borderline yesterday gets a fresh evaluation tomorrow.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "llm:score:"
_CACHE_TTL_SECONDS = 86400  # 24 hours


async def get_cached_score(candidate_id: str, job_id: str) -> dict | None:
    """Return a previously cached LLM fit-score result, or None on miss.

    Args:
        candidate_id: UUID string of the candidate.
        job_id:       UUID string of the job.

    Returns:
        The cached match_result dict if it exists and hasn't expired, else None.
    """
    try:
        from app.redis_client import redis_client
        key = f"{_CACHE_PREFIX}{candidate_id}:{job_id}"
        raw = await redis_client.get(key)
        if raw:
            result = json.loads(raw)
            logger.debug("[LLMCache] HIT for candidate=%s job=%s", candidate_id, job_id)
            return result
    except Exception as exc:
        # Cache failure must never break matching — treat as miss
        logger.warning("[LLMCache] GET failed: %s", exc)
    return None


async def cache_score(candidate_id: str, job_id: str, result: dict, ttl: int = _CACHE_TTL_SECONDS) -> None:
    """Store an LLM fit-score result in Redis with a TTL.

    Args:
        candidate_id: UUID string of the candidate.
        job_id:       UUID string of the job.
        result:       The match_result dict returned by score_job_fit().
        ttl:          Seconds until the cached value expires (default 24h).
    """
    try:
        from app.redis_client import redis_client
        key = f"{_CACHE_PREFIX}{candidate_id}:{job_id}"
        await redis_client.setex(key, ttl, json.dumps(result, default=str))
        logger.debug("[LLMCache] STORED for candidate=%s job=%s ttl=%ds", candidate_id, job_id, ttl)
    except Exception as exc:
        logger.warning("[LLMCache] SET failed: %s", exc)


async def invalidate_score(candidate_id: str, job_id: str) -> None:
    """Remove a cached score (e.g. when a candidate's resume is updated)."""
    try:
        from app.redis_client import redis_client
        key = f"{_CACHE_PREFIX}{candidate_id}:{job_id}"
        await redis_client.delete(key)
    except Exception as exc:
        logger.warning("[LLMCache] DELETE failed: %s", exc)


async def invalidate_all_for_candidate(candidate_id: str) -> int:
    """Remove all cached scores for a candidate (e.g. after resume update).

    Returns the number of keys deleted.
    """
    try:
        from app.redis_client import redis_client
        pattern = f"{_CACHE_PREFIX}{candidate_id}:*"
        keys = await redis_client.keys(pattern)
        if keys:
            deleted = await redis_client.delete(*keys)
            logger.info("[LLMCache] Invalidated %d cached scores for candidate %s", deleted, candidate_id)
            return deleted
    except Exception as exc:
        logger.warning("[LLMCache] Bulk invalidation failed: %s", exc)
    return 0
