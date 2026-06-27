import asyncio
from sqlalchemy import select, text
from app.database import AsyncSessionLocal
from app.models.job import Job

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT count(*) FROM jobs"))
        count = result.scalar()
        print(f"Total jobs in DB: {count}")

asyncio.run(main())
