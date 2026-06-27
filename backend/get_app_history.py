import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.application import Application
from app.models.application_history import ApplicationHistory

async def get_logs():
    async with AsyncSessionLocal() as session:
        stmt = select(Application).where(Application.id == 'ab9f348d-a744-4d6b-9739-90602a8214b4')
        result = await session.execute(stmt)
        app = result.scalar_one_or_none()
        if not app:
            print("App not found")
            return
            
        stmt = select(ApplicationHistory).where(ApplicationHistory.application_id == app.id).order_by(ApplicationHistory.created_at.asc())
        result = await session.execute(stmt)
        history = result.scalars().all()
        for h in history:
            print(f"{h.created_at}: {h.from_status} -> {h.to_status} (Meta: {h.metadata})")

if __name__ == "__main__":
    asyncio.run(get_logs())
