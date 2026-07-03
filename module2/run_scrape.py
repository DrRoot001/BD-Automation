# module2/run_scrape.py
# Orchestrates the job scraper:
#   for each link in links.py:
#     1. run the Node/fetchfox/Gemini scraper as a subprocess
#     2. filter out disallowed job types and clearance-required postings
#     3. map remaining items into the JobCreate shape
#     4. POST the batch to this same backend's /api/jobs endpoint
#
# Can be run directly:  python -m app.module2.run_scrape
# Or invoked from a Celery task (see app/tasks/job_discovery.py).

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

try:
    from app.module2.links import get_links_with_categories
except ImportError:
    from module2.links import get_links_with_categories

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / "backend" / ".env")
load_dotenv(REPO_ROOT / ".env")

SCRAPER_DIR = Path(__file__).resolve().parent / "scraper"
SCRAPER_ENTRYPOINT = SCRAPER_DIR / "scrape.js"

# Allowed job_type values per the standing scraping rules. Internship and
# anything else is dropped.
ALLOWED_JOB_TYPES = {
    "full time", "contract", "1099", "c2c",
    "full time/contract", "freelance", "part time",
}

# Allowed work_type values per the standing scraping rules. Anything else
# (or blank — meaning the AI couldn't confirm it's remote) gets skipped.
ALLOWED_WORK_TYPES = {
    "fully remote", "remote", "remote in united states",
}

def get_api_base_url() -> str:
    url = (os.environ.get("M1_API_BASE_URL") or os.environ.get("API_BASE_URL") or os.environ.get("API_URL") or "http://localhost:8002").rstrip("/").replace("/api", "")
    if ":8000" in url:
        url = url.replace(":8000", ":8002")
    return url

SCRAPE_TIMEOUT_SECONDS = int(os.environ.get("SCRAPE_TIMEOUT_SECONDS", "300"))


def run_node_scraper(link: str) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Run the Node scraper subprocess for a single link and return parsed items and error message if any."""
    try:
        result = subprocess.run(
            ["node", str(SCRAPER_ENTRYPOINT), link],
            cwd=str(SCRAPER_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SCRAPE_TIMEOUT_SECONDS,
            env={**os.environ},
        )
    except FileNotFoundError:
        return [], "Node.js executable ('node') not found in PATH."
    except subprocess.TimeoutExpired:
        return [], f"Scraper execution timed out after {SCRAPE_TIMEOUT_SECONDS}s."
    except Exception as e:
        return [], f"Subprocess execution error: {str(e)}"

    if result.returncode not in (0, 2):  # 2 = partial results, still usable
        stderr_msg = result.stderr.strip()
        if len(stderr_msg) > 300:
            stderr_msg = stderr_msg[-300:]
        return [], stderr_msg or f"Scraper process exited with code {result.returncode}"

    try:
        return json.loads(result.stdout.strip() or "[]"), None
    except json.JSONDecodeError as e:
        return [], f"Failed to parse JSON output: {str(e)}"


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(float(str(value).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def map_item_to_job_create(item: dict[str, Any], link: str, category: str) -> Optional[dict[str, Any]]:
    """Translate a raw scraped item into the JobCreate shape, or None to skip it."""
    title = (item.get("title") or "").strip()
    company = (item.get("company") or "").strip()
    source_url = (item.get("source_url") or "").strip()

    if not title or not company or not source_url:
        return None  # JobCreate requires these

    # Clearance filter — skip entirely per the standing rule.
    clearance = (item.get("clearance_required") or "").strip().lower()
    if clearance.startswith("yes"):
        return None

    # Remote filter — skip anything not confirmed remote.
    location = (item.get("location") or "").strip()
    if location.lower() not in ALLOWED_WORK_TYPES:
        return None

    # Job type filter.
    job_type = (item.get("job_type") or "").strip()
    if job_type.lower() not in ALLOWED_JOB_TYPES:
        return None

    skills_raw = (item.get("skills") or "").strip()
    skills = [s.strip() for s in skills_raw.split(",") if s.strip()] if skills_raw else []

    source = urlparse(link).hostname or "unknown"

    return {
        "title": title,
        "company": company,
        "location": location,
        "source": source,
        "source_url": source_url,
        "canonical_url": (item.get("canonical_url") or "").strip() or None,
        "description": (item.get("description") or "").strip() or None,
        "skills": skills,
        "salary_min": _safe_int(item.get("salary_min")),
        "salary_max": _safe_int(item.get("salary_max")),
        "pay_period": (item.get("pay_period") or "").strip() or None,
        "job_type": job_type,
        "posted_at": (item.get("posted_at") or "").strip() or None,
        "job_category": category,
    }


def post_jobs(jobs: list[dict[str, Any]]) -> int:
    """POST mapped jobs to this backend's own /api/jobs endpoint. Returns count created."""
    if not jobs:
        return 0
    try:
        api_base = get_api_base_url()
        resp = httpx.post(f"{api_base}/api/jobs", json=jobs, timeout=30)
        resp.raise_for_status()
        created = resp.json()
        return len(created)
    except httpx.HTTPError as exc:
        print(f"[run_scrape] failed to POST jobs: {exc}")
        return 0


def run_all() -> dict[str, Any]:
    """Scrape every link in LINKS, filter/map results, and post them. Returns summary stats and errors list."""
    stats = {"scraped": 0, "kept": 0, "posted": 0, "errors": []}

    # Verify if node_modules exists
    if not (SCRAPER_DIR / "node_modules").is_dir():
        stats["errors"].append("Scraper node_modules folder is missing. Please run 'npm install' inside module2/scraper.")
        return stats

    for category, link in get_links_with_categories():
        print(f"[run_scrape] scraping {link} (category={category})")
        raw_items, err = run_node_scraper(link)
        if err:
            stats["errors"].append(f"{link}: {err}")
            print(f"[run_scrape] failed scraping {link}: {err}")
            continue

        stats["scraped"] += len(raw_items)

        mapped = []
        for item in raw_items:
            job = map_item_to_job_create(item, link, category)
            if job:
                mapped.append(job)
        stats["kept"] += len(mapped)

        created_count = post_jobs(mapped)
        stats["posted"] += created_count
        print(f"[run_scrape] {link}: {len(raw_items)} scraped, {len(mapped)} kept, {created_count} posted")

    return stats


if __name__ == "__main__":
    print(run_all())