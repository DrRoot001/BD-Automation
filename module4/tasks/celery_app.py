# Redirect to the single canonical Celery app in the backend.
# Two Celery app instances with the same broker/backend cause duplicate task
# registration, split beat schedules, and routing ambiguity.
from app.celery_app import celery_app  # noqa: F401
