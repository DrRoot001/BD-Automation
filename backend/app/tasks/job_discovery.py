"""Module 2 → Module 1 integration: Job Discovery tasks.

Discovers USA-only jobs from Greenhouse (and other adapters) or using Node scrapers,
and stores them in the central database via the Module 1 REST API.
"""

from __future__ import annotations

import asyncio
import os
from typing import List

import httpx

from app.celery_app import celery_app
from app.module2.run_scrape import run_all as run_node_scraper_all

# ── Companies to scrape from Greenhouse ──────────────────────────────────────
# Expand this list as needed.
GREENHOUSE_COMPANIES: List[str] = [
    "anthropic",
    "vercel",
    "stripe",
    "openai",
    "github",
    "hashicorp",
    "cloudflare",
    "datadog",
    "figma",
    "linear",
    "notion",
    "airtable",
    "plaid",
    "segment",
    "brex",
    "rippling",
    "gusto",
    "lattice",
    "retool",
    "postman",
]

API_BASE_URL = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api")


async def _scrape_and_store() -> dict:
    """Run the full job-discovery pipeline asynchronously."""
    # Lazy imports so the Celery worker doesn't fail if module2 is unavailable
    from module2.adapters.greenhouse_adapter import GreenhouseAdapter
    from module2.normalization.normalizer import Normalizer

    adapter = GreenhouseAdapter()
    normalizer = Normalizer()

    total_scraped = 0
    total_stored = 0
    total_skipped = 0

    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=30.0) as client:
        for company in GREENHOUSE_COMPANIES:
            print(f"[job_discovery] Scraping Greenhouse for: {company}")
            try:
                raw_jobs = await adapter.discover_jobs({"company": company})
            except Exception as exc:
                print(f"[job_discovery] Failed to scrape {company}: {exc}")
                continue

            for raw_job in raw_jobs:
                total_scraped += 1
                try:
                    normalized = normalizer.normalize(raw_job)
                except Exception as exc:
                    print(f"[job_discovery] Normalization error for {raw_job.title}: {exc}")
                    total_skipped += 1
                    continue

                # Build payload for the Module 1 /api/jobs endpoint
                payload = {
                    "title": normalized.title,
                    "company": normalized.company,
                    "location": normalized.location,
                    "description": normalized.description,
                    "source": normalized.source,
                    "source_url": normalized.source_url,
                    "canonical_url": normalized.canonical_url,
                    "skills": normalized.skills,
                    "salary_min": normalized.salary_min,
                    "salary_max": normalized.salary_max,
                    "pay_period": normalized.pay_period,
                    "job_type": normalized.job_type,
                }

                try:
                    resp = await client.post("/jobs", json=[payload])
                    if resp.status_code == 201:
                        total_stored += 1
                    elif resp.status_code == 409:
                        # Duplicate – expected for previously seen jobs
                        total_skipped += 1
                    else:
                        print(
                            f"[job_discovery] Unexpected status {resp.status_code} "
                            f"storing {normalized.title}: {resp.text[:200]}"
                        )
                        total_skipped += 1
                except Exception as exc:
                    print(f"[job_discovery] DB store error for {normalized.title}: {exc}")
                    total_skipped += 1

    summary = {
        "scraped": total_scraped,
        "stored": total_stored,
        "skipped": total_skipped,
    }
    print(f"[job_discovery] ✓ Done — {summary}")
    return summary


@celery_app.task(name="task:discover_jobs_all_platforms", bind=True, max_retries=1)
def discover_jobs_all_platforms(self):
    """Celery task: scrape USA-only jobs from all configured platforms (Python adapters) and store them."""
    print("[job_discovery] Starting discover_jobs_all_platforms task …")
    try:
        return asyncio.run(_scrape_and_store())
    except Exception as exc:
        print(f"[job_discovery] Task failed: {exc}")
        raise self.retry(exc=exc, countdown=60)


@celery_app.task(name="task:discover_jobs_node_scraper")
def discover_jobs_node_scraper():
    """Celery task: scrape all configured links (app/module2/links.py) using the Node-based scrapers and post to /api/jobs."""
    print("[job_discovery] Starting discover_jobs_node_scraper task …")
    return run_node_scraper_all()
