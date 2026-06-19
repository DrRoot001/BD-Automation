import asyncio
import os
import json
import datetime
import httpx
from celery.exceptions import Retry
import redis.asyncio as aioredis

from app.celery_app import celery_app
from app.browser_automation.services.models import ApplicationPackage, ApplicationResult
from app.browser_automation.services.executor import ApplicationExecutor

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
    
    async with httpx.AsyncClient() as client:
        app_resp = await client.get(f"{api_base}/applications/{app_id}")
        app_resp.raise_for_status()
        app_data = app_resp.json()
        
        cand_id = app_data["candidate_id"]
        job_id = app_data["job_id"]
        
        cand_resp = await client.get(f"{api_base}/candidates/{cand_id}")
        cand_resp.raise_for_status()
        cand_data = cand_resp.json()
        
        job_resp = await client.get(f"{api_base}/jobs/{job_id}")
        job_resp.raise_for_status()
        job_data = job_resp.json()
    
        # Resolve resume_url if it is a UUID or missing
        resume_url = package_dict.get("resume_url", "")
        from uuid import UUID
        is_uuid = False
        try:
            UUID(resume_url)
            is_uuid = True
        except ValueError:
            pass

        if is_uuid or not resume_url or not os.path.exists(resume_url):
            res_resp = await client.get(f"{api_base}/resumes/{cand_id}")
            if res_resp.status_code == 200:
                resumes = res_resp.json()
                matching_resume = None
                if is_uuid:
                    matching_resume = next((r for r in resumes if r["id"] == resume_url), None)
                if not matching_resume and resumes:
                    # Sort so that latest tailored (is_base=False) is preferred
                    resumes.sort(key=lambda r: (not r.get("is_base"), r.get("version", 0)), reverse=True)
                    matching_resume = resumes[0]
                if matching_resume:
                    resume_url = matching_resume["file_url"]

        cover_letter_url = package_dict.get("cover_letter_url") or app_data.get("cover_letter_url")
    
    full_package_dict = {
        "application_id": app_id,
        "candidate_id": cand_id,
        "job_id": job_id,
        "job_url": job_data.get("source_url", ""),
        "platform": job_data.get("source", ""),
        "candidate_profile": {
            "name": cand_data.get("name") or "",
            "email": cand_data.get("email") or "",
            "phone": cand_data.get("phone") or "",
            "location": cand_data.get("location") or "",
            "linkedin_url": cand_data.get("linkedin_url") or "",
            "website": cand_data.get("website") or "",
        },
        "resume_url": resume_url,
        "cover_letter_url": cover_letter_url,
        "screening_answers": package_dict.get("screening_answers"),
    }
    
    package = ApplicationPackage(**full_package_dict)
    executor = ApplicationExecutor()
    result = await executor.execute(package, retry_count=retry_count)
    
    if result.status == "SUBMITTED":
        await publish_application_submitted(
            application_id=result.application_id,
            screenshot_url=result.screenshot_url or "",
            confirmation_text=result.confirmation_text or ""
        )
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
