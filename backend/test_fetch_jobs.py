import asyncio
import time
import httpx

async def main():
    start = time.time()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get("http://localhost:8000/api/jobs", params={"skip": 0, "limit": 100})
        print(f"Status: {r.status_code}")
        print(f"Fetched {len(r.json())} jobs.")
    end = time.time()
    print(f"Execution time: {end - start:.4f} seconds")

asyncio.run(main())
