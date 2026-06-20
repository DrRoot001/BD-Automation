"""Run all adapters (mock, greenhouse, lever, indeed, rss) and POST normalized jobs to backend.

Usage:
    python -m module2.examples.test_all_scrapers

Limits discovery to a small number of jobs per adapter to avoid flooding services.
"""
from __future__ import annotations

import asyncio
import traceback
from typing import Any, Dict, List

import httpx
from module2.adapters import get_adapter
from module2.normalization import normalize_batch


ALLOWED_FIELDS = {
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

BACKEND_URL = "http://localhost:8000/api/jobs"


def to_payload(normalized_jobs: List[Any]) -> List[Dict[str, Any]]:
    return [{k: v for k, v in job.to_dict().items() if k in ALLOWED_FIELDS} for job in normalized_jobs]


async def run_adapter(name: str, filters: Dict[str, Any], cap: int = 5) -> None:
    print(f"\n--- Running adapter: {name} ---")
    try:
        cls = get_adapter(name)
        adapter = cls()
    except Exception as e:
        print(f"Adapter {name} not available: {e}")
        return

    try:
        # Allow a longer timeout for slower APIs like Lever
        if name == "lever":
            jobs = await asyncio.wait_for(adapter.discover_jobs(filters), timeout=60.0)
        else:
            jobs = await adapter.discover_jobs(filters)
        print(f"Discovered {len(jobs)} raw jobs from {name}")
        if not jobs:
            return
        normalized = normalize_batch(jobs)
        print(f"Normalized {len(normalized)} jobs")
        payload = to_payload(normalized[:cap])
        if not payload:
            print("No payload to post (empty after filtering)")
            return

        # Fetch existing source_urls from backend to avoid unique constraint errors
        try:
            existing = httpx.get(BACKEND_URL, timeout=10.0).json()
            existing_urls = {j.get("source_url") for j in existing if isinstance(j, dict)}
        except Exception:
            existing_urls = set()
        payload = [p for p in payload if p.get("source_url") not in existing_urls]
        if not payload:
            print("All discovered jobs already exist in backend; skipping post.")
            return

        # For Greenhouse, post in single-job batches to isolate server errors
        if name == "greenhouse":
            for job_payload in payload:
                try:
                    print(f"Posting 1 job to backend (greenhouse): {BACKEND_URL}")
                    resp = httpx.post(BACKEND_URL, json=[job_payload], timeout=30.0)
                    print(f"  Response: {resp.status_code}")
                    print(resp.text)
                except Exception as e:
                    print(f"  Error posting single greenhouse job: {e}")
        else:
            print(f"Posting {len(payload)} jobs to backend: {BACKEND_URL}")
            resp = httpx.post(BACKEND_URL, json=payload, timeout=30.0)
            print(f"  Backend response: {resp.status_code}")
            try:
                print(f"  Body: {resp.json()}")
            except Exception:
                print(f"  Body text: {resp.text}")
    except Exception as e:
        print(f"Error running adapter {name}: {e}")
        traceback.print_exc()


async def main():
    # Mock adapter (offline)
    await run_adapter("mock", {"location": "USA", "remote_only": True}, cap=5)

    # Greenhouse (public endpoint)
    await run_adapter("greenhouse", {"company": "anthropic", "retries": 3}, cap=10)

    # Lever (public endpoint)
    await run_adapter("lever", {"company": "netflix", "location": "Remote", "retries": 3}, cap=10)

    # Indeed (RSS)
    await run_adapter("indeed", {"q": "remote", "sort": "date", "retries": 3}, cap=5)

    # RSS generic - try a few known feeds but include headers
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    rss_urls = [
        "https://weworkremotely.com/categories/remote-jobs/jobs.rss",
        "https://remotive.com/remote-jobs/rss",
        "https://stackoverflow.com/jobs/feed",
    ]
    for rss in rss_urls:
        await run_adapter("rss_generic", {"rss_url": rss, "request_headers": headers, "retries": 4}, cap=5)


if __name__ == "__main__":
    asyncio.run(main())
