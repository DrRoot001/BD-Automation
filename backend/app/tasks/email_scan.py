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
        from app.database import task_session
        from module5.scanner import scan_candidate_inbox as _scan_inbox
        async with task_session() as db:
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
        from app.database import task_session
        from sqlalchemy import text
        async with task_session() as db:
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


@celery_app.task(name="task:scan_single_candidate_interviews", bind=True, max_retries=2)
def scan_single_candidate_interviews(self, candidate_id: str):
    """
    Checks the candidate's connected Gmail for emails matching interview subject keywords
    and saves them in interview_tracking.
    """
    async def _scan():
        from app.database import task_session
        from sqlalchemy import text
        from module5.gmail.client import refresh_access_token, fetch_emails_since
        from module5.gmail.matcher import match_email_to_application
        from datetime import datetime, timedelta

        async with task_session() as db:
            # 1. Fetch candidate OAuth token
            result = await db.execute(
                text("SELECT name, google_refresh_token FROM candidates WHERE id = :cid"),
                {"cid": candidate_id}
            )
            row = result.fetchone()
            if not row or not row.google_refresh_token:
                logger.warning(f"[InterviewScan] No refresh token for candidate {candidate_id}")
                return 0
                
            # Look back 7 days to cover weekends/holidays
            since = datetime.utcnow() - timedelta(days=7)

            # Decrypt the stored refresh token (Fernet) before use — safe no-op
            # for plaintext/dev. Without this, Gmail returns invalid_grant when
            # ENCRYPTION_KEY is set in production.
            from app.services.crypto import decrypt_token
            refresh_token = decrypt_token(row.google_refresh_token)
            try:
                access_token = await refresh_access_token(refresh_token)
                emails = await fetch_emails_since(access_token, since)
            except Exception as e:
                logger.error(f"[InterviewScan] Gmail fetch failed for candidate {candidate_id}: {e}")
                return 0
                
            keywords = ["interview", "phone screen", "we'd like to meet", "schedule a call", "phone interview", "schedule a meeting", "meet with us"]
            new_interviews = 0
            
            for email in emails:
                subject_lower = email.subject.lower()
                if not any(kw in subject_lower for kw in keywords):
                    continue
                    
                # Check duplicate
                dupe = await db.execute(
                    text("SELECT id FROM interview_tracking WHERE gmail_id = :gid"),
                    {"gid": email.gmail_id}
                )
                if dupe.fetchone():
                    continue
                    
                # Match to application
                app_id = await match_email_to_application(
                    candidate_id, email.from_addr, email.subject, email.body_text, db
                )
                
                # Determine interview type
                itype = "phone"
                if any(k in subject_lower for k in ["video", "zoom", "meet", "teams"]):
                    itype = "video"
                elif any(k in subject_lower for k in ["onsite", "in-person", "office"]):
                    itype = "onsite"
                elif any(k in subject_lower for k in ["assessment", "test", "hackerrank", "codility"]):
                    itype = "assessment"
                    
                # Insert
                await db.execute(text("""
                    INSERT INTO interview_tracking
                        (id, candidate_id, application_id, email_subject, email_from, received_at, interview_type, status, gmail_id, created_at)
                    VALUES
                        (gen_random_uuid(), :cid, :app_id, :subject, :from_addr, :received_at, :itype, 'PENDING', :gmail_id, NOW())
                    ON CONFLICT (gmail_id) DO NOTHING
                """), {
                    "cid": candidate_id,
                    "app_id": app_id,
                    "subject": email.subject,
                    "from_addr": email.from_addr,
                    "received_at": email.received_at,
                    "itype": itype,
                    "gmail_id": email.gmail_id
                })
                new_interviews += 1
                
            await db.commit()
            return new_interviews

    try:
        count = _run(_scan())
        logger.info("[InterviewScan] Processed %d new tracked interviews for candidate %s", count, candidate_id)
        return {"tracked_interviews": count, "candidate_id": candidate_id}
    except Exception as exc:
        logger.error("[InterviewScan] Failed for candidate %s: %s", candidate_id, exc)
        raise self.retry(exc=exc, countdown=60)


@celery_app.task(name="task:scan_interviews", bind=True)
def scan_interviews(self):
    """
    Poll connected Gmail accounts for candidate interviews (runs every 30 minutes).
    """
    async def _fetch_connected_candidates():
        from app.database import task_session
        from sqlalchemy import text
        async with task_session() as db:
            result = await db.execute(
                text("SELECT id FROM candidates WHERE google_refresh_token IS NOT NULL")
            )
            return [str(row[0]) for row in result.fetchall()]

    try:
        candidate_ids = _run(_fetch_connected_candidates())
        logger.info("[InterviewScan] Dispatching interview scan for %d candidates", len(candidate_ids))
        for cid in candidate_ids:
            scan_single_candidate_interviews.delay(cid)
        return {"status": "dispatched", "candidate_count": len(candidate_ids)}
    except Exception as exc:
        logger.error(f"[InterviewScan] Dispatch failed: {exc}")
        raise self.retry(exc=exc, countdown=60)


@celery_app.task(name="task:refresh_analytics", bind=True)
def refresh_analytics(self):
    """
    Recompute and cache dashboard analytics every hour.
    (Currently a no-op — dashboard endpoints query live; add Redis caching here if needed.)
    """
    logger.info("[Analytics] refresh_analytics task called — live queries in use")
    return {"status": "ok"}
