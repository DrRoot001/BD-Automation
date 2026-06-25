# Module 2: Job Discovery & Normalization Engine

| Field | Value |
|-------|-------|
| **Owner** | Scraping / Data Pipeline Engineer |
| **Build Order** | 2 of 5 (start after Module 1 schema is deployed) |
| **Depends On** | Module 1 (Database, API, Redis, Celery) |

---

## Scope

### This module DOES

- Multi-platform job discovery across 30+ sources
- Source adapter pattern (one interface, many implementations)
- Raw job data extraction from APIs, scrapers, RSS feeds
- Job normalization to a single unified schema
- 3-layer deduplication (URL → Title+Company → Embedding)
- Job filtering against business rules (location, pay, skills, recency)
- Embedding generation for semantic dedup and downstream matching
- Source priority ranking when the same job appears on multiple platforms
- Proxy rotation and rate limiting for scraper-based sources

### This module DOES NOT

- Resume processing or AI scoring (→ Module 3)
- Browser-based application submission (→ Module 4)
- Email monitoring or classification (→ Module 5)
- Frontend dashboard (→ Module 5)
- Database schema design (→ Module 1, consumed here)

---

## Public Interface

### 1. Source Adapter Protocol

Every platform connector implements this interface:

```python
from abc import ABC, abstractmethod
from typing import Literal, Optional
from pydantic import BaseModel
from datetime import datetime


class RawJobData(BaseModel):
    """Raw data as scraped/fetched from a source, before normalization."""
    title: str
    company: str
    location: Optional[str] = None
    url: str
    description: Optional[str] = None
    salary_text: Optional[str] = None  # raw salary string, e.g. "$120k - $180k"
    posted_at: Optional[datetime] = None
    raw_html: Optional[str] = None     # for scraper sources
    raw_json: Optional[dict] = None    # for API sources
    source_platform: str


class RateLimitConfig(BaseModel):
    max_per_hour: int
    max_per_day: int
    delay_between_requests_seconds: tuple[float, float]  # (min, max) for random


class BaseSourceAdapter(ABC):
    platform_name: str
    ingestion_type: Literal["api", "scraper", "rss", "enterprise_ats"]

    @abstractmethod
    async def discover_jobs(self, filters: "JobSearchFilters") -> list[RawJobData]:
        """Discover jobs matching the given filters."""
        ...

    @abstractmethod
    async def get_job_detail(self, job_url: str) -> RawJobData:
        """Fetch full details for a single job listing."""
        ...

    def get_rate_limit_config(self) -> RateLimitConfig:
        """Return platform-specific rate limits."""
        ...
```

### 2. Normalized Job Schema (output of this module)

```python
class NormalizedJob(BaseModel):
    """Unified job format stored in the `jobs` table. All sources normalize to this."""
    title: str
    company: str
    location: str                                          # e.g. "Remote", "New York, NY"
    source: str                                            # e.g. "greenhouse", "linkedin"
    source_url: str                                        # original URL
    canonical_url: str                                     # tracking params stripped
    description: str
    skills: list[str]                                      # extracted from description
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    pay_period: Literal["hourly", "yearly"]
    job_type: Literal["full-time", "contract", "part-time"]
    posted_at: datetime
    embedding: list[float]                                 # 1536-dim (text-embedding-3-small)
```

### 3. Job Search Filters

```python
class JobSearchFilters(BaseModel):
    location: str = "US"
    remote_only: bool = True
    job_types: list[str] = ["full-time", "contract"]
    min_hourly_rate: int = 60          # contract threshold
    min_annual_salary: int = 120_000   # full-time threshold
    required_skills: list[str] = []    # from candidate profile
    exclude_keywords: list[str] = []
    posted_within_days: int = 7
```

### 4. Deduplication Engine

```python
class DeduplicationResult(BaseModel):
    is_duplicate: bool
    duplicate_of: Optional[str] = None   # job_id of original
    match_layer: Optional[Literal["url", "title_company", "embedding"]] = None
    similarity_score: Optional[float] = None


class DeduplicationEngine:
    async def check(self, job: NormalizedJob) -> DeduplicationResult:
        """
        3-layer dedup check:
        Layer 1: URL canonicalization — strip ?ref=, ?utm_source=, etc.
                 Check canonical_url against job_seen:{hash} in Redis.
        Layer 2: Title + Company fuzzy match — Levenshtein ratio > 0.85
                 Query: SELECT id FROM jobs WHERE company ILIKE %s
                 Then fuzzy-match title.
        Layer 3: Embedding cosine similarity > 0.92
                 Query pgvector: SELECT id FROM jobs
                 WHERE embedding <=> %s < 0.08  (1 - 0.92 = 0.08 distance)
        """
        ...
```

