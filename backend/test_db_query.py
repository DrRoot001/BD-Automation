import asyncio
import time
from sqlalchemy import select
from sqlalchemy.orm import defer
from app.database import AsyncSessionLocal
from app.models.job import Job

async def main():
    start = time.time()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Job)
            .options(defer(Job.embedding))
            .order_by(Job.created_at.desc())
            .limit(100)
        )
        jobs = result.scalars().all()
    end = time.time()
    print(f"DB Query + SQLAlchemy parsing: {end - start:.4f} seconds")

asyncio.run(main())
