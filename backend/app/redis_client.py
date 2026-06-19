import redis.asyncio as redis
from app.config import get_settings

import ssl

_settings = get_settings()
kwargs = {"decode_responses": True}
if "rediss://" in _settings.redis_url:
    kwargs["ssl_cert_reqs"] = "none"

redis_client = redis.from_url(_settings.redis_url, **kwargs)