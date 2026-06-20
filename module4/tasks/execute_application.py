# This file is intentionally a thin re-export.
#
# The canonical implementation lives in backend/app/tasks/browser_automation.py.
# Having two separate Celery apps register the same task name ("task:execute_application")
# causes race conditions and silent routing bugs.  All workers should import from the
# backend module to ensure a single task registry.
from app.tasks.browser_automation import (  # noqa: F401
    execute_application,
    retry_failed_application,
    verify_submission,
    hydrate_and_execute,
    publish_application_submitted,
    publish_application_failed,
    publish_status_changed,
)
