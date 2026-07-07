"""Shared helpers for account-walled adapters to recover from stale sessions.

When a restored session (Redis blob or `backend/data/sessions/<platform>.json`)
has expired, the ATS shows a login wall even though we "have" a session. For
adapters that can log in programmatically (Dice/Glassdoor/Workday/ZipRecruiter)
this lets them wipe the stale file so a fresh `_perform_login` starts clean
rather than fighting corrupt cookies. For storage_state-only adapters (LinkedIn)
it clears the stale file so the operator's next re-seed isn't merged with dead
cookies, and lets us surface a clear, actionable error.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def session_file_path(platform: str) -> Path:
    """Resolve the on-disk session path for a platform.

    Mirrors context_manager's precedence: the `<PLATFORM>_STORAGE_STATE` env
    override wins, else `backend/data/sessions/<platform>.json`.
    """
    override = os.getenv(f"{platform.upper()}_STORAGE_STATE", "").strip()
    if override:
        return Path(override)
    backend_dir = Path(__file__).resolve().parents[3]
    return backend_dir / "data" / "sessions" / f"{platform}.json"


def invalidate_session_file(platform: str) -> bool:
    """Delete the persisted session file for `platform` if it exists.

    Returns True if a file was removed. Safe to call when no file exists
    (returns False) and never raises on a missing/locked file.
    """
    try:
        path = session_file_path(platform)
        if path.is_file():
            path.unlink()
            logger.warning(
                f"[{platform}] Invalidated stale session file {path} — "
                "a fresh login (or operator re-seed) is required."
            )
            return True
    except Exception as exc:
        logger.debug(f"[{platform}] session-file invalidation skipped: {exc}")
    return False


async def load_candidate_credentials(candidate_id) -> dict:
    """Load a candidate's portal login credentials from their DB profile.

    Returns ``{"login_email": str, "password": str, "gmail": str}`` — empty
    strings when a field is absent, the candidate is unknown, or the DB lookup
    fails (never raises; callers degrade to env-var credentials).

    `login_email` is the candidate's Gmail address when present (portal accounts
    for our candidates are provisioned against their Gmail), falling back to the
    primary account email. The password is the candidate's stored portal
    password.

    SECURITY: the returned values — the password especially — are handed ONLY to
    an adapter's login method via ``set_candidate_credentials``. They are never
    written into ``candidate_profile`` (which is serialized into LLM prompts and
    logs) or logged directly. Mirrors code_fetcher._load_refresh_token's
    Celery-safe NullPool session usage.
    """
    out = {"login_email": "", "password": "", "gmail": ""}
    if not candidate_id:
        return out
    try:
        import uuid

        from sqlalchemy import select
        from app.database import task_session
        from app.models.candidate import Candidate

        cid = candidate_id
        if isinstance(cid, str):
            try:
                cid = uuid.UUID(cid)
            except ValueError:
                pass
        async with task_session() as s:
            row = (
                await s.execute(select(Candidate).where(Candidate.id == cid))
            ).scalar_one_or_none()
        if not row:
            return out
        gmail = (getattr(row, "gmail", "") or "").strip()
        email = (getattr(row, "email", "") or "").strip()
        password = (getattr(row, "password", "") or "").strip()
        out["gmail"] = gmail
        out["login_email"] = gmail or email
        out["password"] = password
    except Exception as exc:
        logger.warning(f"[credentials] could not load candidate credentials: {exc}")
    return out
