import asyncio
import os
import json
import datetime
import httpx
import logging
from celery.exceptions import Retry
import redis.asyncio as aioredis

from app.celery_app import celery_app
from app.services.events import publish_event
from app.browser_automation.services.models import ApplicationPackage, ApplicationResult
from app.browser_automation.services.executor import ApplicationExecutor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

async def publish_event(event_name: str, payload: dict) -> None:
    """
    Publish payload to Redis.
    To be fully integrated with both Module 4 specifications and the main backend,
    this publishes to both f"event:{clean_name}" and f"events:{clean_name}" channels.
    """
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    kwargs = {}
    if "rediss://" in redis_url:
        kwargs["ssl_cert_reqs"] = "none"
    redis_client = aioredis.from_url(redis_url, **kwargs)
    try:
        clean_name = event_name
        if clean_name.startswith("event:"):
            clean_name = clean_name[len("event:"):]
        elif clean_name.startswith("events:"):
            clean_name = clean_name[len("events:"):]

        # 1. Publish to the event:clean_name channel (plain payload)
        await redis_client.publish(f"event:{clean_name}", json.dumps(payload))

        # 2. Publish to the events:clean_name channel (wrapped payload)
        wrapped = {
            "event": clean_name,
            "data": payload,
            "timestamp": datetime.datetime.utcnow().isoformat()
        }
        await redis_client.publish(f"events:{clean_name}", json.dumps(wrapped))
    finally:
        await redis_client.aclose()


# ---------------------------------------------------------------------------
# Public publish helpers
# ---------------------------------------------------------------------------

async def publish_application_submitted(
    application_id: str,
    screenshot_url: str,
    confirmation_text: str,
) -> None:
    """Publish an *application.submitted* event."""
    await publish_event(
        "event:application.submitted",
        {
            "application_id":    application_id,
            "screenshot_url":    screenshot_url,
            "confirmation_text": confirmation_text,
        },
    )


async def publish_application_failed(
    application_id: str,
    error: str,
    retry_eligible: bool,
) -> None:
    """Publish an *application.failed* event."""
    await publish_event(
        "event:application.failed",
        {
            "application_id": application_id,
            "error":          error,
            "retry_eligible": retry_eligible,
        },
    )


async def publish_status_changed(
    application_id: str,
    from_status: str,
    to_status: str,
) -> None:
    """Publish an *application.status_changed* event with an ISO-8601 timestamp."""
    await publish_event(
        "event:application.status_changed",
        {
            "application_id": application_id,
            "from_status":    from_status,
            "to_status":      to_status,
            "timestamp":      datetime.datetime.utcnow().isoformat(),
        },
    )


