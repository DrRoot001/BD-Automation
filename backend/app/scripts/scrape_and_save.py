import asyncio
import json
import os
import sys
from pathlib import Path
from datetime import datetime

# Add root folder to sys.path to allow imports
HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "backend"))

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.job import Job
from sqlalchemy import select

from module2.adapters.greenhouse_adapter import GreenhouseAdapter
from module2.adapters.rss_adapter import RssAdapter
from module2.normalization.normalizer import Normalizer
from module2.deduplication.deduplicate import Deduplicator
from module2.extraction.url_canonicalizer import url_hash

async def check_redis_duplicate(redis_cache, canonical_url: str) -> bool:
    """Check both job_seen and job_hash keys in Redis cache."""
    hashed = url_hash(canonical_url)
    # Check job_seen:{hash}
    if redis_cache.exists(f"job_seen:{hashed}"):
        return True
    # Check job_hash:{hash}
    if redis_cache.exists(f"job_hash:{hashed}"):
        return True
    return False

async def main():
    settings = get_settings()
    deduplicator = Deduplicator(redis_url=settings.redis_url)
    
    print("--------------------------------------------------")
    print("STEP 1: Fetching Real-Time Job Data from Adapters")
    print("--------------------------------------------------")
    
    # 1. Fetch from Greenhouse (Stripe)
    greenhouse = GreenhouseAdapter()
    print("Scraping Greenhouse jobs for Stripe...")
    try:
        stripe_raw = await greenhouse.discover_jobs({"company": "stripe"})
        print(f"✓ Scraped {len(stripe_raw)} raw jobs from Greenhouse (Stripe).")
    except Exception as e:
        print(f"✗ Error scraping Greenhouse: {e}")
        stripe_raw = []

    # 2. Fetch from RSS (Remotive)
    rss = RssAdapter()
    print("Scraping RSS jobs from Remotive...")
    try:
        remotive_raw = await rss.discover_jobs({
            "rss_url": "https://remotive.com/remote-jobs/rss",
            "remote_only": True
        })
        print(f"✓ Scraped {len(remotive_raw)} raw jobs from Remotive RSS.")
    except Exception as e:
        print(f"✗ Error scraping Remotive RSS: {e}")
        remotive_raw = []
        
    all_raw_jobs = stripe_raw + remotive_raw
    print(f"\nTotal raw jobs fetched: {len(all_raw_jobs)}")
    if not all_raw_jobs:
        print("No jobs fetched. Exiting.")
        return

    print("--------------------------------------------------")
    print("STEP 2: Normalization")
    print("--------------------------------------------------")
    normalizer = Normalizer()
    normalized_jobs = normalizer.normalize_batch(all_raw_jobs)
    print(f"✓ Normalized {len(normalized_jobs)} jobs.")

    print("--------------------------------------------------")
    print("STEP 3: Internal Batch Deduplication")
    print("--------------------------------------------------")
    # We remove duplicates within the newly scraped batch
    unique_in_batch = []
    skipped_internal_url = 0
    skipped_internal_fuzzy = 0
    
    for job in normalized_jobs:
        # Check canonical URL uniqueness in the current batch
        is_url_dup = False
        for seen_job in unique_in_batch:
            if job.canonical_url == seen_job.canonical_url:
                is_url_dup = True
                break
        if is_url_dup:
            skipped_internal_url += 1
            continue

        # Check title + company fuzzy matching in current batch
        is_fuzzy_dup = False
        for seen_job in unique_in_batch:
            if deduplicator.fuzzy_duplicate(job, seen_job):
                is_fuzzy_dup = True
                break
        if is_fuzzy_dup:
            skipped_internal_fuzzy += 1
            continue

        unique_in_batch.append(job)

    print(f"Internal Url duplicates skipped: {skipped_internal_url}")
    print(f"Internal Fuzzy duplicates skipped: {skipped_internal_fuzzy}")
    print(f"Remaining jobs in batch after internal dedup: {len(unique_in_batch)}")

    print("--------------------------------------------------")
    print("STEP 4: External Cache & DB Deduplication")
    print("--------------------------------------------------")
    
    final_unique_jobs = []
    skipped_external_redis = 0
    skipped_external_db = 0
    
    # Connect to PostgreSQL to check existing jobs
    async with AsyncSessionLocal() as db:
        # Fetch canonical urls and company/titles for comparison
        result = await db.execute(select(Job.canonical_url, Job.title, Job.company))
        db_jobs = result.all()
        # Create lookups
        db_urls = {item[0] for item in db_jobs if item[0]}
        
        for job in unique_in_batch:
            # 1. Check Redis Cache
            if await check_redis_duplicate(deduplicator.cache, job.canonical_url):
                skipped_external_redis += 1
                continue
                
            # 2. Check Postgres DB (by URL)
            if job.canonical_url in db_urls:
                skipped_external_db += 1
                continue
                
            # 3. Check Postgres DB (Fuzzy matching on Title/Company)
            is_db_fuzzy_dup = False
            for db_url, db_title, db_company in db_jobs:
                # Mock a simple object for fuzzy_duplicate
                class MockJob:
                    def __init__(self, title, company):
                        self.title = title
                        self.company = company
                
                db_mock = MockJob(db_title, db_company)
                if deduplicator.fuzzy_duplicate(job, db_mock):
                    is_db_fuzzy_dup = True
                    break
            
            if is_db_fuzzy_dup:
                skipped_external_db += 1
                continue

            # Record seen in Redis for subsequent runs
            deduplicator.mark_seen_url(job.canonical_url, "scraped_run")
            final_unique_jobs.append(job)

    print(f"External Redis cache duplicates skipped: {skipped_external_redis}")
    print(f"External Postgres DB duplicates skipped: {skipped_external_db}")
    print(f"Final new unique jobs to save: {len(final_unique_jobs)}")

    print("--------------------------------------------------")
    print("STEP 5: Saving to scraped_jobs.json")
    print("--------------------------------------------------")
    
    json_path = ROOT_DIR / "scraped_jobs.json"
    
    # Load existing scraped_jobs.json if it exists to append to it
    existing_data = []
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
            print(f"Found {len(existing_data)} existing jobs in scraped_jobs.json.")
        except Exception as e:
            print(f"Could not read existing scraped_jobs.json: {e}")
            existing_data = []

    # Map target dictionary
    serialized_jobs = []
    for job in final_unique_jobs:
        job_dict = job.to_dict()
        # Add a scrape timestamp
        job_dict["scraped_at"] = datetime.utcnow().isoformat()
        serialized_jobs.append(job_dict)
        
    combined_data = existing_data + serialized_jobs
    
    # Deduplicate combined list by canonical_url
    final_combined = []
    seen_urls = set()
    for item in combined_data:
        url = item.get("canonical_url")
        if url not in seen_urls:
            seen_urls.add(url)
            final_combined.append(item)
            
    # Write back to file
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(final_combined, f, indent=2, default=str)
        print(f"✓ Successfully saved {len(final_combined)} total unique jobs to scraped_jobs.json")
        print(f"File location: scraped_jobs.json")
    except Exception as e:
        print(f"✗ Error saving JSON file: {e}")
        
    print("--------------------------------------------------")
    print("RUN COMPLETE")
    print("--------------------------------------------------")

if __name__ == "__main__":
    asyncio.run(main())
