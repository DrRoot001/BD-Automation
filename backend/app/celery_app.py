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
import sys
from pathlib import Path

# Ensure the repo root (parent of backend/) is importable so that background tasks
# can import modules from module2, module3, etc. out-of-the-box.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
import ssl
from celery import Celery

# macOS: Python's datetime/timezone operations load Apple's NSTimeZone ObjC class.
# When billiard forks worker processes, the ObjC runtime detects the class was
# mid-initialization and crashes the child with SIGABRT. This env var disables
# that safety check, which is safe for fork-based worker pools in development.
os.environ.setdefault('OBJC_DISABLE_INITIALIZE_FORK_SAFETY', 'YES')
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
import getpass
queue_suffix = (settings.queue_suffix or "").strip()
if not queue_suffix:
    queue_suffix = getpass.getuser().lower()
suffix = f"_{queue_suffix}" if queue_suffix else ""

celery_app.conf.task_queues = [
    Queue(f"celery{suffix}", routing_key=f"celery{suffix}"),
    Queue(f"queue:job_discovery{suffix}", routing_key=f"queue:job_discovery{suffix}"),
    Queue(f"queue:job_processing{suffix}", routing_key=f"queue:job_processing{suffix}"),
    Queue(f"queue:resume_generation{suffix}", routing_key=f"queue:resume_generation{suffix}"),
    Queue(f"queue:application_execution{suffix}", routing_key=f"queue:application_execution{suffix}"),
    Queue(f"queue:email_scan{suffix}", routing_key=f"queue:email_scan{suffix}"),
]

# Browser-execution queue. Defaults to this developer's suffixed queue;
# QUEUE_APPLICATION_EXECUTION overrides it so a machine can pin its workers to
# an explicit private queue (e.g. while stale-code workers still consume an
# older queue name). Exported: the task decorators in tasks/browser_automation.py
# MUST use the same value — a decorator queue= beats task_routes on .delay(),
# so a mismatch would silently unroute browser tasks from the suffixed queues.
EXECUTION_QUEUE = os.getenv("QUEUE_APPLICATION_EXECUTION", f"queue:application_execution{suffix}")
if all(q.name != EXECUTION_QUEUE for q in celery_app.conf.task_queues):
    celery_app.conf.task_queues.append(Queue(EXECUTION_QUEUE, routing_key=EXECUTION_QUEUE))

JOB_PROCESSING_QUEUE = f"queue:job_processing{suffix}"

# ── Task routing ──────────────────────────────────────────────────────────────
celery_app.conf.task_routes = {
    "task:discover_jobs_*":             {"queue": f"queue:job_discovery{suffix}"},
    "task:daily_job_matching":          {"queue": f"queue:job_processing{suffix}"},
    "task:match_single_candidate":      {"queue": f"queue:job_processing{suffix}"},
    "task:aggregate_matching_results":  {"queue": f"queue:job_processing{suffix}"},
    "task:normalize_job":               {"queue": f"queue:job_processing{suffix}"},
    "task:deduplicate_job":             {"queue": f"queue:job_processing{suffix}"},
    "task:filter_job":                  {"queue": f"queue:job_processing{suffix}"},
    "task:score_job_match":             {"queue": f"queue:resume_generation{suffix}"},
    "task:tailor_resume":               {"queue": f"queue:resume_generation{suffix}"},
    "task:generate_cover_letter":       {"queue": f"queue:resume_generation{suffix}"},
    "task:prepare_application_package": {"queue": f"queue:resume_generation{suffix}"},
    "task:execute_application":         {"queue": EXECUTION_QUEUE},
    "task:retry_failed_application":    {"queue": EXECUTION_QUEUE},
    "task:dynamic_apply":               {"queue": f"queue:job_processing{suffix}"},
    "task:scan_candidate_inbox":        {"queue": f"queue:email_scan{suffix}"},
    "task:scan_single_inbox":           {"queue": f"queue:email_scan{suffix}"},
    "task:scan_interviews":             {"queue": f"queue:email_scan{suffix}"},
    "task:scan_single_candidate_interviews": {"queue": f"queue:email_scan{suffix}"},
    "task:refresh_analytics":           {"queue": f"queue:email_scan{suffix}"},
    "task:cleanup_old_resumes":         {"queue": f"celery{suffix}"},
    "task:recover_stuck_applications":  {"queue": f"celery{suffix}"},
    "task:sweep_missing_job_embeddings": {"queue": f"queue:job_processing{suffix}"},
    "task:embed_jobs_batch":            {"queue": f"queue:job_processing{suffix}"},
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
# Publishing (.delay/send_task) resilience: laptop DNS to Upstash flaps for
# seconds at a time (gaierror Errno 8), which was silently eating browser-task
# dispatches mid-pipeline ("Dispatching browser automation" → connect error →
# app stuck QUEUED until the watchdog). Celery's default publish retry only
# spans ~0.6s; stretch it to cover a realistic DNS blip (~30s).
celery_app.conf.task_publish_retry = True
celery_app.conf.task_publish_retry_policy = {
    "max_retries": 6,
    "interval_start": 1.0,
    "interval_step": 2.0,
    "interval_max": 10.0,
}
celery_app.conf.redis_socket_keepalive = True
celery_app.conf.redis_retry_on_timeout = True
celery_app.conf.redis_backend_health_check_interval = 25
celery_app.conf.broker_pool_limit = 10
# RESULT-BACKEND resilience: a transient Upstash DNS/connection blip during
# store_result (mark_as_done) used to raise `network:ConnectionError` OUT of
# trace_task and fail a task whose work had already SUCCEEDED (seen as
# "getaddrinfo failed" bursts when many tasks — 11 email scans + matching +
# apply — open fresh Redis connections at once). always_retry makes the backend
# retry recoverable connection errors instead of propagating them, so a DNS
# hiccup no longer kills a completed task.
celery_app.conf.result_backend_always_retry = True
celery_app.conf.result_backend_max_retries = 10
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
    # Self-heal job embeddings: re-embed any jobs the per-job fire-and-forget task
    # dropped (Gemini rate-limit during a discovery burst, worker blip, etc.).
    "sweep-missing-job-embeddings-every-15m": {
        "task": "task:sweep_missing_job_embeddings",
        "schedule": 60 * 15,  # 15 minutes
    },
}

# Job discovery — scrape all configured platforms every 24 hours. Gated behind
# ENABLE_AUTO_SCRAPE (default OFF) so that dev laptops sharing one Redis/DB don't
# each fire the Gemini-backed scraper. Enable on exactly ONE machine.
if settings.enable_auto_scrape:
    celery_app.conf.beat_schedule["discover-jobs-every-24h"] = {
        "task": "task:discover_jobs_all_platforms",
        "schedule": 60 * 60 * 24,  # 24 hours
    }