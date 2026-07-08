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
import random
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

try:
    from module2.links import get_links_with_categories_for_today
except ImportError:  # pragma: no cover - fallback for backend/app layouts
    try:
        from app.module2.links import get_links_with_categories_for_today
    except ImportError:
        from links import get_links_with_categories_for_today

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")

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
    "fully remote", "remote", "remote in united states", "worldwide", "remote worldwide", "remote us", "us remote"
}

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
SCRAPE_TIMEOUT_SECONDS = int(os.environ.get("SCRAPE_TIMEOUT_SECONDS", "300"))
SCRAPE_PAGES = int(os.environ.get("SCRAPE_PAGES", "3"))
SCRAPE_PAGE_SIZE = int(os.environ.get("SCRAPE_PAGE_SIZE", "20"))


def run_node_scraper(link: str) -> list[dict[str, Any]]:
    """Run the Node scraper subprocess for a single link and return parsed items."""
    try:
        result = subprocess.run(
            ["node", str(SCRAPER_ENTRYPOINT), link],
            cwd=str(SCRAPER_DIR),
            capture_output=True,
            text=True,
            timeout=SCRAPE_TIMEOUT_SECONDS,
            env={**os.environ},
        )
    except subprocess.TimeoutExpired:
        print(f"[run_scrape] TIMEOUT scraping {link}")
        return []

    if result.returncode not in (0, 2):  # 2 = partial results, still usable
        print(f"[run_scrape] scraper failed for {link}: {result.stderr.strip()[-500:]}")
        return []

    try:
        return json.loads(result.stdout.strip() or "[]")
    except json.JSONDecodeError:
        print(f"[run_scrape] could not parse scraper output for {link}: {result.stdout[:300]}")
        return []


