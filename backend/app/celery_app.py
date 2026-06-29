# Monkey patch redis.Redis and redis.client.PubSub to fix AttributeError: 'NoneType' object has no attribute '_sock'
# compatibility bug in Celery/Kombu with newer redis versions
# try:
#     import redis
#     class RedisConnectionProperty:
#         def __get__(self, instance, owner):
#             if instance is None:
#                 return self
#             val = getattr(instance, '_patched_connection', None)
#             if val is None:
#                 try:
#                     val = instance.connection_pool.get_connection()
#                     instance._patched_connection = val
#                 except Exception:
#                     pass
#             return val
#         def __set__(self, instance, value):
#             instance._patched_connection = value
# 
#     redis.Redis.connection = RedisConnectionProperty()
#     redis.client.PubSub.connection = RedisConnectionProperty()
# except Exception:
#     pass


import ssl
from celery import Celery
from celery.schedules import crontab
from app.config import get_settings
from app.logging_config import configure_logging

# Configure global structured logging
configure_logging()

settings = get_settings()

from kombu import Queue

celery_app = Celery(
    "bd_automation",
    broker=settings.redis_url,
    backend=settings.redis_url,
    broker_use_ssl={"ssl_cert_reqs": ssl.CERT_NONE} if "rediss://" in settings.redis_url else None,
    redis_backend_use_ssl={"ssl_cert_reqs": ssl.CERT_NONE} if "rediss://" in settings.redis_url else None,
    include=[
        "app.tasks.job_discovery",
        "app.tasks.resume_generation",
        "app.tasks.browser_automation",
        "app.tasks.email_scan",
        "app.tasks.dynamic_apply",
        "app.tasks.daily_job_matching",
        "app.tasks.cleanup",
        "app.tasks.embedding_generation",
    ]
)

# ── Queue definitions ─────────────────────────────────────────────────────────
celery_app.conf.task_queues = [
    Queue("celery", routing_key="celery"),
    Queue("queue:job_discovery", routing_key="queue:job_discovery"),
    Queue("queue:job_processing", routing_key="queue:job_processing"),
    Queue("queue:resume_generation", routing_key="queue:resume_generation"),
    Queue("queue:application_execution", routing_key="queue:application_execution"),
    Queue("queue:email_scan", routing_key="queue:email_scan"),
]

# ── Task routing ──────────────────────────────────────────────────────────────
celery_app.conf.task_routes = {
    "task:discover_jobs_*":             {"queue": "queue:job_discovery"},
    "task:daily_job_matching":          {"queue": "queue:job_processing"},
    "task:match_single_candidate":      {"queue": "queue:job_processing"},
    "task:aggregate_matching_results":  {"queue": "queue:job_processing"},
    "task:normalize_job":               {"queue": "queue:job_processing"},
    "task:deduplicate_job":             {"queue": "queue:job_processing"},
    "task:filter_job":                  {"queue": "queue:job_processing"},
    "task:score_job_match":             {"queue": "queue:resume_generation"},
    "task:tailor_resume":               {"queue": "queue:resume_generation"},
    "task:generate_cover_letter":       {"queue": "queue:resume_generation"},
    "task:prepare_application_package": {"queue": "queue:resume_generation"},
    "task:execute_application":         {"queue": "queue:application_execution"},
    "task:retry_failed_application":    {"queue": "queue:application_execution"},
    "task:dynamic_apply":               {"queue": "queue:application_execution"},
    "task:scan_candidate_inbox":        {"queue": "queue:email_scan"},
    "task:scan_single_inbox":           {"queue": "queue:email_scan"},
    "task:scan_interviews":             {"queue": "queue:email_scan"},
    "task:scan_single_candidate_interviews": {"queue": "queue:email_scan"},
    "task:refresh_analytics":           {"queue": "queue:email_scan"},
    "task:cleanup_old_resumes":         {"queue": "celery"},
    "task:recover_stuck_applications":  {"queue": "celery"},
}

# ── Worker reliability settings ───────────────────────────────────────────────
# prefetch_multiplier=1: worker fetches one task at a time — prevents a slow
# browser-automation task from starving the queue on a single-worker setup.
celery_app.conf.worker_prefetch_multiplier = 1

# acks_late=True: task is acknowledged only after it completes (or explicitly
# fails), so a worker crash doesn't silently drop a task.
celery_app.conf.task_acks_late = True

# Serialisation
celery_app.conf.task_serializer = "json"
celery_app.conf.result_serializer = "json"
celery_app.conf.accept_content = ["json"]

# Task time limits — prevents hung browser sessions from blocking the worker forever.
# soft limit raises SoftTimeLimitExceeded (catchable); hard limit kills the process.
celery_app.conf.task_soft_time_limit = 3000   # 50 minutes — log / clean up
celery_app.conf.task_time_limit = 3600        # 60 minutes — hard kill

# ── Beat schedule ─────────────────────────────────────────────────────────────
celery_app.conf.beat_schedule = {
    # Daily job matching — runs once per day at the configured hour (default 8 AM UTC)
    "daily-job-matching": {
        "task": "task:daily_job_matching",
        "schedule": crontab(hour=settings.daily_match_hour, minute=0),
    },
    # Job discovery — scrape all configured platforms every 24 hours
    "discover-jobs-every-24h": {
        "task": "task:discover_jobs_all_platforms",
        "schedule": 60 * 60 * 24,  # 24 hours
    },
    # Email inbox scan — poll every 15 minutes for candidates with Gmail connected
    "scan-inbox-every-15m": {
        "task": "task:scan_candidate_inbox",
        "schedule": 60 * 15,  # 15 minutes
    },
    # Interview search scan — poll every 30 minutes
    "scan-interviews-every-30m": {
        "task": "task:scan_interviews",
        "schedule": 60 * 30,  # 30 minutes
    },
    # Analytics refresh — recompute cached dashboard metrics every hour
    "refresh-analytics-every-1h": {
        "task": "task:refresh_analytics",
        "schedule": 60 * 60,  # 1 hour
    },
    # Cleanup tailored resumes older than 30 days every Sunday at 2:00 AM
    "cleanup-old-resumes-weekly": {
        "task": "task:cleanup_old_resumes",
        "schedule": crontab(day_of_week=0, hour=2, minute=0),
    },
    # Recover stuck applications (status='QUEUED' or 'APPLICATION_STARTED' for more than 30 minutes)
    "recover-stuck-applications-every-10m": {
        "task": "task:recover_stuck_applications",
        "schedule": 60 * 10,  # 10 minutes
    },
}