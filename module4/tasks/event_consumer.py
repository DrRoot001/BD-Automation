import json
import os
import datetime

import redis.asyncio as aioredis

from .execute_application import execute_application


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

async def publish_event(event_name: str, payload: dict) -> None:
    """
    Publish payload to Redis.
    To be fully integrated with both Module 4 specifications and the main backend,
    this publishes to both f"event:{clean_name}" and f"events:{clean_name}" channels.
    """
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    kwargs = {}
    if "rediss://" in redis_url:
        kwargs["ssl_cert_reqs"] = "none"
    redis_client = aioredis.from_url(redis_url, **kwargs)
    try:
        clean_name = event_name
        if clean_name.startswith("event:"):
            clean_name = clean_name[len("event:"):]
        elif clean_name.startswith("events:"):
            clean_name = clean_name[len("events:"):]

        # 1. Publish to the event:clean_name channel (plain payload)
        await redis_client.publish(f"event:{clean_name}", json.dumps(payload))

        # 2. Publish to the events:clean_name channel (wrapped payload)
        wrapped = {
            "event": clean_name,
            "data": payload,
            "timestamp": datetime.datetime.utcnow().isoformat()
        }
        await redis_client.publish(f"events:{clean_name}", json.dumps(wrapped))
    finally:
        await redis_client.aclose()


# ---------------------------------------------------------------------------
# Public publish helpers
# ---------------------------------------------------------------------------

async def publish_application_submitted(
    application_id: str,
    screenshot_url: str,
    confirmation_text: str,
) -> None:
    """Publish an *application.submitted* event."""
    await publish_event(
        "event:application.submitted",
        {
            "application_id":    application_id,
            "screenshot_url":    screenshot_url,
            "confirmation_text": confirmation_text,
        },
    )


async def publish_application_failed(
    application_id: str,
    error: str,
    retry_eligible: bool,
) -> None:
    """Publish an *application.failed* event."""
    await publish_event(
        "event:application.failed",
        {
            "application_id": application_id,
            "error":          error,
            "retry_eligible": retry_eligible,
        },
    )


async def publish_status_changed(
    application_id: str,
    from_status: str,
    to_status: str,
) -> None:
    """Publish an *application.status_changed* event with an ISO-8601 timestamp."""
    await publish_event(
        "event:application.status_changed",
        {
            "application_id": application_id,
            "from_status":    from_status,
            "to_status":      to_status,
            "timestamp":      datetime.datetime.utcnow().isoformat(),
        },
    )


# ---------------------------------------------------------------------------
# Redis pub/sub consumer
# ---------------------------------------------------------------------------

async def start_consumer() -> None:
    """
    Subscribe to both ``event:application.package_ready`` and ``events:application.package_ready``
    and dispatch a Celery ``execute_application`` task for every message received.

    This coroutine runs indefinitely and should be started with
    ``asyncio.run(start_consumer())`` or inside an async entry-point.
    """
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    kwargs = {}
    if "rediss://" in redis_url:
        kwargs["ssl_cert_reqs"] = "none"
    redis_client = aioredis.from_url(redis_url, **kwargs)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("event:application.package_ready", "events:application.package_ready")

    async for message in pubsub.listen():
        if message["type"] == "message":
            raw_data = json.loads(message["data"])
            
            # Extract internal data if it is wrapped in standard events format
            if isinstance(raw_data, dict) and "data" in raw_data and "event" in raw_data:
                package_dict = raw_data["data"]
            else:
                package_dict = raw_data
                
            # Dispatch the Celery task with the package dict; the task will
            # deserialise it into an ApplicationPackage internally.
            execute_application.apply_async(args=[package_dict])

