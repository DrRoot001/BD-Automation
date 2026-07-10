
VALID_TRANSITIONS: dict[str, list[str]] = {
    "FOUND":               ["ANALYZED", "QUEUED", "FAILED", "BLOCKED", "WITHDRAWN"],
    "ANALYZED":            ["MATCHED", "APPLICATION_STARTED", "FAILED", "WITHDRAWN"],
    "MATCHED":             ["RESUME_UPDATED", "APPLICATION_STARTED", "QUEUED", "FAILED", "WITHDRAWN"],
    "RESUME_UPDATED":      ["COVER_LETTER_CREATED", "FORM_COMPLETED", "APPLICATION_STARTED", "QUEUED", "FAILED", "WITHDRAWN"],
    "COVER_LETTER_CREATED":["QUEUED", "FAILED", "WITHDRAWN"],
    "QUEUED":              ["APPLICATION_STARTED", "FORM_COMPLETED", "SUBMITTED", "FAILED", "ANALYZED", "BLOCKED", "WITHDRAWN"],
    "APPLICATION_STARTED": ["FORM_COMPLETED", "QUEUED", "ANALYZED", "SUBMITTED", "FAILED", "BLOCKED", "WITHDRAWN"],
    "FORM_COMPLETED":      ["SUBMITTED", "QUEUED", "FAILED", "WITHDRAWN"],
    "SUBMITTED":           ["CONFIRMED", "REJECTED", "GHOSTED"],
    "CONFIRMED":           ["INTERVIEW_R1", "REJECTED", "WITHDRAWN"],
    "INTERVIEW_R1":        ["INTERVIEW_R2", "REJECTED", "WITHDRAWN", "OFFER"],
    "INTERVIEW_R2":        ["OFFER", "REJECTED", "WITHDRAWN"],
    # Terminal or pseudo-terminal states
    # SUBMITTED is allowed from FAILED: a late Celery retry can complete the
    # real submission AFTER retry-exhaustion already marked the row FAILED.
    # The ATS has the application at that point — refusing the write corrupts
    # our record and re-exposes the job to matching (double-apply risk).
    "FAILED":              ["QUEUED", "SUBMITTED", "WITHDRAWN"],
    "BLOCKED":             ["QUEUED", "WITHDRAWN"],   # Blocked apps can be retried
    "REJECTED":            [],
    "OFFER":               [],
    "GHOSTED":             ["QUEUED", "WITHDRAWN"],   # Can retry if desired
    "WITHDRAWN":           ["QUEUED"],   # Cancelled apps can be re-queued if it was a mistake
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
from datetime import datetime, timedelta, timezone

from app.models.application import Application
from app.models.application_history import ApplicationHistory
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    from sqlalchemy import text

    now = datetime.now(timezone.utc)

    # 1. Fetch stuck apps and their latest history timestamp in a single grouped query
    # paused = 0 filter: an operator-paused application is intentionally held and
    # must never be force-failed by the watchdog. It is also excluded from the
    # active-count gate, so it does not need recovering to unblock the pipeline.
    stmt = text("""
        SELECT a.id, a.status, COALESCE(MAX(h.created_at), a.created_at) as last_updated
        FROM applications a
        LEFT JOIN application_history h ON h.application_id = a.id
        WHERE a.status IN ('FOUND', 'MATCHED', 'RESUME_UPDATED', 'COVER_LETTER_CREATED',
                           'QUEUED', 'APPLICATION_STARTED', 'FORM_COMPLETED')
          AND a.paused = 0
        GROUP BY a.id, a.status, a.created_at
    """)

    result = await session.execute(stmt)
    rows = result.fetchall()

    stuck_ids_and_statuses = []

    for row in rows:
        app_id, status, last_updated = row

        if last_updated.tzinfo is None:
            last_updated = last_updated.replace(tzinfo=timezone.utc)

        if status == "FOUND":
            # FOUND means the orchestration pipeline is running. 30 min is generous
            # for LLM scoring + tailoring; if it's still FOUND after that, the pipeline died.
            threshold_min = 30
        elif status in ("MATCHED", "RESUME_UPDATED", "COVER_LETTER_CREATED"):
            # Tailoring phase (resume tailoring + cover letter + screening QA). These
            # are transient hand-off states; if an app lingers here it means the
            # tailoring pipeline died mid-way. Before this was added, such rows were
            # never recovered and permanently counted toward the candidate's active
            # cap (get_active_application_count), silently blocking all new jobs.
            threshold_min = 20
        elif status == "QUEUED":
            # Must match get_active_application_count's 15-min stale threshold exactly.
            # The watchdog runs before the limit check inside run_matching_for_candidate,
            # so any QUEUED app ≥ 15 min is killed here before active_count is computed.
            # This prevents stale QUEUED jobs from appearing as "limit reached".
            threshold_min = 15
        else:
            # APPLICATION_STARTED / FORM_COMPLETED: reduce to 20 min so stuck browser
            # sessions don't block worker slots for a full 30 minutes. The 30 min threshold
            # was causing live workers to appear "full" even though the browser had crashed.
            threshold_min = 20

        time_limit = now - timedelta(minutes=threshold_min)

        if last_updated < time_limit:
            stuck_ids_and_statuses.append((app_id, status, last_updated))

    if not stuck_ids_and_statuses:
        return

    stuck_ids = [row[0] for row in stuck_ids_and_statuses]

    # A FORM_COMPLETED transition tagged submit_confirmed/submit_fired means the
    # submit click already went to the ATS — the pipeline died between the click
    # and the SUBMITTED persist (evidence upload, resume-id lookup, etc.).
    # Failing those rows records a real submission as FAILED, and a later retry
    # would double-submit. Promote them to SUBMITTED instead.
    fc_ids = [row[0] for row in stuck_ids_and_statuses if row[1] == "FORM_COMPLETED"]
    submit_fired_ids = set()
    if fc_ids:
        fc_hist_stmt = select(ApplicationHistory).where(
            ApplicationHistory.application_id.in_(fc_ids),
            ApplicationHistory.to_status == "FORM_COMPLETED",
        )
        for h in (await session.execute(fc_hist_stmt)).scalars().all():
            meta = h.meta_data or {}
            if meta.get("submit_confirmed") or meta.get("submit_fired"):
                submit_fired_ids.add(h.application_id)

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

        if app_id in submit_fired_ids:
            logger.warning(
                f"[Watchdog] Promoting stuck application {app.id} to SUBMITTED "
                f"(submit already fired; pipeline died before persisting, last_updated={last_updated})"
            )
            app.status = "SUBMITTED"
            app.failure_reason = None
            app.error_message = None
            if getattr(app, "submitted_at", None) is None:
                app.submitted_at = last_updated
            new_status = "SUBMITTED"
            history = ApplicationHistory(
                application_id=app.id,
                from_status=old_status,
                to_status="SUBMITTED",
                meta_data={"info": (
                    "Recovered by watchdog: submit had already fired but the "
                    f"pipeline died before persisting (stuck in {old_status} "
                    f"since {last_updated}); evidence capture was interrupted"
                )}
            )
        else:
            logger.warning(f"[Watchdog] Killing stuck application {app.id} (status={old_status}, last_updated={last_updated})")
            app.status = "FAILED"
            app.failure_reason = "INFRA_ERROR"
            app.error_message = f"watchdog: stuck in {old_status} since {last_updated} — worker crashed or pipeline timed out"
            new_status = "FAILED"
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
                to_status=new_status
            )
        except Exception as ev_err:
            logger.error(f"[Watchdog] failed to publish status changed event for {app.id}: {ev_err}")

    if stuck_count > 0:
        await session.commit()
        logger.info(f"[Watchdog] Successfully recovered {stuck_count} stuck applications.")


