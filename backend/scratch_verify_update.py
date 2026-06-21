import asyncio
import httpx

async def test_update():
    async with httpx.AsyncClient() as client:
        # Fetch candidate list
        res = await client.get("http://localhost:8002/api/candidates")
        if res.status_code != 200:
            print("Failed to get candidates:", res.status_code, res.text)
            return
            
        candidates = res.json()
        if not candidates:
            print("No candidates found in DB. Skip test.")
            return
            
        cand = candidates[0]
        cand_id = cand["id"]
        original_name = cand["name"]
        test_name = f"{original_name} Test Updated"
        
        print(f"Testing update on candidate: {cand_id} ({original_name})")
        
        # Test PUT update
        put_res = await client.put(
            f"http://localhost:8002/api/candidates/{cand_id}",
            json={"name": test_name}
        )
        if put_res.status_code != 200:
            print("PUT update failed:", put_res.status_code, put_res.text)
            return
            
        updated = put_res.json()
        print("Updated response name:", updated["name"])
        
        # Restore name
        restore_res = await client.put(
            f"http://localhost:8002/api/candidates/{cand_id}",
            json={"name": original_name}
        )
        print("Restored candidate name status:", restore_res.status_code)

if __name__ == "__main__":
    asyncio.run(test_update())
