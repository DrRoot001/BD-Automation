import asyncio
from app.celery_app import celery_app
from app.tasks.dynamic_apply import dynamic_apply

print("Triggering task...")
res = dynamic_apply.apply_async(args=["28052ebd-5e0c-41cb-a803-23e315ff56fb", 1])
print(f"Task triggered: {res.id}")