def generate_paginated_url(link: str, page: int, page_size: int) -> str:
    """Produce a page-specific URL for common pagination patterns.
    If the original URL already contains a page/start/offset param, replace it;
    otherwise append a `page` query parameter.
    """
    if page <= 1:
        return link

    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

    parsed = urlparse(link)
    qs = parse_qs(parsed.query, keep_blank_values=True)

    # Common page params to try: 'page', 'p'
    if 'page' in qs:
        qs['page'] = [str(page)]
    elif 'p' in qs:
        qs['p'] = [str(page)]
    # Common offset params: 'start' or 'offset' expecting item offset
    elif 'start' in qs or 'offset' in qs:
        offset = (page - 1) * page_size
        if 'start' in qs:
            qs['start'] = [str(offset)]
        if 'offset' in qs:
            qs['offset'] = [str(offset)]
    else:
        # No known param present: append `page` or `start` depending on heuristics.
        # Default to `page` since many portals accept it; also add `start` fallback.
        # Preserve existing query string formatting.
        qs['page'] = [str(page)]

    new_query = urlencode({k: v[0] for k, v in qs.items()})
    new_parts = (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment)
    return urlunparse(new_parts)


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(float(str(value).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _clean_optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text or _normalize_text(text) in {"(not found)", "unknown", "n/a", "na", "none", "null"}:
        return None
    return text


def _parse_posted_at(value: Any) -> Optional[datetime]:
    text = _clean_optional_text(value)
    if not text:
        return None

    candidates = [
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S%z",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_remote_location(location: str) -> bool:
    value = _normalize_text(location)
    if not value:
        return False
    # Direct matches against allowed work type phrases
    if any(work_type in value for work_type in ALLOWED_WORK_TYPES):
        return True

    # Accept explicit worldwide/virtual markers
    if "worldwide" in value or "virtual" in value:
        return True

    # If the string contains 'remote', also accept when it specifically
    # refers to the United States or any US state (full name or 2-letter code).
    if "remote" in value:
        if "united states" in value or "united states of america" in value or "usa" in value or "us " in value or value.endswith(" us"):
            return True

        # US state names and postal abbreviations to catch cases like
        # 'Remote - CA' or 'Remote (New York)'. Lowercase for comparison.
        US_STATES = [
            'alabama','alaska','arizona','arkansas','california','colorado','connecticut','delaware','florida','georgia',
            'hawaii','idaho','illinois','indiana','iowa','kansas','kentucky','louisiana','maine','maryland','massachusetts',
            'michigan','minnesota','mississippi','missouri','montana','nebraska','nevada','new hampshire','new jersey',
            'new mexico','new york','north carolina','north dakota','ohio','oklahoma','oregon','pennsylvania','rhode island',
            'south carolina','south dakota','tennessee','texas','utah','vermont','virginia','washington','west virginia','wisconsin','wyoming'
        ]
        US_ABBREVS = [
            'al','ak','az','ar','ca','co','ct','de','fl','ga','hi','id','il','in','ia','ks','ky','la','me','md','ma','mi','mn','ms','mo','mt',
            'ne','nv','nh','nj','nm','ny','nc','nd','oh','ok','or','pa','ri','sc','sd','tn','tx','ut','vt','va','wa','wv','wi','wy'
        ]

        for st in US_STATES:
            if st in value:
                return True
        # check common patterns like 'remote - NY' or 'ny, remote'
        for ab in US_ABBREVS:
            if f" {ab}" in value or f"-{ab}" in value or f"({ab})" in value or f",{ab}" in value:
                return True

    return False


def _allows_job_type(job_type: str) -> bool:
    value = _normalize_text(job_type).replace("-", " ")
    if not value or value in {"(not found)", "unknown", "n/a", "na"}:
        return True
    return any(allowed in value for allowed in ALLOWED_JOB_TYPES)


def _normalize_job_category(category: Optional[str]) -> Optional[str]:
    if category is None:
        return None
    value = str(category).strip()
    return value.lower() if value else None


def map_item_to_job_create(item: dict[str, Any], link: str, category: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Translate a raw scraped item into the JobCreate shape, or None to skip it."""
    title = (item.get("title") or "").strip()
    company = (item.get("company") or "").strip()
    source_url = (item.get("source_url") or "").strip()

    if not title or not company or not source_url:
        return None  # JobCreate requires these

    # Clearance filter — skip entirely per the standing rule.
    clearance = _normalize_text(item.get("clearance_required"))
    if clearance.startswith("yes"):
        return None

    # Remote filter — skip anything not confirmed remote.
    location = (item.get("location") or "").strip()
    if not _is_remote_location(location):
        return None

    # Job type filter.
    job_type = (item.get("job_type") or "").strip()
    if not _allows_job_type(job_type):
        return None

    skills_raw = (item.get("skills") or "").strip()
    if not skills_raw or _normalize_text(skills_raw) in {"(not found)", "unknown", "n/a", "na", "none", "null"}:
        skills = []
    else:
        skills = [s.strip() for s in skills_raw.split(",") if s.strip()]

    source = urlparse(link).hostname or "unknown"

    return {
        "title": title,
        "company": company,
        "location": location,
        "source": source,
        "source_url": source_url,
        "canonical_url": (item.get("canonical_url") or "").strip() or None,
        "description": _clean_optional_text(item.get("description")),
        "skills": skills,
        "salary_min": _safe_int(item.get("salary_min")),
        "salary_max": _safe_int(item.get("salary_max")),
        "pay_period": _clean_optional_text(item.get("pay_period")),
        "job_type": _clean_optional_text(job_type),
        "posted_at": _parse_posted_at(item.get("posted_at")),
        "job_category": _normalize_job_category(category),
    }


def _serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    serialized = {}
    for key, value in job.items():
        if isinstance(value, datetime):
            serialized[key] = value.isoformat()
        else:
            serialized[key] = value
    return serialized


def post_jobs(jobs: list[dict[str, Any]]) -> int:
    """POST mapped jobs to this backend's own /api/jobs endpoint. Returns count created."""
    if not jobs:
        return 0
    try:
        resp = httpx.post(f"{API_BASE_URL}/api/jobs", json=jobs, timeout=30)
        resp.raise_for_status()
        created = resp.json()
        return len(created)
    except httpx.HTTPError as exc:
        print(f"[run_scrape] failed to POST jobs: {exc}")
        return 0


def run_all() -> dict[str, int]:
    """Scrape every link in today's schedule, filter/map results, and post them. Returns summary stats."""
    links = get_links_with_categories_for_today()
    stats = {"scraped": 0, "kept": 0, "posted": 0}

    print(f"[run_scrape] using {len(links)} links for today")
    for category, link in links:
        print(f"[run_scrape] scraping {link} (category={category})")

        # Pagination: call the Node scraper for multiple pages and aggregate results.
        aggregated_raw = []
        seen_urls = set()
        for page in range(1, SCRAPE_PAGES + 1):
            paged_link = generate_paginated_url(link, page, SCRAPE_PAGE_SIZE)
            print(f"[run_scrape] scraping page {page} -> {paged_link}")
            page_items = run_node_scraper(paged_link)
            if not page_items:
                print(f"[run_scrape] no items from page {page}, stopping pagination for this link")
                break

            new_count = 0
            for item in page_items:
                # Use source_url or canonical_url as dedupe key when available.
                key = (item.get('source_url') or item.get('canonical_url') or '').strip()
                if key:
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                aggregated_raw.append(item)
                new_count += 1

            print(f"[run_scrape] page {page}: {len(page_items)} scraped, {new_count} new")

            # Add a random cooldown sleep between pages to respect rate limits and prevent WAF blocking
            sleep_time = random.uniform(3.0, 7.0)
            print(f"[run_scrape] Sleeping for {sleep_time:.2f}s to respect rate limits...")
            time.sleep(sleep_time)

        stats["scraped"] += len(aggregated_raw)

        mapped = []
        for item in aggregated_raw:
            job = map_item_to_job_create(item, link, category)
            if job:
                mapped.append(job)
        stats["kept"] += len(mapped)

        serialized_jobs = [_serialize_job(job) for job in mapped]
        created_count = post_jobs(serialized_jobs)
        stats["posted"] += created_count
        print(f"[run_scrape] {link}: {len(aggregated_raw)} scraped, {len(mapped)} kept, {created_count} posted")

    return stats


if __name__ == "__main__":
    print(run_all())
