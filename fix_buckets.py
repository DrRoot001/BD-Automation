import os
import asyncio
import asyncpg
from dotenv import load_dotenv

load_dotenv()

async def run():
    db_url = os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    if not db_url:
        raise ValueError("DATABASE_URL not found in environment")
    conn = await asyncpg.connect(db_url)
    await conn.execute("UPDATE storage.buckets SET public = true WHERE name IN ('resume', 'cover_letter', 'updated_resume', 'screenshots');")
    print("Buckets updated to public: True")
    await conn.close()

asyncio.run(run())
