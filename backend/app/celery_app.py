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


import os
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

# ── Broker connection stability (Upstash / managed Redis) ─────────────────────
# Upstash reaps idle TCP sockets after a short timeout. Without a keepalive /
# periodic health-check the worker's connection silently dies, kombu logs
# "Connection closed by server", and every reserved-but-unacked task is
# "Restored" and redelivered — which made a multi-minute browser run loop
# forever (it could never finish inside one ~90s connection window). These
# options keep the socket warm, detect half-open connections before a command
# blocks on them, and retry transient timeouts instead of crashing the consumer.
_REDIS_TRANSPORT_OPTIONS = {
    # A reserved task is only redelivered to another worker after this many
    # seconds. Must be comfortably LONGER than the longest browser run so a slow
    # application is never handed to a 2nd worker while the 1st is still filling.
    "visibility_timeout": 7200,        # 2 hours
    # Periodic PING keeps Upstash from reaping the connection as idle AND surfaces
    # a dead socket early. This is the single most important setting here.
    "health_check_interval": 25,
    "socket_keepalive": True,          # OS-level TCP keepalive (cross-platform safe)
    "socket_timeout": 120,             # don't block forever on a read
    "socket_connect_timeout": 30,
    "retry_on_timeout": True,
}

celery_app.conf.broker_transport_options = _REDIS_TRANSPORT_OPTIONS
celery_app.conf.result_backend_transport_options = {
    "retry_on_timeout": True,
    "health_check_interval": 25,
    "socket_keepalive": True,
}
# Keep retrying the broker on startup and at runtime instead of dying.
celery_app.conf.broker_connection_retry = True
celery_app.conf.broker_connection_retry_on_startup = True
celery_app.conf.broker_connection_max_retries = None   # retry forever
celery_app.conf.redis_socket_keepalive = True
celery_app.conf.redis_retry_on_timeout = True
celery_app.conf.redis_backend_health_check_interval = 25
celery_app.conf.broker_pool_limit = 10
# Setting this explicitly silences the Celery-6 CPendingDeprecationWarning and,
# at False, means a transient connection blip does NOT cancel an in-flight
# browser run — it is allowed to finish.
celery_app.conf.worker_cancel_long_running_tasks_on_connection_loss = False

# ── Worker reliability settings ───────────────────────────────────────────────
# prefetch_multiplier=1: worker fetches one task at a time — prevents a slow
# browser-automation task from starving the queue on a single-worker setup.
celery_app.conf.worker_prefetch_multiplier = 1

# acks EARLY (acks_late=False): the task is acknowledged when the worker picks it
# up, NOT after it finishes. This is deliberate: a browser-automation run takes
# minutes, and with acks_late the message stayed un-acked the whole time. On the
# flaky Upstash link, EVERY disconnect "Restored" that message and redelivered it,
# so the same application ran forever and never completed (the bug in the logs).
# Acking early means a connection blip during a long run can no longer resurrect
# the task. The safety net against a genuine worker crash is the
# recover_stuck_applications watchdog (re-queues stuck apps) plus the executor's
# idempotency guard (never double-submits an app already past SUBMITTED).
celery_app.conf.task_acks_late = False
celery_app.conf.task_reject_on_worker_lost = False

# worker_max_tasks_per_child: recycle each worker process after N tasks. Browser
# automation spawns Chrome per application; even with explicit cleanup, recycling
# the process periodically reclaims any leaked browser handles / memory so a
# long-lived worker doesn't degrade into resource exhaustion (a prior cause of
# applications hanging until the watchdog reaped them as "worker died").
celery_app.conf.worker_max_tasks_per_child = int(
    os.getenv("CELERY_MAX_TASKS_PER_CHILD", "20")
)

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