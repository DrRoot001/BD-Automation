import redis
import ssl
from app.config import get_settings

settings = get_settings()
r = redis.from_url(settings.redis_url, decode_responses=True, ssl_cert_reqs=ssl.CERT_NONE)
print("Keys:", r.keys("*"))
print("Queue len:", r.llen("queue:application_execution"))
