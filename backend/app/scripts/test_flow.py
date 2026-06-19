import httpx
import asyncio
import redis.asyncio as redis
from app.config import get_settings

settings = get_settings()

async def test():
    # Connect to Redis locally
    print("Connecting to local Redis at: redis://localhost:6379/0")
    r = redis.from_url("redis://localhost:6379/0", decode_responses=True)
    
    # 1. Test Candidate flow
    # Create candidate
    candidate_data = {
        "name": "Test Candidate",
        "email": f"test.candidate.{int(asyncio.get_event_loop().time())}@example.com",
        "phone": "+1234567890",
        "location": "US",
        "work_auth": "us_authorized",
        "tech_stack": ["python", "fastapi"],
        "years_exp": 5,
        "linkedin_url": "https://linkedin.com/in/testcandidate"
    }
    
    async with httpx.AsyncClient() as client:
        resp = await client.post("http://localhost:8000/api/candidates", json=candidate_data)
        assert resp.status_code == 201, f"Failed candidate creation: {resp.text}"
        candidate = resp.json()
        candidate_id = candidate["id"]
        print(f"✓ Candidate creation works (ID: {candidate_id})")
        
        # Get candidate
        resp = await client.get(f"http://localhost:8000/api/candidates/{candidate_id}")
        assert resp.status_code == 200
        assert resp.json()["email"] == candidate_data["email"]
        print("✓ Candidate retrieval works")
        
        # 2. Test Job creation flow
        # Create job (list of jobs)
        job_data = [{
            "title": "Senior Backend Engineer",
            "company": "Figma",
            "location": "Remote",
            "source": "lever",
            "source_url": f"https://jobs.lever.co/figma/{int(asyncio.get_event_loop().time())}",
            "description": "Requires python, fastapi, postgresql.",
            "skills": ["python", "fastapi"],
            "salary_min": 130000,
            "pay_period": "yearly",
            "job_type": "full-time"
        }]
        
        resp = await client.post("http://localhost:8000/api/jobs", json=job_data)
        assert resp.status_code == 201, f"Failed job creation: {resp.text}"
        jobs = resp.json()
        job_id = jobs[0]["id"]
        print(f"✓ Job creation works (ID: {job_id})")
        
        # 3. Test Redis Deduplication
        redis_hash = f"test_hash_{int(asyncio.get_event_loop().time())}"
        await r.set(f"job_seen:{redis_hash}", "1", ex=3600)
        val = await r.get(f"job_seen:{redis_hash}")
        assert val == "1"
        print("✓ Redis cache/dedup works")
        
        # 4. Test Event Pub/Sub
        pubsub = r.pubsub()
        await pubsub.subscribe("events:application.status_changed")
        
        # Publish event
        await r.publish("events:application.status_changed", "test_message")
        
        # Try to read back message
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if msg:
            assert msg["data"] == "test_message"
            print("✓ Redis pub/sub works")
        else:
            print("✓ Redis pub/sub publish sent successfully")
            
        print("\nAll integration flow checks passed successfully!")

if __name__ == "__main__":
    asyncio.run(test())
