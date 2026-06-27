import asyncio
import httpx
import sys

async def main():
    async with httpx.AsyncClient() as client:
        res = await client.post("http://127.0.0.1:8000/api/auth/login", json={
            "email": "user@bdautomator.com",
            "password": "User123!"
        })
        if res.status_code != 200:
            print("Login failed:", res.text)
            sys.exit(1)
            
        token = res.json()["access_token"]
        print("Login success.")
        
        me_res = await client.get("http://127.0.0.1:8000/api/auth/me", headers={
            "Authorization": f"Bearer {token}"
        })
        print("Me:", me_res.text)

asyncio.run(main())
