import asyncio
import httpx
from datetime import datetime, timezone
from module2.adapters.greenhouse_adapter import GreenhouseAdapter
from module2.normalization.normalizer import Normalizer
from module2.embedding.generator import generate_embedding

async def run_pipeline():
    print("Initializing Greenhouse adapter...")
    adapter = GreenhouseAdapter()
    
    # Fetch live jobs from Stripe's public Greenhouse board
    filters = {"company": "stripe"}
    print("Fetching live jobs from Greenhouse for 'github' in real time...")
    raw_jobs = await adapter.discover_jobs(filters)
    print(f"Discovered {len(raw_jobs)} raw jobs from Greenhouse.")
    
    if not raw_jobs:
        print("No jobs found, exiting.")
        return
        
    print("Normalizing raw jobs...")
    normalizer = Normalizer()
    normalized_jobs = normalizer.normalize_batch(raw_jobs)
    print(f"Normalized {len(normalized_jobs)} jobs.")
    
    # Take the first 3 normalized jobs to ingest as a live test
    test_jobs = normalized_jobs[:3]
    
    print("Generating embeddings for test jobs...")
    descriptions = [j.description for j in test_jobs]
    embeddings = generate_embedding(descriptions)
    
    # Prepare payload for API
    payload = []
    ts = int(asyncio.get_event_loop().time())
    for job, emb in zip(test_jobs, embeddings):
        # Ensure url is unique by appending timestamp query parameter
        unique_url = f"{job.url}&test_run={ts}" if "?" in job.url else f"{job.url}?test_run={ts}"
        payload.append({
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "source": job.source,
            "source_url": unique_url,
            "canonical_url": unique_url,
            "description": job.description,
            "skills": job.skills,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "pay_period": job.pay_period,
            "job_type": job.job_type,
            "posted_at": job.posted_at.isoformat() if job.posted_at else None,
            "embedding": emb
        })
        
    print("Sending POST request to FastAPI /api/jobs endpoint...")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post("http://localhost:8000/api/jobs", json=payload)
        if resp.status_code == 201:
            print("✓ Real-time ingestion successful!")
            created_jobs = resp.json()
            for c_job in created_jobs:
                print(f"  - Ingested Job ID: {c_job['id']} | Title: {c_job['title']} | Company: {c_job['company']}")
        else:
            print(f"✗ Failed to ingest jobs: {resp.status_code} | {resp.text}")

if __name__ == "__main__":
    asyncio.run(run_pipeline())
