"""Screenshot capture + Supabase upload.

Final-state screenshots are stored in TWO places:
  1. Local disk under ``SCREENSHOT_LOCAL_DIR`` (default ``./screenshots``) for
     immediate debugging.
  2. Supabase Storage bucket named ``screenshots`` so they are reachable from
     the dashboard and persisted across machines. The returned URL is the
     PUBLIC Supabase URL when upload succeeds; otherwise the local path is
     returned as a fallback so the application record still has SOMETHING to
     reference.

Auth: tries ``SUPABASE_ANON_KEY`` first, then ``NEXT_PUBLIC_SUPABASE_ANON_KEY``,
then the same frontend ``.env`` fallback chain ``executor.py`` already uses.
The bucket must exist + be configured for public reads (or anon-key writes).
"""
import os
import datetime
import logging
from typing import Optional

import httpx
from playwright.async_api import Page
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Supabase helpers (duplicated from executor.py to avoid an import cycle)
# ──────────────────────────────────────────────────────────────────────────

def _read_env_file(path: str, key: str) -> str:
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(f"{key}="):
                    return line.strip().split("=", 1)[1]
    except Exception:
        pass
    return ""


def _supabase_anon_key() -> str:
    return (
        os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY")
        or _read_env_file("frontend/.env.local", "NEXT_PUBLIC_SUPABASE_ANON_KEY")
        or _read_env_file("frontend/.env", "NEXT_PUBLIC_SUPABASE_ANON_KEY")
        or ""
    )


def _supabase_write_key() -> str:
    """Key used for Storage WRITES. Prefer the service-role key — uploads with the
    anon key are rejected by row-level-security ("new row violates RLS policy"),
    which is exactly why screenshots failed to persist to the dashboard. The
    service-role key is a server-side secret that bypasses RLS; fall back to the
    anon key only if it isn't configured."""
    return (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_SERVICE_KEY")
        or _supabase_anon_key()
    )


def _supabase_project_url() -> Optional[str]:
    """Derive https://<project>.supabase.co from SUPABASE_URL or DATABASE_URL."""
    explicit = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
    if explicit:
        return explicit.rstrip("/")
    # Fallback: parse the project ref from DATABASE_URL (postgresql://...@aws-...-postgres.<project>.supabase.com)
    db = os.getenv("DATABASE_URL", "")
    # Pattern observed in this repo: postgres.<project_ref>:password@aws-... .pooler.supabase.com
    import re
    m = re.search(r"postgres\.([a-z0-9]+):", db)
    if m:
        return f"https://{m.group(1)}.supabase.co"
    return None


async def _upload_to_supabase(local_path: str, bucket: str, remote_name: str) -> Optional[str]:
    """PUT the file to Supabase storage. Returns the public URL on success."""
    project_url = _supabase_project_url()
    write_key = _supabase_write_key()
    if not project_url or not write_key:
        logger.warning(
            f"[Screenshot] Supabase upload skipped — "
            f"project_url={bool(project_url)} write_key={bool(write_key)}"
        )
        return None

    upload_url = f"{project_url}/storage/v1/object/{bucket}/{remote_name}"
    headers = {
        "Authorization": f"Bearer {write_key}",
        "apikey": write_key,
        "Content-Type": "image/png",
        "x-upsert": "true",  # overwrite if exists
    }
    try:
        with open(local_path, "rb") as fh:
            body = fh.read()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(upload_url, headers=headers, content=body)
        if resp.status_code in (200, 201):
            public_url = f"{project_url}/storage/v1/object/public/{bucket}/{remote_name}"
            logger.info(f"[Screenshot] Uploaded to Supabase: {public_url}")
            return public_url
        logger.warning(
            f"[Screenshot] Supabase upload HTTP {resp.status_code}: {resp.text[:200]}"
        )
    except Exception as exc:
        logger.warning(f"[Screenshot] Supabase upload failed (non-fatal): {exc}")
    return None


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────

async def capture_and_store_screenshot(page: Page, application_id: str) -> str:
    """Capture a full-page screenshot, save locally AND upload to Supabase.

    Returns the SUPABASE URL when upload succeeds, falling back to the local
    path so the application record always has a reachable reference. Local
    file is preserved for debugging regardless of upload outcome.
    """
    local_dir = os.getenv("SCREENSHOT_LOCAL_DIR", "./screenshots")
    os.makedirs(local_dir, exist_ok=True)

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{application_id}_{timestamp}.png"
    full_path = os.path.join(local_dir, filename)

    await page.screenshot(path=full_path, full_page=True)
    logger.info(f"[Screenshot] Saved locally: {full_path}")

    # Upload to Supabase bucket "screenshots" (configurable via env)
    bucket = os.getenv("SUPABASE_SCREENSHOT_BUCKET", "screenshots")
    remote_name = f"{application_id}/{timestamp}.png"
    public_url = await _upload_to_supabase(full_path, bucket, remote_name)
    if public_url:
        try:
            if os.path.exists(full_path):
                os.remove(full_path)
                logger.info(f"[Screenshot] Cleaned up local screenshot file: {full_path}")
        except Exception as e:
            logger.warning(f"[Screenshot] Failed to delete local screenshot (non-fatal): {e}")
    return public_url or full_path
