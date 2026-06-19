import os

from celery import Celery

# ---------------------------------------------------------------------------
# Standalone Celery application for Module 4.
# Do NOT import or reuse any other module's Celery app.
# ---------------------------------------------------------------------------

celery_app = Celery(
    "browser_automation",
    broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    backend=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
)

celery_app.conf.update(
    task_default_queue="queue:application_execution",
    task_queues={
        "queue:application_execution": {
            "exchange": "queue:application_execution",
            "routing_key": "queue:application_execution",
        }
    },
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)
