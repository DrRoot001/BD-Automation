import asyncio; import sys; import os; sys.path.insert(0, os.path.abspath('backend'))
from app.database import AsyncSessionLocal
from app.models.resume import Resume
from sqlalchemy import select
async def run():
    async with AsyncSessionLocal() as session:
        r=await session.execute(select(Resume).where(Resume.candidate_id=='76a9f624-ad21-41ac-bf10-fad936413b75'))
        c=r.scalars().all()
        print([(x.id, x.is_base, x.file_url) for x in c])
asyncio.run(run())
