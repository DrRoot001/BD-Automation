import asyncio
from sqlalchemy import select
from app.database import get_db
from app.models.application import Application
from app.models.job import Job

async def check():
    try:
        async for session in get_db():
            result = await session.execute(
                select(Application, Job)
                .join(Job, Application.job_id == Job.id)
                .order_by(Application.created_at.desc())
                .limit(10)
            )
            rows = result.all()
            print(f"Total applications found: {len(rows)}")
            for app, job in rows:
                print(f"App ID: {app.id} | Candidate: {app.candidate_id} | Job: {job.title} at {job.company} | Status: {app.status} | Created At: {app.created_at}")
            break
    except Exception as e:
        print("Verification failed:", e)

if __name__ == "__main__":
    asyncio.run(check())
