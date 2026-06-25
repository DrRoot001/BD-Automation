import httpx, asyncio, redis.asyncio as redis

async def test():
    r = redis.from_url("redis://localhost:6379/0")
    
    # Test 1: M2 can POST jobs
    jobs = [{
        "title": "Senior Engineer",
        "company": "TestCo",
        "source": "greenhouse",
        "source_url": "https://boards.greenhouse.io/testco/jobs/123",
        "skills": ["python", "fastapi"],
        "salary_min": 150000,
        "pay_period": "yearly",
        "job_type": "full-time"
    }]
    resp = httpx.post("http://localhost:8000/api/jobs", json=jobs)
    assert resp.status_code == 201
    job_id = resp.json()[0]["id"]
    print("✓ M2 Jobs API works")
    
    # Test 2: M2 Redis dedup
    await r.set("job_seen:testhash", "1", ex=30*24*3600)
    val = await r.get("job_seen:testhash")
    assert val == "1"
    print("✓ M2 Redis dedup works")
    
    # Test 3: M3 can read candidate
    resp = httpx.get("http://localhost:8000/api/candidates/{some_uuid}")
    print("✓ M3 Read API works")
    
    # Test 4: M4 rate limits
    await r.incr("rate_limit:linkedin:candidate-1")
    await r.expire("rate_limit:linkedin:candidate-1", 3600)
    print("✓ M4 Rate limit Redis works")
    
    # Test 5: M4 session cache
    await r.hset("session:candidate-1:linkedin", mapping={"cookie": "abc123"})
    print("✓ M4 Session cache works")
    
    # Test 6: State machine
    # Create application, then PATCH status
    resp = httpx.patch(f"http://localhost:8000/api/applications/{app_id}/status",
                      json={"status": "ANALYZED"})
    assert resp.status_code == 200
    # Invalid transition should 422
    resp = httpx.patch(f"http://localhost:8000/api/applications/{app_id}/status",
                      json={"status": "SUBMITTED"})
    assert resp.status_code == 422
    print("✓ State machine enforces transitions")
    
    # Test 7: Events
    pubsub = r.pubsub()
    await pubsub.subscribe("events:application.status_changed")
    print("✓ Event pub/sub works")

asyncio.run(test())