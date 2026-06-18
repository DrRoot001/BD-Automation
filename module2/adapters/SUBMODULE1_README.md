Submodule 1: Source Adapters
=============================

Complete implementation of job source adapters for Module 2.

## Files

- `base.py`                   — BaseSourceAdapter interface, RawJobData, RateLimitConfig
- `registry.py`              — Adapter registry and registration decorator
- `rss_adapter.py`           — Generic RSS feed adapter
- `greenhouse_adapter.py`    — Greenhouse ATS API adapter
- `lever_adapter.py`         — Lever ATS API adapter
- `mock_adapter.py`          — Mock adapter for testing (hardcoded sample data)
- `example_usage.py`         — Example code showing how to use adapters
- `test_adapters_quick.py`   — Quick unit tests
- `__init__.py`              — Package exports
- `README.md`                — This file

## Architecture

### BaseSourceAdapter
All adapters inherit from `BaseSourceAdapter` and implement:
- `async discover_jobs(filters)` — Return list of RawJobData from the platform
- `async get_job_detail(url)` — Fetch full details for a single job
- `get_rate_limit_config()` — Platform-specific rate limits

### RawJobData
Dataclass representing raw job data as fetched from a source (before normalization):
- `title`, `company`, `location`, `url` (required)
- `description`, `salary_text`, `posted_at` (optional)
- `raw_json`, `raw_html` (raw response data)
- `source_platform` (which adapter returned this)

### Adapter Registry
- `@register_adapter` decorator auto-registers adapters by platform_name
- `get_adapter(name)` retrieves adapter class by name
- `list_adapters()` returns all registered adapters
- `is_adapter_registered(name)` checks if adapter exists

## Supported Platforms

| Platform | Type | File | Status |
|----------|------|------|--------|
| Greenhouse | API | greenhouse_adapter.py | ✓ Implemented |
| Lever | API | lever_adapter.py | ✓ Implemented |
| RSS (Generic) | RSS | rss_adapter.py | ✓ Implemented |
| Mock (Testing) | Mock | mock_adapter.py | ✓ Implemented |
| Indeed | Scraper | (TODO) | Not yet |
| LinkedIn | Scraper | (TODO) | Not yet |
| Ashby | API | (TODO) | Not yet |

## Usage

### Basic Example: Using MockAdapter

```python
import asyncio
from module2.adapters import get_adapter

async def main():
    # Get adapter by name (auto-registered)
    adapter = get_adapter("mock")()
    
    # Define filters
    filters = {
        "location": "USA",
        "remote_only": True,
        "job_types": ["contract", "part-time"],
    }
    
    # Discover jobs
    jobs = await adapter.discover_jobs(filters)
    
    for job in jobs:
        print(f"{job.title} at {job.company}")
        print(f"  URL: {job.url}")
        print(f"  Salary: {job.salary_text}")

asyncio.run(main())
```

### Using GreenhouseAdapter with Real API

```python
import asyncio
from module2.adapters import get_adapter

async def main():
    adapter = get_adapter("greenhouse")()
    
    filters = {
        "company": "stripe",  # Company short name
        "location": "USA",
        "remote_only": True,
    }
    
    jobs = await adapter.discover_jobs(filters)
    print(f"Found {len(jobs)} jobs from Greenhouse")

asyncio.run(main())
```

### Using RssAdapter

```python
import asyncio
from module2.adapters import get_adapter

async def main():
    adapter = get_adapter("rss_generic")()
    
    filters = {
        "rss_url": "https://weworkremotely.com/categories/remote-jobs/jobs.rss",
    }
    
    jobs = await adapter.discover_jobs(filters)
    print(f"Found {len(jobs)} jobs from RSS feed")

asyncio.run(main())
```

## Filter Dictionary

Standard filters understood by all adapters:

```python
filters = {
    "location": "USA",                    # Location preference (str)
    "remote_only": True,                  # Include only remote jobs (bool)
    "job_types": ["contract", "part-time"],  # Allowed types (list[str])
    "min_hourly_rate": 60,                # Minimum hourly ($) (int)
    "min_annual_salary": 120000,          # Minimum annual ($) (int)
    "required_skills": ["ml service now", "ai automation"],  # Keywords (list[str])
    "exclude_keywords": ["senior", "lead"],  # Avoid jobs with these (list[str])
    "posted_within_days": 7,              # Only recent jobs (int)
    # Platform-specific keys:
    "company": "stripe",                  # For Greenhouse, Lever
    "rss_url": "https://...",             # For RSS adapter
    "keywords": "python machine learning",  # For scrapers
}
```

Most adapters only require their platform-specific key and ignore standard filters.

## Testing

### Run Quick Tests
```bash
cd module2/adapters
python test_adapters_quick.py
```

### Run Example Usage
```bash
cd module2/adapters
python -m asyncio example_usage.py
```

### Import & Test in Python REPL
```python
import asyncio
from module2.adapters import get_adapter, list_adapters

# List all registered adapters
print(list_adapters())

# Test mock adapter
adapter = get_adapter("mock")()
jobs = asyncio.run(adapter.discover_jobs({}))
print(f"Mock adapter returned {len(jobs)} jobs")
```

## Implementing a New Adapter

1. Create a new file, e.g., `indeed_adapter.py`:

```python
from module2.adapters.base import BaseSourceAdapter, RawJobData
from module2.adapters.registry import register_adapter

@register_adapter
class IndeedAdapter(BaseSourceAdapter):
    platform_name = "indeed"
    ingestion_type = "scraper"
    
    async def discover_jobs(self, filters):
        # Implement discovery logic
        # Return list[RawJobData]
        pass
    
    async def get_job_detail(self, job_url):
        # Implement detail fetching
        # Return RawJobData
        pass
```

2. Import it somewhere (e.g., in `__init__.py` or at startup):
```python
from module2.adapters.indeed_adapter import IndeedAdapter
```

3. Use it:
```python
adapter = get_adapter("indeed")()
jobs = await adapter.discover_jobs(filters)
```

## Notes on Implementation

- All adapters are **async** to allow integration with Celery/async task runners
- Network calls use `urllib.request` (stdlib) for simplicity in the prototype
- Production use should replace with `httpx` or `aiohttp` for better async support
- Rate limiting and proxy rotation are handled by Submodule 10 (ratelimit/)
- Adapters should handle errors gracefully and log them
- Each adapter can define custom rate limits via `get_rate_limit_config()`

## What's Next

- Implement more adapters (Indeed, LinkedIn, Ashby, etc.)
- Add retry/backoff logic for network failures
- Add proxy rotation support for scraper adapters
- Add caching layer for API responses
- Add comprehensive unit tests with mock HTTP responses
- Integrate with Celery task queue for async discovery

## Error Handling

Adapters should handle common errors:
- Network timeouts (HTTP 408, 504)
- Rate limits (HTTP 429)
- Authentication errors (HTTP 401, 403)
- Server errors (HTTP 500, 502, 503)
- Parsing errors (malformed JSON, invalid XML)
- Missing required filters

Example:
```python
try:
    jobs = await adapter.discover_jobs(filters)
except ValueError as e:
    print(f"Invalid filters: {e}")
except Exception as e:
    print(f"Error discovering jobs: {e}")
    return []
```

---

**Status**: ✓ Complete and tested for core use cases (MockAdapter, GreenhouseAdapter, LeverAdapter, RssAdapter).
