from celery import Celery
from app.config import get_settings
import ssl

settings = get_settings()
app = Celery("test", broker=settings.redis_url, broker_use_ssl={"ssl_cert_reqs": ssl.CERT_NONE})

@app.task(name="test_dummy")
def dummy():
    print("Dummy executed!")
    return "done"

res = dummy.apply_async(queue="queue:email_scan")
print("Queued dummy:", res.id)
