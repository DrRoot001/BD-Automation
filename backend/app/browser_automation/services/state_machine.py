import os
import asyncio
import httpx
import logging
from dotenv import load_dotenv
from typing import Dict, Optional

load_dotenv()
logger = logging.getLogger(__name__)


class StatusTransitionError(RuntimeError):
    """Raised when a transition the caller declared critical could not be
    persisted (connection error or server error). Lets the executor avoid
    reporting a SUBMITTED application whose DB row never actually changed."""


async def transition_status(
    application_id: str,
    new_status: str,
    metadata: Optional[Dict] = None,
    *,
    raise_on_fail: bool = False,
    _max_attempts: int = 3,
    **extra_fields,          # cover_letter_url, resume_id, fit_score, etc.
) -> bool:
    # Avoid the mutable-default-arg footgun.
    metadata = dict(metadata or {})
    m1_api_base_url = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api")
    url = f"{m1_api_base_url}/applications/{application_id}/status"

    payload = {"status": new_status, "metadata": metadata, **extra_fields}

    extra_log = {k: v for k, v in extra_fields.items() if v is not None}
    if extra_log:
        logger.info(f"[SM] PATCH {application_id} → {new_status} with extra: {extra_log}")

    last_problem = ""
    # Retry transient failures (connection drop / 5xx). A 422 (invalid transition)
    # is NOT transient — the server intentionally rejected it; don't retry.
    for attempt in range(1, _max_attempts + 1):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.patch(url, json=payload, timeout=30)

            if response.status_code in [200, 204]:
                logger.info(f"Application {application_id} transitioned to {new_status}")
                return True
            elif response.status_code == 422:
                logger.warning(f"Invalid state transition attempted for {application_id}: {response.text}")
                if raise_on_fail:
                    raise StatusTransitionError(
                        f"{new_status} rejected as invalid transition for {application_id}: {response.text[:200]}"
                    )
                return False
            else:
                last_problem = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.error(f"Failed to transition {application_id} → {new_status}: {last_problem}")
        except httpx.RequestError as e:
            last_problem = f"connection error: {e}"
            logger.error(f"Connection error during status transition for {application_id}: {e}")

        if attempt < _max_attempts:
            await asyncio.sleep(min(2 * attempt, 5))

    if raise_on_fail:
        raise StatusTransitionError(
            f"Could not persist {new_status} for {application_id} after {_max_attempts} attempts: {last_problem}"
        )
    return False
