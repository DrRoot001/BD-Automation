import asyncio
from app.database import AsyncSessionLocal
from app.models.user import User
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()
        print(f"Total users: {len(users)}")
        for u in users:
            print(f"ID: {u.id}, Email: {u.email}, Role: {u.role.value if hasattr(u.role, 'value') else u.role}, FullName: {u.full_name}")

asyncio.run(main())
