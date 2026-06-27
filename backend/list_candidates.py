import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND_DIR = Path("/Users/sabihhaider/Documents/BD-Automator-Agent/backend")
sys.path.insert(0, str(BACKEND_DIR))

from app.database import AsyncSessionLocal
from app.models.candidate import Candidate
from app.models.application import Application
from app.models.job import Job
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as session:
        print("--- Candidates ---")
        cands = (await session.execute(select(Candidate))).scalars().all()
        for c in cands:
            print(f"ID: {c.id} | Name: {c.name} | Email: {c.email}")
            
        print("\n--- Applications in DB ---")
        apps = (await session.execute(select(Application))).scalars().all()
        for a in apps:
            job = await session.get(Job, a.job_id)
            job_title = job.title if job else "Unknown"
            print(f"AppID: {a.id} | CandID: {a.candidate_id} | Job: {job_title} | Status: {a.status} | Created: {a.created_at}")

if __name__ == "__main__":
    asyncio.run(main())
