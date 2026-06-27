import asyncio
from app.database import AsyncSessionLocal
from sqlalchemy import text

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT id, name FROM candidates WHERE email = 'chammar310@gmail.com'"))
        for row in result:
            print(f"Candidate: {row[0]} -> {row[1]}")

asyncio.run(main())
