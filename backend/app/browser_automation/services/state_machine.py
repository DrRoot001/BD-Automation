import os
import httpx
import logging
from dotenv import load_dotenv
from typing import Dict

load_dotenv()
logger = logging.getLogger(__name__)

async def fetch_application_status(application_id: str) -> str | None:
    """Read the application's CURRENT status from M1 (best-effort).

    Returns the uppercase status string, or None if it can't be read (network
    error / missing field). Callers MUST treat None as "unknown → proceed" so a
    transient read never blocks a legitimate apply — the idempotency guard only
    aborts on a *positively confirmed* already-terminal status.
    """
    m1_api_base_url = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api")
    url = f"{m1_api_base_url}/applications/{application_id}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json() or {}
            status = data.get("status")
            return str(status).upper() if status else None
        logger.warning(f"[SM] fetch_application_status {application_id}: HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"[SM] fetch_application_status {application_id} failed (non-fatal): {e}")
    return None


async def transition_status(
    application_id: str,
    new_status: str,
    metadata: Dict = {},
    **extra_fields,          # cover_letter_url, resume_id, fit_score, etc.
) -> bool:
    m1_api_base_url = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api")
    url = f"{m1_api_base_url}/applications/{application_id}/status"

    payload = {"status": new_status, "metadata": metadata, **extra_fields}

    try:
        # Log full payload so we can trace exactly what reaches the API
        extra_log = {k: v for k, v in extra_fields.items() if v is not None}
        if extra_log:
            logger.info(f"[SM] PATCH {application_id} → {new_status} with extra: {extra_log}")

        async with httpx.AsyncClient() as client:
            response = await client.patch(url, json=payload, timeout=30)

            if response.status_code in [200, 204]:
                logger.info(f"Application {application_id} transitioned to {new_status}")
                return True
            elif response.status_code == 422:
                logger.warning(f"Invalid state transition attempted for {application_id}: {response.text}")
                return False
            else:
                logger.error(f"Failed to transition status for {application_id} to {new_status}: "
                             f"Status Code: {response.status_code}, Response: {response.text}")
                return False
    except httpx.RequestError as e:
        logger.error(f"Connection error during status transition for {application_id}: {e}")
        return False
