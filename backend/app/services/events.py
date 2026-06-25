import json
from datetime import datetime
from app.redis_client import redis_client

async def publish_event(event_name: str, payload: dict):
    """
    Universal event bus. All modules call this to broadcast.
    """
    await redis_client.publish(
        f"events:{event_name}",
        json.dumps({
            "event": event_name,
            "data": payload,
            "timestamp": datetime.utcnow().isoformat()
        })
    )