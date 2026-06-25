from celery import Celery
from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "bd_automation",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        # Module 2 tasks (will be defined in M2, registered here)
        "app.tasks.job_discovery",
        # Module 3 tasks
        "app.tasks.resume_generation",
        # Module 4 tasks
        "app.tasks.application_execution",
        # Module 5 tasks
        "app.tasks.email_scan",
    ]
)

celery_app.conf.task_routes = {
    "task:discover_jobs_*": {"queue": "queue:job_discovery"},
    "task:normalize_job": {"queue": "queue:job_processing"},
    "task:deduplicate_job": {"queue": "queue:job_processing"},
    "task:filter_job": {"queue": "queue:job_processing"},
    "task:score_job_match": {"queue": "queue:resume_generation"},
    "task:tailor_resume": {"queue": "queue:resume_generation"},
    "task:generate_cover_letter": {"queue": "queue:resume_generation"},
    "task:prepare_application_package": {"queue": "queue:resume_generation"},
    "task:execute_application": {"queue": "queue:application_execution"},
    "task:retry_failed_application": {"queue": "queue:application_execution"},
    "task:scan_candidate_inbox": {"queue": "queue:email_scan"},
    "task:refresh_analytics": {"queue": "queue:email_scan"},
}

celery_app.conf.beat_schedule = {
    "discover-jobs-every-6h": {
        "task": "task:discover_jobs_all_platforms",
        "schedule": 60 * 60 * 6,  # 6 hours
    },
    "scan-inbox-every-15m": {
        "task": "task:scan_candidate_inbox",
        "schedule": 60 * 15,  # 15 minutes
    },
    "refresh-analytics-every-1h": {
        "task": "task:refresh_analytics",
        "schedule": 60 * 60,  # 1 hour
    },
}