import asyncio
import json
import logging
from datetime import datetime
from app.redis_client import redis_client

logger = logging.getLogger(__name__)


async def publish_event(event_name: str, payload: dict):
    """
    Universal event bus. All modules call this to broadcast.

    SAFETY: publishing is non-critical (it only feeds the frontend progress
    chips). On a managed broker like Upstash, PUBLISH can intermittently
    hang or time out — letting that exception propagate would crash the
    browser-automation Celery task and force a 5-minute Celery retry hold,
    even though the actual apply work could continue fine. Swallow all
    publish errors with a 3s ceiling so an Upstash blip never gates the
    real work.
    """
    try:
        await asyncio.wait_for(
            redis_client.publish(
                f"events:{event_name}",
                json.dumps({
                    "event": event_name,
                    "data": payload,
                    "timestamp": datetime.utcnow().isoformat(),
                }),
            ),
            timeout=3.0,
        )
    except Exception as exc:
        # Specifically: asyncio.TimeoutError, redis.ConnectionError,
        # socket.gaierror, anything Upstash throws under load. We log and move
        # on — the operator sees the missed event in metrics, not as a dead apply.
        #
        # "Event loop is closed" is EXPECTED and BENIGN in the Celery threads-pool
        # worker: the shared async Redis client is bound to the loop that created
        # it, but each task runs asyncio.run() in its own short-lived loop, so a
        # publish after that loop closes raises this. Frontend progress chips are
        # cosmetic (the DB status is authoritative and the UI also polls it), so
        # log the benign case at DEBUG to keep run logs clean; surface anything
        # else at WARNING.
        _msg = str(exc)
        if "Event loop is closed" in _msg or isinstance(exc, RuntimeError) and "loop" in _msg.lower():
            logger.debug(f"[events] publish '{event_name}' skipped (worker loop closed) — cosmetic only.")
        else:
            logger.warning(
                f"[events] publish '{event_name}' suppressed ({type(exc).__name__}: {exc}) "
                "— event dropped, real work continues."
            )