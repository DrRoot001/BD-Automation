"""Main email scan pipeline — orchestrates fetch → classify → match → store → update."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.7

# Maps email classification → application status
CLASSIFICATION_TO_STATUS = {
    "APPLIED_CONFIRMATION": "CONFIRMED",
    "INTERVIEW_R1": "INTERVIEW_R1",
    "INTERVIEW_R2": "INTERVIEW_R2",
    "ASSESSMENT": "INTERVIEW_R1",
    "REJECTED": "REJECTED",
    "OFFER": "OFFER",
}

INTERVIEW_CATEGORIES = {"INTERVIEW_R1", "INTERVIEW_R2", "ASSESSMENT"}


async def scan_candidate_inbox(candidate_id: str, db_session) -> int:
    """
    Full scan pipeline for one candidate.
    Returns number of new emails processed.
    """
    from module5.gmail.client import refresh_access_token, fetch_emails_since
    from module5.gmail.classifier import classify_email
    from module5.gmail.matcher import match_email_to_application
    from module5.gmail.extractor import extract_interview_details
    from app.services.events import publish_event
    from sqlalchemy import text

    # 1. Fetch candidate OAuth token
    result = await db_session.execute(
        text("SELECT google_refresh_token FROM candidates WHERE id = :cid"),
        {"cid": candidate_id}
    )
    row = result.fetchone()
    if not row or not row.google_refresh_token:
        logger.warning(f"[Scanner] No refresh token for candidate {candidate_id}")
        return 0

    # 2. Get last scan timestamp from Redis (default: 24h ago)
    try:
        from app.redis_client import redis_client
        last_scan_key = f"last_email_scan:{candidate_id}"
        last_scan_ts = await redis_client.get(last_scan_key)
        if last_scan_ts:
            since = datetime.fromtimestamp(float(last_scan_ts), tz=timezone.utc).replace(tzinfo=None)
        else:
            since = datetime.utcnow() - timedelta(hours=24)
    except Exception:
        since = datetime.utcnow() - timedelta(hours=24)

    # 3. Refresh access token and fetch emails
    # The refresh token is stored ENCRYPTED (Fernet) when ENCRYPTION_KEY is set.
    # It MUST be decrypted before use, otherwise Gmail returns invalid_grant in
    # production. decrypt_token() is a safe no-op for plaintext/dev values.
    from app.services.crypto import decrypt_token
    refresh_token = decrypt_token(row.google_refresh_token)
    try:
        access_token = await refresh_access_token(refresh_token)
        emails = await fetch_emails_since(access_token, since)
    except Exception as e:
        logger.error(f"[Scanner] Gmail fetch failed for {candidate_id}: {e}")
        return 0

    processed = 0
    for email in emails:
        # Skip already processed gmail IDs
        dupe = await db_session.execute(
            text("SELECT id FROM emails WHERE gmail_id = :gid"),
            {"gid": email.gmail_id}
        )
        if dupe.fetchone():
            continue

        # 4. Classify
        classification = await classify_email(email.subject, email.from_addr, email.body_text)

        # 5. Match to application
        app_id = await match_email_to_application(
            candidate_id, email.from_addr, email.subject, email.body_text, db_session
        )

        # 6. Store email record
        await db_session.execute(text("""
            INSERT INTO emails
              (id, candidate_id, application_id, gmail_id, from_addr, subject,
               body_text, classification, confidence, received_at, processed_at)
            VALUES
              (gen_random_uuid(), :cid, :app_id, :gmail_id, :from_addr, :subject,
               :body_text, :classification, :confidence, :received_at, NOW())
            ON CONFLICT (gmail_id) DO NOTHING
        """), {
            "cid": candidate_id,
            "app_id": app_id,
            "gmail_id": email.gmail_id,
            "from_addr": email.from_addr,
            "subject": email.subject,
            "body_text": email.body_text[:10000],
            "classification": classification.classification,
            "confidence": classification.confidence,
            "received_at": email.received_at,
        })

        # 7. Auto-update application status if confidence is high enough
        if (classification.confidence >= CONFIDENCE_THRESHOLD
                and classification.classification != "UNKNOWN"
                and app_id):
            new_status = CLASSIFICATION_TO_STATUS.get(classification.classification)
            if new_status:
                try:
                    import httpx as _httpx
                    api_base = os.getenv("M1_API_BASE_URL", "http://localhost:8002/api")
                    async with _httpx.AsyncClient(timeout=10.0) as client:
                        await client.patch(
                            f"{api_base}/applications/{app_id}/status",
                            json={"status": new_status, "metadata": {
                                "source": "email_classification",
                                "gmail_id": email.gmail_id,
                                "confidence": classification.confidence,
                            }}
                        )
                except Exception as e:
                    logger.error(f"[Scanner] Status update failed: {e}")

                # 8. Extract interview details if applicable
                if classification.classification in INTERVIEW_CATEGORIES:
                    try:
                        details = await extract_interview_details(email.subject, email.body_text)
                        round_num = 2 if classification.classification == "INTERVIEW_R2" else 1
                        interview_date = None
                        if details.interview_date:
                            try:
                                interview_date = datetime.fromisoformat(
                                    details.interview_date.replace("Z", "+00:00")
                                ).replace(tzinfo=None)
                            except Exception:
                                pass

                        await db_session.execute(text("""
                            INSERT INTO interviews
                              (id, application_id, round, type, scheduled_at,
                               meeting_url, interviewer_name, notes, created_at)
                            VALUES
                              (gen_random_uuid(), :app_id, :round, :itype, :scheduled_at,
                               :meeting_url, :interviewer_name, :notes, NOW())
                        """), {
                            "app_id": app_id,
                            "round": round_num,
                            "itype": details.interview_type,
                            "scheduled_at": interview_date,
                            "meeting_url": details.meeting_url,
                            "interviewer_name": details.interviewer_name,
                            "notes": details.additional_notes,
                        })

                        await publish_event("interview.detected", {
                            "application_id": app_id,
                            "interview_type": details.interview_type,
                            "scheduled_at": details.interview_date,
                            "meeting_url": details.meeting_url,
                        })
                    except Exception as e:
                        logger.error(f"[Scanner] Interview extraction failed: {e}")

                await publish_event("email.classified", {
                    "candidate_id": candidate_id,
                    "application_id": app_id,
                    "classification": classification.classification,
                    "confidence": classification.confidence,
                    "gmail_id": email.gmail_id,
                })

        processed += 1

    await db_session.commit()

    # Update last scan timestamp
    try:
        from app.redis_client import redis_client
        await redis_client.set(last_scan_key, datetime.utcnow().timestamp())
    except Exception:
        pass

    logger.info(f"[Scanner] Processed {processed} new emails for candidate {candidate_id}")
    return processed
