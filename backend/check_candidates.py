import asyncio
from app.database import AsyncSessionLocal
from app.models.candidate import Candidate
from app.schemas.candidate import CandidateResponse
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Candidate))
        candidates = result.scalars().all()
        print(f"Total candidates in DB: {len(candidates)}")
        for c in candidates:
            print(f"ID: {c.id} | Name: {c.name} | Email: {c.email} | UserID: {c.user_id}")

asyncio.run(main())
