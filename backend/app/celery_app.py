# Monkey patch redis.Redis and redis.client.PubSub to fix AttributeError: 'NoneType' object has no attribute '_sock' compatibility bug in Celery/Kombu with newer redis versions
try:
    import redis
    class RedisConnectionProperty:
        def __get__(self, instance, owner):
            if instance is None:
                return self
            val = getattr(instance, '_patched_connection', None)
            if val is None:
                try:
                    val = instance.connection_pool.get_connection()
                    instance._patched_connection = val
                except Exception:
                    pass
            return val
        def __set__(self, instance, value):
            instance._patched_connection = value

    redis.Redis.connection = RedisConnectionProperty()
    redis.client.PubSub.connection = RedisConnectionProperty()
except Exception:
    pass

import ssl
from celery import Celery
from app.config import get_settings

settings = get_settings()

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