async def fail_applications_in_window_async(session: AsyncSession, hours: int = 5) -> int:
    """Admin action: force-fail all non-terminal applications created in the last N hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    stmt = select(Application).where(
        Application.status.in_(["FOUND", "QUEUED", "APPLICATION_STARTED", "FORM_COMPLETED"]),
        Application.created_at >= cutoff
    )
    result = await session.execute(stmt)
    apps = result.scalars().all()

    count = 0
    for app in apps:
        old_status = app.status
        logger.warning(f"[AdminFail] Force-failing application {app.id} (status={old_status}, created={app.created_at})")
        app.status = "FAILED"
        app.failure_reason = "INFRA_ERROR"
        app.error_message = f"admin: force-failed via admin endpoint (was {old_status}, created within {hours}h window)"

        history = ApplicationHistory(
            application_id=app.id,
            from_status=old_status,
            to_status="FAILED",
            meta_data={"info": f"Force-failed by admin (created_at={app.created_at}, was in {old_status})"}
        )
        session.add(history)
        count += 1

        try:
            from app.tasks.browser_automation import publish_status_changed
            await publish_status_changed(
                application_id=str(app.id),
                from_status=old_status,
                to_status="FAILED"
            )
        except Exception as ev_err:
            logger.error(f"[AdminFail] Failed to publish event for {app.id}: {ev_err}")

    if count > 0:
        await session.commit()
        logger.info(f"[AdminFail] Force-failed {count} applications from the last {hours} hours.")

    return count
