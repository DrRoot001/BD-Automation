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
