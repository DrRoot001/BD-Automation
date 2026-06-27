import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.application_history import ApplicationHistory

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ApplicationHistory).where(
                ApplicationHistory.application_id == "f104a72a-e9b0-44b6-8540-9ae47e77d0f2"
            ).order_by(ApplicationHistory.created_at)
        )
        for h in result.scalars().all():
            print(f"[{h.created_at}] {h.from_status} -> {h.to_status} | metadata={h.meta_data}")

asyncio.run(main())
