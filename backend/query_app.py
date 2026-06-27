import asyncio
from sqlalchemy import select, text
from app.database import AsyncSessionLocal
from app.models.application import Application

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Application).where(
                Application.id == "f104a72a-e9b0-44b6-8540-9ae47e77d0f2"
            )
        )
        app = result.scalars().first()
        if app:
            print(f"App {app.id}: status={app.status} err={app.error_message}")
        else:
            print("App not found!")
            
        # Get all application histories for this candidate
        res2 = await session.execute(text("SELECT application_id, from_status, to_status, meta_data FROM application_history WHERE application_id = 'f104a72a-e9b0-44b6-8540-9ae47e77d0f2'"))
        for row in res2:
            print(row)

asyncio.run(main())
