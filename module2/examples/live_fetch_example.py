"""Live fetch example for public Greenhouse endpoints.

Usage:
    python -m module2.examples.live_fetch_example

Replace the company value with a real Greenhouse board token.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import httpx
from module2.adapters.greenhouse_adapter import GreenhouseAdapter
from module2.embedding.batch import batch_generate
from module2.normalization import normalize_batch

BACKEND_URL = "http://localhost:8000/api/jobs"
POST_BATCH_SIZE = 20

# List of companies with active Greenhouse job boards
COMPANIES = [
    "Airtable",
    "Algolia",
    "Asana",
    "Attentive",
    "BigID",
    "Bitwarden",
    "Braze",
    "Branch",
    "Brex",
    "Buildkite",
    "CircleCI",
    "Coinbase",
    "Contentful",
    "Datadog",
    "Dialpad",
    "Dremio",
    "Dropbox",
    "Elastic",
    "Figma",
    "Fastly",
    "Fivetran",
    "GitLab",
    "Hightouch",
    "Intercom",
    "Iterable",
    "Lyft",
    "Mattermost",
    "Neo4j",
    "Netskope",
    "NexHealth",
    "PagerDuty",
    "Pendo",
    "Postman",
    "Qualia",
    "Reltio",
    "Remote",
    "Salsify",
    "Sisense",
    "Stripe",
    "Vercel",
    "Webflow",
]

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
    # Ensure required backend fields exist
    if "source_url" not in payload or not payload["source_url"]:
        payload["source_url"] = payload.get("url")
    return payload


async def post_jobs_to_backend(normalized_jobs: List[Dict[str, Any]]) -> None:
    if not normalized_jobs:
        print("  No jobs to post.")
        return

    print(f"Posting {len(normalized_jobs)} jobs to backend: {BACKEND_URL}")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            for start in range(0, len(normalized_jobs), POST_BATCH_SIZE):
                batch = normalized_jobs[start:start + POST_BATCH_SIZE]
                batch_number = start // POST_BATCH_SIZE + 1
                print(f"  Posting batch {batch_number} ({len(batch)} jobs)...")
                resp = await client.post(BACKEND_URL, json=batch)
                resp.raise_for_status()
                print(f"    Batch posted successfully: {resp.status_code}")
                try:
                    ids = [job.get("id") for job in resp.json()]
                except Exception:
                    ids = []
                print(f"    Response IDs: {ids}")
    except Exception as e:
        print(f"  Error posting jobs to backend: {e}")


KEYWORD_PATTERNS = [
    r"\b(ai|artificial intelligence|machine learning|genai|llm|large language model)s?\b",
    r"\bsalesforce\b",
    r"\b(service now|servicenow)\b",
    r"\b(dynamics 365|dynamics365)\b",
]

KEYWORD_RE = re.compile("|".join(KEYWORD_PATTERNS), re.IGNORECASE)


def _matches_target_keywords(text: str) -> bool:
    return bool(KEYWORD_RE.search(text))


def _is_within_posted_range(posted_at: datetime | None, reference: datetime) -> bool:
    if not posted_at:
        return False
    posted_at = posted_at.astimezone(timezone.utc)
    weekday = reference.weekday()
    # Monday = 0, Tuesday = 1, Wednesday = 2, Thursday = 3, Friday = 4
    days_ago = 3 if weekday == 0 else 1
    cutoff = reference - timedelta(days=days_ago)
    return posted_at >= cutoff


async def fetch_greenhouse(company: str) -> List[Dict[str, Any]]:
    adapter = GreenhouseAdapter()
    slug = company.lower().replace(" ", "-").replace("..", "")
    print(f"Fetching Greenhouse jobs for company: {company} (slug: {slug})")
    jobs = await adapter.discover_jobs({
        "company": slug,
        "remote_only": True,
        "us_only": True,
        "enforce_salary_threshold": False,
        "require_clearance": False,
    })
    print(f"  Retrieved {len(jobs)} jobs")

    now = datetime.now(timezone.utc)
    filtered_jobs = []
    for job in jobs:
        text = " ".join([
            job.title or "",
            job.description or "",
            job.location or "",
        ])
        if not _matches_target_keywords(text):
            continue
        if not _is_within_posted_range(job.posted_at, now):
            continue
        filtered_jobs.append(job)

    print(f"  Filtered to {len(filtered_jobs)} target jobs")
    for job in filtered_jobs[:5]:
        print(f"    - {job.title} | {job.job_type if hasattr(job, 'job_type') else '?'} | {job.location} | Posted: {job.posted_at} | Salary: {job.salary_text or 'N/A'}")

    normalized = normalize_batch(filtered_jobs)
    descriptions = [job.description or "" for job in normalized]
    embeddings = batch_generate(descriptions)
    for job, emb in zip(normalized, embeddings):
        job.embedding = emb
    return [normalized_job_to_backend_payload(job) for job in normalized]


async def main() -> None:
    print("Live fetch example")
    print("====================")

    summary: dict[str, int] = {}
    for company in COMPANIES:
        try:
            gh_jobs = await fetch_greenhouse(company)
            posted = 0
            if gh_jobs:
                await post_jobs_to_backend(gh_jobs)
                posted = len(gh_jobs)
            summary[company] = posted
        except Exception as e:
            print(f"Error fetching/posting for {company}: {e}")
            summary[company] = 0

    print("\nSummary of posted jobs per company:")
    for c, n in summary.items():
        print(f"  - {c}: {n}")


if __name__ == "__main__":
    asyncio.run(main())
