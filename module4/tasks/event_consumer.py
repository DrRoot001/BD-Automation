"""Module 4 Redis event consumer.

Subscribes to ``event:application.package_ready`` and dispatches a Celery
``execute_application`` task for each message.  All event-publishing helpers
are re-exported from the canonical backend module to avoid duplication.
"""
import json
import os

import redis.asyncio as aioredis

# Re-export from the single source of truth so any module importing these from
# event_consumer still works without importing from two places.
from app.tasks.browser_automation import (  # noqa: F401
    publish_event,
    publish_application_submitted,
    publish_application_failed,
    publish_status_changed,
)
from .execute_application import execute_application


async def start_consumer() -> None:
    """Subscribe to application.package_ready events and dispatch execution tasks.

    Handles both the bare ``event:...`` channel and the wrapped ``events:...``
    channel so it works regardless of which publisher format is used.
    """
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    kwargs = {}
    if "rediss://" in redis_url:
        kwargs["ssl_cert_reqs"] = "none"

    redis_client = aioredis.from_url(redis_url, **kwargs)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(
        "event:application.package_ready",
        "events:application.package_ready",
    )

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue

        try:
            raw = json.loads(message["data"])
        except (json.JSONDecodeError, TypeError):
            continue

        # Unwrap the events:... envelope if present
        package_dict = raw.get("data", raw) if isinstance(raw, dict) and "data" in raw else raw

        execute_application.apply_async(args=[package_dict])