### 5. Filtering Engine

```python
class FilterResult(BaseModel):
    passed: bool
    reason: Optional[str] = None  # why it was filtered out


class FilteringEngine:
    def apply_filters(self, job: NormalizedJob, filters: JobSearchFilters) -> FilterResult:
        """
        Sequential filter checks:
        1. Location: must be US or Remote
        2. Job type: must be in filters.job_types
        3. Pay threshold:
           - If contract: salary_min >= filters.min_hourly_rate (pay_period='hourly')
           - If full-time: salary_min >= filters.min_annual_salary (pay_period='yearly')
           - If salary unknown: PASS (don't filter out)
        4. Recency: posted_at within filters.posted_within_days
        5. Skills: at least 50% of required_skills must appear in job.skills
        6. Exclude keywords: none of exclude_keywords in title or description
        """
        ...
```

### 6. Platform Registry

```python
# All adapters register here at startup
ADAPTER_REGISTRY: dict[str, type[BaseSourceAdapter]] = {}

def register_adapter(cls: type[BaseSourceAdapter]) -> type[BaseSourceAdapter]:
    ADAPTER_REGISTRY[cls.platform_name] = cls
    return cls
```

---

## Supported Platforms

### A. API-Based Sources (highest quality, implement first after RSS)

| Platform | Ingestion | Notes |
|----------|-----------|-------|
| Greenhouse | `api` | Public job board JSON feed: `https://boards-api.greenhouse.io/v1/boards/{company}/jobs` |
| Lever | `api` | Public postings API: `https://api.lever.co/v0/postings/{company}` |
| Ashby | `api` | Public API: `https://api.ashbyhq.com/posting-api/job-board/{company}` |
| SmartRecruiters | `api` | Public API: `https://api.smartrecruiters.com/v1/companies/{id}/postings` |
| Workable | `api` | Public shortcodes API |
| Jobvite | `api` | Public feed |

### B. Scraped Aggregators (semi-structured, implement later)

| Platform | Ingestion | Notes |
|----------|-----------|-------|
| LinkedIn | `scraper` | Aggressive anti-bot. Residential proxies required. Highest risk. |
| Indeed | `scraper` | Moderate anti-bot. Easier than LinkedIn. |
| ZipRecruiter | `scraper` | API available for partners, scraping fallback |
| Dice | `scraper` | Tech-focused, moderate difficulty |
| Glassdoor | `scraper` | Requires auth, heavy bot detection |

### C. RSS / Lightweight (easiest, implement first)

| Platform | Ingestion | Notes |
|----------|-----------|-------|
| We Work Remotely | `rss` | Static HTML, no auth |
| Remote OK | `rss` | JSON API available |
| Remotive | `rss` | RSS feed |
| Himalayas | `rss` | API available |
| YC Jobs / Work at a Startup | `rss` | Static scraping |
| Wellfound | `rss` | Light scraping |
| Hacker News Who is Hiring | `rss` | Monthly thread parsing |

### D. Enterprise ATS (hard mode, defer to Phase 2)

| Platform | Ingestion | Notes |
|----------|-----------|-------|
| Workday | `enterprise_ats` | Every company customizes. Needs adaptive form scraping. |
| SAP SuccessFactors | `enterprise_ats` | Complex, dynamic |
| Oracle Recruiting | `enterprise_ats` | Complex, dynamic |

### Source Priority Ranking

When the same job is found on multiple platforms, prefer the higher-priority source:

```
1. Direct ATS (Greenhouse, Lever, Ashby)     ← highest quality
2. YC / Wellfound / HN
3. Remote boards (Remote OK, WWR, Remotive)
4. Aggregators (Indeed, LinkedIn, Dice)
5. Enterprise ATS (Workday, etc.)             ← most unstable
```

---

## Celery Tasks

| Task Name | Queue | Trigger | Description |
|-----------|-------|---------|-------------|
| `task:discover_jobs_all_platforms` | `queue:job_discovery` | Celery Beat (every 6h) | Fan-out: dispatches per-platform tasks |
| `task:discover_jobs_{platform}` | `queue:job_discovery` | Called by fan-out | Run single adapter's `discover_jobs()` |
| `task:normalize_job` | `queue:job_processing` | After discovery | Convert `RawJobData` → `NormalizedJob` |
| `task:generate_job_embedding` | `queue:job_processing` | During normalization | Call embedding API, attach vector |
| `task:deduplicate_job` | `queue:job_processing` | After normalization | Run 3-layer dedup check |
| `task:filter_job` | `queue:job_processing` | After dedup (if not duplicate) | Apply business rule filters |

### Task Pipeline Flow

