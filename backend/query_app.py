import asyncio
from app.database import AsyncSessionLocal
from sqlalchemy import text
async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT id, candidate_id FROM applications WHERE id = 'e20d36d0-f54d-41ea-877c-49b5bfe9985c'"))
        for row in result:
            print(f"App ID: {row.id}, Candidate ID: {row.candidate_id}")
asyncio.run(main())
