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
from module2.run_scrape import run_all as run_node_scraper_all

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

API_BASE_URL = os.getenv("M1_API_BASE_URL", "http://localhost:8002/api")


@celery_app.task(name="task:discover_jobs_all_platforms", bind=True, max_retries=1)
def discover_jobs_all_platforms(self):
    """Celery task: scrape jobs from all configured platforms (delegated to Node-based scrapers) and store them."""
    print("[job_discovery] Starting discover_jobs_all_platforms task (delegating to Node scraper) …")
    try:
        return run_node_scraper_all()
    except Exception as exc:
        print(f"[job_discovery] Task failed: {exc}")
        raise self.retry(exc=exc, countdown=60)


@celery_app.task(name="task:discover_jobs_node_scraper")
def discover_jobs_node_scraper():
    """Celery task: scrape all configured links (app/module2/links.py) using the Node-based scrapers and post to /api/jobs."""
    print("[job_discovery] Starting discover_jobs_node_scraper task …")
    return run_node_scraper_all()
