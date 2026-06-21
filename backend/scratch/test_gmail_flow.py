import asyncio
import httpx

async def main():
    async with httpx.AsyncClient() as client:
        # 1. Fetch candidates list to get an existing candidate ID
        resp = await client.get("http://localhost:8002/api/candidates")
        if resp.status_code != 200:
            print("Failed to fetch candidates from backend:", resp.text)
            return
            
        candidates = resp.json()
        if not candidates:
            print("No candidates found in DB. Please run dev.sh or seed the database first.")
            return
            
        cand_id = candidates[0]['id']
        print(f"Testing with candidate: {candidates[0]['name']} (ID: {cand_id})")
        
        # 2. Test GET auth-url endpoint
        url_resp = await client.get(f"http://localhost:8002/api/candidates/{cand_id}/google/auth-url")
        print("GET auth-url status:", url_resp.status_code)
        print("GET auth-url response:", url_resp.json())
        
        # 3. Test POST callback endpoint (simulate oauth)
        callback_resp = await client.post(
            f"http://localhost:8002/api/candidates/{cand_id}/google/callback",
            json={"code": "test_mock_code"}
        )
        print("POST callback status:", callback_resp.status_code)
        print("POST callback response:", callback_resp.json())
        
        # 4. Fetch the candidate details again to check google_connected
        detail_resp = await client.get(f"http://localhost:8002/api/candidates/{cand_id}")
        print("GET candidate status:", detail_resp.status_code)
        updated_cand = detail_resp.json()
        print("google_connected is now:", updated_cand.get("google_connected"))
        print("google_refresh_token exists in DB:", bool(updated_cand.get("google_refresh_token", None)) or "google_refresh_token" in updated_cand)

if __name__ == "__main__":
    asyncio.run(main())
