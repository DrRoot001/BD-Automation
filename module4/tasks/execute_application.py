import asyncio

from .celery_app import celery_app
from ..services.models import ApplicationPackage, ApplicationResult
from ..services.executor import ApplicationExecutor


import os
import httpx
from celery.exceptions import Retry
from .event_consumer import publish_application_submitted, publish_application_failed

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
    
    full_package_dict = {
        "application_id": app_id,
        "candidate_id": cand_id,
        "job_id": job_id,
        "job_url": job_data.get("source_url", ""),
        "platform": job_data.get("source", ""),
        "candidate_profile": {
            "name": cand_data.get("name", ""),
            "email": cand_data.get("email", ""),
            "phone": cand_data.get("phone", ""),
            "location": cand_data.get("location", ""),
            "linkedin_url": cand_data.get("linkedin_url", ""),
            "website": cand_data.get("website", ""),
        },
        "resume_url": package_dict["resume_url"],
        "cover_letter_url": package_dict.get("cover_letter_url"),
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
    import os
    import httpx

    api_base = os.getenv("M1_API_BASE_URL")
    with httpx.Client() as client:
        resp = client.get(f"{api_base}/applications/{application_id}")
        return resp.json()
