import redis.asyncio as redis
from app.config import get_settings

_settings = get_settings()
redis_client = redis.from_url(_settings.redis_url, decode_responses=True)