```
Celery Beat (every 6h)
  → task:discover_jobs_all_platforms
    → task:discover_jobs_greenhouse
    → task:discover_jobs_lever
    → task:discover_jobs_weworkremotely
    → ... (one per registered adapter)
      → For each RawJobData returned:
        → task:normalize_job
          → task:generate_job_embedding
            → task:deduplicate_job
              → If NOT duplicate:
                → task:filter_job
                  → If PASSED:
                    → POST /api/jobs (store in DB)
                    → Publish event:job.passed_filters
```

---

## Events

| Event | Payload | Description |
|-------|---------|-------------|
| `event:job.discovered` | `{job_id, source, title, company}` | Emitted after normalization + storage |
| `event:job.duplicate_found` | `{job_id, duplicate_of, match_layer}` | Emitted when dedup detects match |
| `event:job.passed_filters` | `{job_id}` | Emitted when job passes all filters → **triggers Module 3** |

---

## Dependencies

| Dependency | Module | What's Used |
|-----------|--------|-------------|
| `POST /api/jobs` | M1 | Store normalized jobs in Supabase |
| `Redis job_seen:{hash}` | M1 | Fast URL dedup cache |
| `Redis rate_limit:{platform}:{candidate_id}` | M1 | Rate limiting counters |
| Celery queues | M1 | `queue:job_discovery`, `queue:job_processing` |
| pgvector | M1 | Embedding similarity queries for dedup layer 3 |
| Redis pub/sub | M1 | Publishing events |

---

## Implementation Sequence

| Step | Task | Est. Time | Notes |
|------|------|-----------|-------|
| 1 | `BaseSourceAdapter` interface + `ADAPTER_REGISTRY` | 0.5 day | Foundation for all adapters |
| 2 | Normalization pipeline: `RawJobData` → `NormalizedJob` (salary parsing, skills extraction, URL canonicalization) | 1 day | Salary text → min/max int, skills from description |
| 3 | 3-layer deduplication engine | 1.5 days | URL cache in Redis, fuzzy title match, pgvector query |
| 4 | Filtering engine with business rules | 0.5 day | Location, pay, recency, skills |
| 5 | RSS adapters: We Work Remotely, Remote OK, Remotive | 1 day | Easiest, validates pipeline end-to-end |
| 6 | API adapters: Greenhouse, Lever, Ashby | 1.5 days | Structured JSON, reliable |
| 7 | Scraper adapters: Indeed (with proxy rotation) | 1.5 days | HTML parsing, anti-bot handling |
| 8 | LinkedIn adapter (with residential proxies, aged accounts) | 2 days | Highest risk, most valuable |
| 9 | Embedding generation (call OpenAI text-embedding-3-small) | 0.5 day | Used by dedup layer 3 and passed to Module 3 |
| 10 | Register Celery tasks with proper routing + Beat schedule | 0.5 day | Wire up the pipeline |
| 11 | Monitoring: per-platform success/failure rates, jobs/run metrics | 0.5 day | Logging + Redis counters |

**Total: ~11 days**

---

## Integration Checklist

> Every item must pass before declaring this module ready for integration.

- [ ] **Store Jobs**: `POST /api/jobs` with a batch of `NormalizedJob` objects returns stored records with UUIDs
- [ ] **Dedup Cache**: Writing `job_seen:{hash}` to Redis prevents re-processing the same URL within 30 days
- [ ] **Dedup Layer 2**: Fuzzy title+company match correctly identifies "Frontend Engineer" ≈ "Frontend Developer" at same company
- [ ] **Dedup Layer 3**: pgvector cosine similarity query returns matches above 0.92 threshold
- [ ] **Filter — Pay**: Contract job with $50/hr is filtered OUT; $65/hr passes
- [ ] **Filter — Pay**: Full-time job with $100k is filtered OUT; $130k passes
- [ ] **Filter — Unknown Salary**: Job with no salary info is NOT filtered out
- [ ] **Filter — Recency**: Job posted 14 days ago is filtered OUT (threshold: 7 days)
- [ ] **Source Priority**: When same job found on Indeed + Greenhouse, Greenhouse version is kept
- [ ] **Event Publishing**: `event:job.passed_filters` is published to Redis pub/sub after a job passes all checks
- [ ] **Celery Routing**: All tasks route to correct queues (`queue:job_discovery` or `queue:job_processing`)
- [ ] **Rate Limiting**: Scraper adapters check `rate_limit:{platform}` before making requests
- [ ] **Schema Compliance**: Every adapter returns data that successfully converts to `NormalizedJob`
- [ ] **End-to-End**: Celery Beat triggers discovery → normalize → dedup → filter → store → event, with no manual intervention
