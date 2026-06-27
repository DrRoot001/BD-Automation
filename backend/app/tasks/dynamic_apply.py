"""Dynamic application sourcing — Phase 4 wiring of M2 → M4.

Reads jobs already discovered by M2 and stored in the central DB, scores each
job against a candidate's CV keywords (tech_stack), creates an Application
record via M1's REST API, and dispatches the M4 execution task. No hardcoded
URLs — the candidate's CV decides which jobs we apply to.

Trigger this task with:
    celery -A app.celery_app call task:dynamic_apply --args='["<candidate_id>"]'

Or via the dashboard's "Apply to matching jobs" button (M5 wiring later).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set

import httpx

from app.celery_app import celery_app
from app.services.sync_events import publish_event_sync

logger = logging.getLogger(__name__)


API_BASE = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api")
MAX_APPLICATIONS_PER_RUN = int(os.getenv("M4_MAX_APPS_PER_RUN", "10"))
SCORE_FLOOR = float(os.getenv("M4_DYNAMIC_SCORE_FLOOR", "0.25"))


_WORD_RE = re.compile(r"[A-Za-z0-9\+\-\#\.]{2,}")


def _tokens(s: str) -> Set[str]:
    if not s:
        return set()
    return {t.lower() for t in _WORD_RE.findall(s)}


def _score_job(candidate_keywords: Set[str], job: Dict[str, Any]) -> float:
    """Cheap relevance score between candidate tech_stack and a job's skills/title/description.

    We don't need a perfect score — we just need to order jobs so the top-N are
    plausibly aligned with the candidate. M3 will do the deep scoring after
    application creation; this is a coarse pre-filter to avoid spam.
    """
    if not candidate_keywords:
        return 0.0
    haystack: Set[str] = set()
    haystack.update(_tokens(job.get("title") or ""))
    haystack.update(_tokens(" ".join(job.get("skills") or [])))
    haystack.update(_tokens((job.get("description") or "")[:2000]))
    if not haystack:
        return 0.0
    overlap = candidate_keywords & haystack
    return len(overlap) / max(len(haystack), 1)


async def _fetch_candidate(client: httpx.AsyncClient, candidate_id: str) -> Optional[Dict[str, Any]]:
    r = await client.get(f"{API_BASE}/candidates/{candidate_id}")
    if r.status_code != 200:
        logger.error(f"[Dynamic] candidate fetch failed: {r.status_code}")
        return None
    return r.json()


async def _fetch_open_jobs(client: httpx.AsyncClient, limit: int = 500) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    skip = 0
    page_size = 100
    while skip < limit:
        r = await client.get(f"{API_BASE}/jobs/for-matching", params={"skip": skip, "limit": page_size})
        if r.status_code != 200:
            logger.error(f"[Dynamic] Failed to fetch jobs: HTTP {r.status_code} — {r.text}")
            raise RuntimeError(f"Failed to fetch jobs from API: HTTP {r.status_code}")
        batch = r.json() or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_size:
            break
        skip += page_size
    return out


async def _already_applied_job_ids(client: httpx.AsyncClient, candidate_id: str) -> Set[str]:
    """Get job IDs already applied to for this candidate.
    
    Uses the per-candidate applications endpoint on the candidates router which
    does NOT require auth (unlike the global /applications list endpoint).
    Falls back gracefully — create_application handles server-side dedup anyway.
    """
    try:
        # Use the candidate-scoped applications endpoint (no auth required)
        r = await client.get(f"{API_BASE}/candidates/{candidate_id}/applications")
        if r.status_code == 200:
            return {str(a.get("job_id")) for a in (r.json() or []) if a.get("job_id")}
    except Exception:
        pass
    # Fallback: query the global endpoint, silently ignore 401
    try:
        r = await client.get(f"{API_BASE}/applications", params={"candidate_id": candidate_id})
        if r.status_code == 200:
            return {str(a.get("job_id")) for a in (r.json() or []) if a.get("job_id")}
    except Exception:
        pass
    logger.warning(f"[Dynamic] Could not fetch already-applied jobs for {candidate_id}; dedup handled server-side.")
    return set()


async def _run(candidate_id: str, max_apps: int) -> Dict[str, Any]:
    queued: List[str] = []
    skipped: int = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        candidate = await _fetch_candidate(client, candidate_id)
        if not candidate:
            return {"queued": [], "skipped": 0, "error": "candidate_not_found"}

        keywords: Set[str] = set()
        keywords.update(_tokens(" ".join(candidate.get("tech_stack") or [])))
        keywords.update(_tokens(candidate.get("current_title") or ""))
        keywords.update(_tokens(candidate.get("education") or ""))
        if not keywords:
            return {"queued": [], "skipped": 0, "error": "no_candidate_keywords"}

        publish_event_sync("pipeline.progress", {
            "candidate_id": candidate_id,
            "step": "fetching_jobs",
            "message": "Fetching available jobs from database..."
        })

        jobs = await _fetch_open_jobs(client)
        if not jobs:
            return {"queued": [], "skipped": 0, "error": "no_jobs"}

        publish_event_sync("pipeline.progress", {
            "candidate_id": candidate_id,
            "step": "matching",
            "message": f"Matching candidate profile against {len(jobs)} available jobs..."
        })

        already = await _already_applied_job_ids(client, candidate_id)

        scored = []
        for job in jobs:
            jid = str(job.get("id") or job.get("job_id") or "")
            if not jid or jid in already:
                continue
            score = _score_job(keywords, job)
            if score < SCORE_FLOOR:
                continue
            scored.append((score, job))
        scored.sort(key=lambda x: x[0], reverse=True)
        scored = scored[:max_apps]

        if not scored:
            publish_event_sync("pipeline.progress", {
                "candidate_id": candidate_id,
                "step": "no_matches",
                "message": "No jobs found above the match score threshold."
            })
            return {"queued": [], "skipped": len(jobs), "error": "no_matches_above_floor"}

        publish_event_sync("pipeline.progress", {
            "candidate_id": candidate_id,
            "step": "matches_found",
            "message": f"Found {len(scored)} suitable jobs. Triggering AI matching and application pipeline..."
        })

        target_job_ids = [str(job.get("id") or job.get("job_id")) for score, job in scored]
        
        from app.database import AsyncSessionLocal
        from app.services.matching import run_matching_for_candidate
        import uuid
        
        cand_uuid = uuid.UUID(candidate_id)
        
        async with AsyncSessionLocal() as session:
            try:
                result = await run_matching_for_candidate(
                    candidate_id=cand_uuid,
                    session=session,
                    target_job_ids=target_job_ids,
                    manual_limit=max_apps
                )
                if result.get("error") == "limit_reached":
                    active = result.get("active_count", 0)
                    publish_event_sync("pipeline.progress", {
                        "candidate_id": candidate_id,
                        "step": "limit_reached",
                        "message": f"Application limit reached ({active} job(s) already pending/queued)."
                    })
                    return {"queued": [], "skipped": len(target_job_ids), "candidate_id": candidate_id, "error": "limit_reached"}
                    
                enqueued_ids = result.get("enqueued_job_ids", [])
                skipped_details = result.get("skipped", [])
                
                queued.extend(enqueued_ids)
                skipped += len(target_job_ids) - len(enqueued_ids)
                logger.info(f"[Dynamic] executed M3 pipeline for cand={candidate_id} targets={target_job_ids} result={result}")
                
                already_applied = [s for s in skipped_details if s.get("reason") == "already_applied"]
                skip_msg = ""
                if already_applied:
                    skip_msg = f", skipped {len(already_applied)} (already applied)"
                elif skipped_details:
                    skip_msg = f", skipped {len(skipped_details)}"

                publish_event_sync("pipeline.progress", {
                    "candidate_id": candidate_id,
                    "step": "done",
                    "message": f"Processed {len(enqueued_ids)} new jobs{skip_msg}."
                })
            except Exception as exc:
                logger.error(f"[Dynamic] run_matching_for_candidate failed: {exc}", exc_info=True)
                skipped += len(target_job_ids)

    return {"queued": queued, "skipped": skipped, "candidate_id": candidate_id}


@celery_app.task(
    bind=True,
    name="task:dynamic_apply",
    queue="queue:application_execution",
    max_retries=1,
)
def dynamic_apply(self, candidate_id: str, max_apps: Optional[int] = None):
    """Score every open job against the candidate's CV, queue top matches into M4.

    Args:
        candidate_id: the candidate to source for.
        max_apps: cap on number of applications (defaults to MAX_APPLICATIONS_PER_RUN env).
    """
    from app.tasks.dynamic_apply import _run
    import asyncio
    
    # Bypass Celery due to Upstash Redis limitations. Run directly in background.
    def run_in_background():
        import logging as _logging
        _log = _logging.getLogger("dynamic_apply.bg")
        _log.info(f"[BG] Starting auto-apply thread for candidate={candidate_id} max_apps={max_apps}")
        try:
            result = asyncio.run(_run(candidate_id, max_apps or MAX_APPLICATIONS_PER_RUN))
            _log.info(f"[BG] Auto-apply completed: {result}")
        except Exception as e:
            import traceback
            _log.error(f"[BG] Failed background apply for candidate={candidate_id}: {e}")
            _log.error(traceback.format_exc())
            try:
                from app.tasks.dynamic_apply import publish_event_sync
                publish_event_sync("pipeline.progress", {
                    "candidate_id": candidate_id,
                    "step": "error",
                    "message": f"Pipeline failed: {e}"
                })
            except Exception as ev_err:
                _log.error(f"[BG] Failed to broadcast error event: {ev_err}")

    import threading
    threading.Thread(target=run_in_background, daemon=True).start()
    return {"status": "started"}
