"""Scrape an RSS feed, normalize jobs and POST to backend `/api/jobs`.

Usage:
    python -m module2.examples.rss_post_example

By default uses WeWorkRemotely remote jobs feed.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import httpx
from module2.adapters.rss_adapter import RssAdapter
from module2.normalization import normalize_batch


def normalized_job_to_backend_payload(job: Any) -> Dict[str, Any]:
    allowed_fields = {
        "title",
        "company",
        "location",
        "source",
        "source_url",
        "canonical_url",
        "description",
        "skills",
        "salary_min",
        "salary_max",
        "pay_period",
        "job_type",
        "posted_at",
        "embedding",
    }
    payload = {k: v for k, v in job.to_dict().items() if k in allowed_fields}
    return payload


async def fetch_rss_and_post(rss_url: str, max_post: int = 10) -> None:
    adapter = RssAdapter()
    print(f"Fetching RSS feed: {rss_url}")
    jobs = await adapter.discover_jobs({"rss_url": rss_url})
    print(f"  Retrieved {len(jobs)} items from RSS")
    if not jobs:
        return

    normalized = normalize_batch(jobs)
    payload = [normalized_job_to_backend_payload(j) for j in normalized]
    to_post = payload[:max_post]

    url = "http://localhost:8000/api/jobs"
    print(f"Posting {len(to_post)} jobs to backend: {url}")
    try:
        resp = httpx.post(url, json=to_post, timeout=30.0)
        resp.raise_for_status()
        print(f"  Posted successfully: {resp.status_code}")
        print(f"  Response IDs: {[job.get('id') for job in resp.json()]}")
    except Exception as e:
        print(f"  Error posting jobs to backend: {e}")


async def main():
    # Default example: We Work Remotely remote jobs RSS
    default_rss = "https://weworkremotely.com/categories/remote-jobs/jobs.rss"
    await fetch_rss_and_post(default_rss, max_post=5)


if __name__ == '__main__':
    asyncio.run(main())
