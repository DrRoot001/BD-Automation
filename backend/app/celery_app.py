from celery import Celery
from app.config import settings

celery_app = Celery(
    "bd_automator",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_routes={
        "task:discover_jobs_all_platforms": {"queue": "queue:job_discovery"},
        "task:scan_candidate_inbox": {"queue": "queue:email_scan"},
        "task:refresh_analytics": {"queue": "queue:email_scan"},
        "task:apply_to_job": {"queue": "queue:application_execution"},
        "task:tailor_resume": {"queue": "queue:resume_generation"},
    }
)