async def hydrate_and_execute(package_dict: dict, retry_count: int) -> ApplicationResult:
    api_base = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api")
    app_id = package_dict["application_id"]

    async with httpx.AsyncClient(timeout=30) as client:
        await publish_event("pipeline.progress", {
            "application_id": str(app_id),
            "step": "preparing_package",
            "message": "Preparing tailored resume and cover letter..."
        })
        app_resp = await client.get(f"{api_base}/applications/{app_id}")
        app_resp.raise_for_status()
        app_data = app_resp.json()

        cand_id = app_data["candidate_id"]
        job_id  = app_data["job_id"]

        cand_resp = await client.get(f"{api_base}/candidates/{cand_id}")
        cand_resp.raise_for_status()
        cand_data = cand_resp.json()

        job_resp = await client.get(f"{api_base}/jobs/{job_id}")
        job_resp.raise_for_status()
        job_data = job_resp.json()

        # ── Resolve resume_url ──
        # Priority: event payload → application record → latest tailored resume in DB
        resume_url = package_dict.get("resume_url", "") or app_data.get("resume_id", "")
        from uuid import UUID
        is_uuid = False
        try:
            UUID(str(resume_url))
            is_uuid = True
        except (ValueError, AttributeError):
            pass

        if is_uuid or not resume_url or (
            not resume_url.startswith(("http://", "https://")) and not os.path.isfile(str(resume_url))
        ):
            res_resp = await client.get(f"{api_base}/resumes/{cand_id}")
            if res_resp.status_code == 200:
                resumes = res_resp.json()
                matching = None
                if is_uuid:
                    matching = next((r for r in resumes if r["id"] == resume_url), None)
                if not matching and resumes:
                    # Prefer the latest tailored resume over the base resume
                    resumes.sort(
                        key=lambda r: (not r.get("is_base"), r.get("version", 0)),
                        reverse=True,
                    )
                    matching = resumes[0]
                if matching:
                    resume_url = matching["file_url"]

        # ── Resolve cover_letter_url ──
        # Priority: event payload → application record
        cover_letter_url = (
            package_dict.get("cover_letter_url")
            or app_data.get("cover_letter_url")
        )

    # ── Build enriched candidate_profile ──
    # The form filler expects all of these keys.  Derive what we can from the DB;
    # use safe defaults for the rest so the pipeline never crashes on a missing key.
    full_name = cand_data.get("name") or ""
    name_parts = full_name.strip().split()
    first_name = name_parts[0] if name_parts else ""
    last_name  = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    # Map DB work_auth codes to human-readable Yes/No answers
    work_auth = (cand_data.get("work_auth") or "us_authorized").lower()
    is_authorized = work_auth in ("us_authorized", "citizen", "green_card", "visa", "ead")
    work_auth_answer  = "Yes" if is_authorized else "No"
    sponsorship_answer = "No" if is_authorized else "Yes"

    candidate_profile = {
        # Identity
        "name":       full_name,
        "first_name": first_name,
        "last_name":  last_name,
        "email":      cand_data.get("email") or "",
        "phone":      cand_data.get("phone") or "",
        # Location
        "location": cand_data.get("location") or "",
        # Online presence
        "linkedin_url": cand_data.get("linkedin_url") or "",
        "website":      cand_data.get("website") or "",
        # Professional
        "experience_years":  str(cand_data.get("years_exp") or ""),
        "tech_stack":        ", ".join(cand_data.get("tech_stack") or []),
        "current_company":   cand_data.get("current_company") or "",
        "current_title":     cand_data.get("current_title") or "",
        "education":         cand_data.get("education") or "",
        "salary_expectation": cand_data.get("salary_expectation") or "",
        # Work authorization (used by screening question answers in filler)
        "work_authorization": work_auth_answer,
        "sponsorship":        sponsorship_answer,
        # Application defaults — these satisfy boilerplate checkbox questions
        "agree_terms":        "Yes",
        "willing_to_relocate": "Yes",
        "background_check":   "Yes",
        "drug_test":          "Yes",
        "referral_source":    "Online",
        "start_date":         "Immediately",
    }

    full_package_dict = {
        "application_id":   app_id,
        "candidate_id":     cand_id,
        "job_id":           job_id,
        "job_title":        job_data.get("title") or "",
        "job_description":  job_data.get("description") or "",
        "job_url":          job_data.get("source_url") or "",
        "platform":         job_data.get("source") or "",
        "ats_type":         job_data.get("ats_type") or "",
        "company":          job_data.get("company") or "",
        "candidate_profile": candidate_profile,
        "resume_url":        resume_url,
        "cover_letter_url":  cover_letter_url,
        "screening_answers": package_dict.get("screening_answers"),
    }
    
    package = ApplicationPackage(**full_package_dict)
    
    logger.info(f"Loaded executor payload. Hydration complete.")
    await publish_event("pipeline.progress", {
        "application_id": str(package.application_id),
        "step": "form_filling",
        "message": f"Initializing browser to fill application at {package.platform.capitalize()}..."
    })
    
    executor = ApplicationExecutor()
    result = await executor.execute(package, retry_count=retry_count)
    
    if result.status == "SUBMITTED":
        await publish_event("pipeline.progress", {
            "application_id": str(package.application_id),
            "step": "submitting",
            "message": "Application successfully submitted with evidence!"
        })
        await publish_application_submitted(
            application_id=result.application_id,
            screenshot_url=result.screenshot_url or "",
            confirmation_text=result.confirmation_text or ""
        )
    else:
        await publish_event("pipeline.progress", {
            "application_id": str(package.application_id),
            "step": "failed",
            "message": f"Automation ended with status: {result.status}"
        })
        
    return result

@celery_app.task(
    bind=True,
    name="task:execute_application",
    queue="queue:application_execution",
    max_retries=3,
    default_retry_delay=300,
    retry_backoff=True,
    retry_backoff_max=1800,
    acks_late=True,
)
def execute_application(self, package_dict: dict):
    """
    Main application execution task.
    """
    try:
        result: ApplicationResult = asyncio.run(
            hydrate_and_execute(package_dict, retry_count=self.request.retries)
        )
        if result.status == "RATE_LIMITED":
            raise self.retry(countdown=600)
        elif result.status == "BLOCKED":
            asyncio.run(publish_application_failed(
                application_id=package_dict.get("application_id", ""),
                error=result.error_message or "Blocked by bot detection",
                retry_eligible=False
            ))
        elif result.status in ["FAILED", "CAPTCHA_FAILED"]:
            logger.info(f"Automation execution completed: {result.status}")
            raise Exception(f"Execution failed: {result.error_message}")
            
        return result.dict()
    except Retry:
        raise
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            asyncio.run(publish_application_failed(
                application_id=package_dict.get("application_id", ""),
                error=str(exc),
                retry_eligible=False
            ))
        raise self.retry(exc=exc)


@celery_app.task(
    name="task:retry_failed_application",
    queue="queue:application_execution",
)
def retry_failed_application(package_dict: dict):
    """
    Schedules a delayed re-execution of a failed application package.

    Dispatches execute_application with a 5-minute countdown so that
    transient failures have time to recover before the next attempt.
    """
    execute_application.apply_async(args=[package_dict], countdown=300)


@celery_app.task(
    name="task:verify_submission",
    queue="queue:application_execution",
)
def verify_submission(application_id: str):
    """
    Calls the Module 1 API to retrieve the current status of an application
    and returns the JSON response for logging / downstream processing.
    """
    api_base = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api")
    with httpx.Client() as client:
        resp = client.get(f"{api_base}/applications/{application_id}")
        return resp.json()
