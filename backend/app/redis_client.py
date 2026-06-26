import redis.asyncio as redis
from app.config import get_settings
import ssl

_settings = get_settings()
kwargs = {"decode_responses": True}
if "rediss://" in _settings.redis_url:
    # macOS 12: use ssl.CERT_NONE to bypass certificate verification for Upstash
    kwargs["ssl_cert_reqs"] = ssl.CERT_NONE

redis_client = redis.from_url(_settings.redis_url, **kwargs)