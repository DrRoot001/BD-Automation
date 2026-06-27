import asyncio
from app.database import AsyncSessionLocal
from app.models.user import User
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.email == "user@bdautomator.com"))
        user = result.scalars().first()
        if user:
            print(f"User: {user.email}")
            print(f"Role: {user.role.value if hasattr(user.role, 'value') else user.role}")
        else:
            print("User not found in local DB.")

asyncio.run(main())
