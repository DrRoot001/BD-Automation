import asyncio
from app.database import AsyncSessionLocal
from sqlalchemy import text

async def main():
    async with AsyncSessionLocal() as session:
        hist = await session.execute(text("""
            SELECT application_id, from_status, to_status, created_at, meta_data 
            FROM application_history 
            WHERE application_id = '2f026488-8dec-403c-9774-aedc08fe4532' 
            ORDER BY created_at ASC
        """))
        print("\n=== HISTORY LOGS FOR 2f026488 ===")
        for row in hist:
            print(row)

asyncio.run(main())
