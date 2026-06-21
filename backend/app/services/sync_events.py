import json
import logging
from datetime import datetime
import redis
from app.config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()
kwargs = {"decode_responses": True}
if "rediss://" in _settings.redis_url:
    kwargs["ssl_cert_reqs"] = "none"

try:
    sync_redis_client = redis.from_url(_settings.redis_url, **kwargs)
except Exception as e:
    logger.error(f"Failed to initialize sync redis client: {e}")
    sync_redis_client = None

def publish_event_sync(event_name: str, payload: dict):
    """
    Synchronous event publisher for Celery tasks.
    """
    if not sync_redis_client:
        return
        
    try:
        sync_redis_client.publish(
            f"events:{event_name}",
            json.dumps({
                "event": event_name,
                "data": payload,
                "timestamp": datetime.utcnow().isoformat()
            })
        )
    except Exception as e:
        logger.error(f"Failed to publish sync event {event_name}: {e}")
