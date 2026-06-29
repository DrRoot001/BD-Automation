import asyncio
import os
import json
import datetime
from typing import Optional
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
        import ssl as _ssl
        kwargs["ssl_cert_reqs"] = _ssl.CERT_NONE
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
    failure_reason: Optional[str] = None,
) -> None:
    """Publish an *application.failed* event and transition DB status."""
    await publish_event(
        "event:application.failed",
        {
            "application_id": application_id,
            "error":          error,
            "retry_eligible": retry_eligible,
            "failure_reason": failure_reason,
        },
    )
    
    # Transition the status in the database to FAILED or BLOCKED
    from app.browser_automation.services.state_machine import transition_status
    status_to_set = "BLOCKED" if failure_reason == "BOT_DETECTED" or "blocked" in error.lower() else "FAILED"
    
    metadata = {
        "error_message": error,
        "retry_eligible": retry_eligible
    }
    await transition_status(
        application_id=application_id,
        new_status=status_to_set,
        metadata=metadata,
        failure_reason=failure_reason
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
    cand_id = package_dict.get("candidate_id")

    async with httpx.AsyncClient(timeout=30) as client:
        await publish_event("pipeline.progress", {
            "application_id": str(app_id),
            "candidate_id": str(cand_id) if cand_id else None,
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

    # Map DB work_auth codes to the *category* vocabulary used by dropdown-style
    # work-authorization fields (e.g. Dice: US Citizen / Green Card Holder / H1B /
    # OPT / TN Visa / Other). The Yes/No answer above is for "Are you authorized?"
    # style questions; this category answer is for "What is your work auth?" lists.
    _WORK_AUTH_TYPE_MAP = {
        "citizen":       "US Citizen",
        "us_authorized": "US Citizen",
        "green_card":    "Green Card Holder",
        "visa":          "H1B",
        "ead":           "OPT",
    }
    work_auth_type = _WORK_AUTH_TYPE_MAP.get(work_auth, "Other")

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
        # Category answer for dropdown-style work-auth fields (Dice, etc.)
        "work_authorization_type": work_auth_type,
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
        "candidate_id": str(package.candidate_id),
        "step": "form_filling",
        "message": f"Initializing browser to fill application at {package.platform.capitalize()}..."
    })
    
    executor = ApplicationExecutor()
    result = await executor.execute(package, retry_count=retry_count)
    
    if result.status == "SUBMITTED":
        await publish_event("pipeline.progress", {
            "application_id": str(package.application_id),
            "candidate_id": str(package.candidate_id),
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
            "candidate_id": str(package.candidate_id),
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
                retry_eligible=False,
                failure_reason="BOT_DETECTED"
            ))
        elif result.status in ["FAILED", "CAPTCHA_FAILED"]:
            logger.info(f"Automation execution completed: {result.status}")
            if result.error_message and "ROBOTS_BLOCKED" in result.error_message:
                asyncio.run(publish_application_failed(
                    application_id=package_dict.get("application_id", ""),
                    error="BLOCKED: Navigation disallowed by robots.txt policy",
                    retry_eligible=False,
                    failure_reason="ROBOTS_BLOCKED"
                ))
                return result.dict()
            if result.error_message and ("JOB_EXPIRED" in result.error_message or "job no longer exists" in result.error_message.lower()):
                asyncio.run(publish_application_failed(
                    application_id=package_dict.get("application_id", ""),
                    error=result.error_message,
                    retry_eligible=False,
                    failure_reason="JOB_EXPIRED"
                ))
                return result.dict()
            raise Exception(f"Execution failed: {result.error_message}")
            
        return result.dict()
    except Retry:
        raise
    except Exception as exc:
        err_msg = str(exc)
        if "ROBOTS_BLOCKED" in err_msg or "robots.txt" in err_msg.lower():
            asyncio.run(publish_application_failed(
                application_id=package_dict.get("application_id", ""),
                error="BLOCKED: Navigation disallowed by robots.txt policy",
                retry_eligible=False,
                failure_reason="ROBOTS_BLOCKED"
            ))
            return {"status": "FAILED", "error": err_msg}

        if "JOB_EXPIRED" in err_msg or "job no longer exists" in err_msg.lower() or "job posting no longer exists" in err_msg.lower():
            asyncio.run(publish_application_failed(
                application_id=package_dict.get("application_id", ""),
                error="Job posting no longer exists or has been removed",
                retry_eligible=False,
                failure_reason="JOB_EXPIRED"
            ))
            return {"status": "FAILED", "error": err_msg}

        if "PLATFORM_NEEDS_REVIEW" in err_msg or "flagged as needing review" in err_msg.lower():
            asyncio.run(publish_application_failed(
                application_id=package_dict.get("application_id", ""),
                error=err_msg,
                retry_eligible=False,
                failure_reason="INFRA_ERROR"
            ))
            return {"status": "FAILED", "error": err_msg}

        if self.request.retries >= self.max_retries:
            # Determine appropriate failure reason
            failure_reason = "INFRA_ERROR"
            if "form fill incomplete" in err_msg.lower() or "one or more required fields" in err_msg.lower():
                failure_reason = "FORM_INCOMPLETE"
            elif "captcha" in err_msg.lower():
                failure_reason = "BOT_DETECTED"
            elif "blocked" in err_msg.lower():
                failure_reason = "BOT_DETECTED"
            elif "qualification" in err_msg.lower() or "mismatch" in err_msg.lower():
                failure_reason = "QUALIFICATION_MISMATCH"
                
            asyncio.run(publish_application_failed(
                application_id=package_dict.get("application_id", ""),
                error=err_msg,
                retry_eligible=False,
                failure_reason=failure_reason
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

@celery_app.task(
    name="task:recover_stuck_applications",
)
def recover_stuck_applications():
    """
    Watchdog task that runs periodically (every 10 minutes) to find applications
    stuck in QUEUED, APPLICATION_STARTED, or FORM_COMPLETED status for more than
    their respective thresholds. Marks them as FAILED.

    Runs on the default 'celery' queue so it is not blocked by a backlog of
    browser-automation tasks on queue:application_execution.
    """
    import asyncio
    from app.database import AsyncSessionLocal
    from app.services.state_machine import recover_stuck_applications_async
    
    async def run_recovery():
        from app.database import task_session
        async with task_session() as session:
            await recover_stuck_applications_async(session)

    asyncio.run(run_recovery())

