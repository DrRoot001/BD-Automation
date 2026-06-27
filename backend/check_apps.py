import asyncio
from app.database import AsyncSessionLocal
from sqlalchemy import text

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("""
            SELECT id, job_id, status, created_at, error_message, failure_reason 
            FROM applications 
            WHERE candidate_id = 'fc982f14-8180-4908-a948-f5d01982736f' 
            ORDER BY created_at DESC 
            LIMIT 5
        """))
        print("=== RECENT APPLICATIONS ===")
        for row in result:
            print(row)
        
        print("\n=== HISTORY LOGS ===")
        hist = await session.execute(text("""
            SELECT application_id, from_status, to_status, created_at 
            FROM application_history 
            WHERE application_id IN (
                SELECT id FROM applications 
                WHERE candidate_id = 'fc982f14-8180-4908-a948-f5d01982736f' 
                ORDER BY created_at DESC LIMIT 2
            )
            ORDER BY created_at ASC
        """))
        for row in hist:
            print(row)

asyncio.run(main())
