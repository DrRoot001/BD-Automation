import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import AsyncSessionLocal
from app.services.state_machine import recover_stuck_applications_async

async def main():
    async with AsyncSessionLocal() as session:
        print("Starting watchdog manual run...")
        try:
            await asyncio.wait_for(recover_stuck_applications_async(session), timeout=10.0)
            print("Watchdog completed successfully.")
        except asyncio.TimeoutError:
            print("Watchdog TIMED OUT after 10 seconds!")
        except Exception as e:
            print(f"Watchdog FAILED with error: {e}")

asyncio.run(main())
