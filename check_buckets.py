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
    rows = await conn.fetch("SELECT id, name, public FROM storage.buckets;")
    for row in rows:
        print(dict(row))
    await conn.close()

asyncio.run(run())
