"""Gmail API client — fetches emails using stored OAuth tokens."""
from __future__ import annotations

import base64
import os
import logging
from datetime import datetime, timezone
from typing import List, Optional
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"


@dataclass
class RawEmail:
    gmail_id: str
    from_addr: str
    subject: str
    body_text: str
    received_at: datetime


async def refresh_access_token(refresh_token: str) -> str:
    """Exchange a stored refresh token for a fresh access token."""
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    async with httpx.AsyncClient() as client:
        resp = await client.post(GMAIL_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        })
        resp.raise_for_status()
        return resp.json()["access_token"]


def _decode_body(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        text = _decode_body(part)
        if text:
            return text
    return ""


async def fetch_emails_since(
    access_token: str,
    since: datetime,
    max_results: int = 50,
) -> List[RawEmail]:
    """
    Fetch emails from the Gmail inbox received after `since`.
    Excludes sent mail and drafts.
    """
    since_epoch = int(since.timestamp())
    query = f"in:inbox after:{since_epoch}"

    headers = {"Authorization": f"Bearer {access_token}"}
    emails: List[RawEmail] = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        # List message IDs
        list_resp = await client.get(
            f"{GMAIL_API_BASE}/users/me/messages",
            headers=headers,
            params={"q": query, "maxResults": max_results},
        )
        if list_resp.status_code != 200:
            logger.error(f"Gmail list failed: {list_resp.status_code} {list_resp.text[:200]}")
            return []

        messages = list_resp.json().get("messages", [])
        logger.info(f"[Gmail] Found {len(messages)} messages since {since.isoformat()}")

        for msg_ref in messages:
            msg_id = msg_ref["id"]
            detail_resp = await client.get(
                f"{GMAIL_API_BASE}/users/me/messages/{msg_id}",
                headers=headers,
                params={"format": "full"},
            )
            if detail_resp.status_code != 200:
                continue

            msg = detail_resp.json()
            headers_list = msg.get("payload", {}).get("headers", [])
            header_map = {h["name"].lower(): h["value"] for h in headers_list}

            subject = header_map.get("subject", "(no subject)")
            from_addr = header_map.get("from", "")
            date_str = header_map.get("date", "")

            # Parse received timestamp
            try:
                from email.utils import parsedate_to_datetime
                received_at = parsedate_to_datetime(date_str).astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                received_at = datetime.utcnow()

            body_text = _decode_body(msg.get("payload", {}))

            emails.append(RawEmail(
                gmail_id=msg_id,
                from_addr=from_addr,
                subject=subject,
                body_text=body_text[:8000],  # cap at 8k chars for LLM
                received_at=received_at,
            ))

    return emails
