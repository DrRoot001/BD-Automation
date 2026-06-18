Module 2: Job Discovery & Normalization Engine
================================================

## Folder Structure

```
module2/
├── __init__.py                          # Package root
├── README.md                            # This file
├── STRUCTURE.md                         # Detailed folder layout
│
├── adapters/                            # Submodule 1: Source Adapters
│   ├── __init__.py
│   ├── base.py                          # BaseSourceAdapter, RawJobData
│   ├── registry.py                      # Adapter registry & decorators
│   ├── rss_adapter.py                   # RSS feed adapter
│   ├── greenhouse_adapter.py            # Greenhouse API adapter
│   ├── lever_adapter.py                 # Lever API adapter
│   ├── indeed_adapter.py                # (TODO) Indeed scraper adapter
│   ├── linkedin_adapter.py              # (TODO) LinkedIn scraper adapter
│   └── ashby_adapter.py                 # (TODO) Ashby API adapter
│
├── normalization/                       # Submodule 2: Normalization Engine
│   ├── __init__.py
│   ├── normalizer.py                    # RawJobData → NormalizedJob
│   ├── schemas.py                       # NormalizedJob dataclass
│   └── helpers.py                       # Text cleaning, parsing helpers
│
├── extraction/                          # Submodules 3 & 4: Skills & Salary Extraction
│   ├── __init__.py
│   ├── skills_extractor.py              # Extract skills from description
│   ├── salary_parser.py                 # Parse salary text → min/max/period
│   ├── url_canonicalizer.py             # URL normalization (Submodule 5)
│   └── skill_taxonomy.py                # Predefined skill list
│
├── deduplication/                       # Submodule 7: Deduplication Engine
│   ├── __init__.py
│   ├── deduplicate.py                   # 3-layer dedup logic
│   ├── fuzzy_matcher.py                 # Layer 2: fuzzy title+company
│   ├── embedding_matcher.py             # Layer 3: pgvector similarity
│   └── redis_cache.py                   # Layer 1: Redis URL cache
│
├── embedding/                           # Submodule 6: Embedding Generation
│   ├── __init__.py
│   ├── generator.py                     # OpenAI embedding API calls
│   ├── cache.py                         # Embedding cache (optional)
│   └── batch.py                         # Batch embedding requests
│
├── filtering/                           # Submodule 8: Filtering & Matching
│   ├── __init__.py
│   ├── matcher.py                       # Match scoring logic
│   ├── business_rules.py                # Location, job type, salary rules
│   └── match_trace.py                   # Score breakdown & reasoning
│
├── storage/                             # Submodule 9: Storage & Event Publishing
│   ├── __init__.py
│   ├── job_store.py                     # PostgreSQL job insertion
│   ├── event_publisher.py               # Redis pub/sub events
│   └── models.py                        # ORM models or schemas
│
├── ratelimit/                           # Submodule 10: Rate Limiting & Proxy
│   ├── __init__.py
│   ├── rate_limiter.py                  # Redis-based rate limiting
│   ├── proxy_manager.py                 # Proxy pool rotation
│   ├── retry_policy.py                  # Exponential backoff + retries
│   └── user_agents.py                   # User agent rotation
│
├── config/                              # Configuration
│   ├── __init__.py
│   ├── settings.py                      # Default settings & env vars
│   ├── adapter_config.yaml              # Adapter-specific configs
│   └── thresholds.py                    # Dedup, matching thresholds
│
├── utils/                               # Utilities
│   ├── __init__.py
│   ├── text.py                          # Text normalization, tokenization
│   ├── date.py                          # Date parsing & windowing (Mon rule)
│   ├── logger.py                        # Logging setup
│   └── errors.py                        # Custom exceptions
│
├── tests/                               # Unit & Integration Tests
│   ├── __init__.py
│   ├── test_adapters.py                 # Adapter tests
│   ├── test_normalization.py            # Normalization tests
│   ├── test_extraction.py               # Skills & salary extraction tests
│   ├── test_deduplication.py            # Dedup engine tests
│   ├── test_filtering.py                # Matching & filtering tests
│   └── integration_test.py              # End-to-end pipeline test
│
├── fixtures/                            # Mock Data
│   ├── __init__.py
│   ├── sample_jobs.json                 # 12 sample job postings
│   ├── sample_applications.json         # 15 application records
│   ├── sample_resumes.json              # 3 resume versions
│   ├── profiles.json                    # Candidate profiles
│   └── raw_job_samples.json             # RawJobData from various sources
│
└── orchestrator.py                      # (TODO) Celery task orchestration
```

## Key Files

### Adapters (Submodule 1)
- `base.py` — Interface all adapters implement
- `registry.py` — Auto-register adapters at import time
- Concrete adapters: `rss_adapter.py`, `greenhouse_adapter.py`, `lever_adapter.py`, etc.

### Normalization (Submodule 2)
- `normalizer.py` — Main entry point; coordinates all extraction submodules
- `schemas.py` — `NormalizedJob` dataclass

### Extraction (Submodules 3, 4, 5)
- `skills_extractor.py` — Match job description against skill taxonomy
- `salary_parser.py` — Extract salary_min, salary_max, pay_period
- `url_canonicalizer.py` — Strip tracking params, normalize URL

### Deduplication (Submodule 7)
- `deduplicate.py` — Main entry; runs 3 layers in sequence
- `redis_cache.py` — Layer 1 (URL)
- `fuzzy_matcher.py` — Layer 2 (title+company)
- `embedding_matcher.py` — Layer 3 (vector similarity)

### Embedding (Submodule 6)
- `generator.py` — Call OpenAI API with text
- `batch.py` — Batch multiple embeddings to reduce API calls

### Filtering & Matching (Submodule 8)
- `matcher.py` — Compute match_score
- `business_rules.py` — Location, job_type, salary rules

### Storage & Events (Submodule 9)
- `job_store.py` — Insert job to PostgreSQL
- `event_publisher.py` — Emit job.passed_filters event

### Rate Limiting (Submodule 10)
- `rate_limiter.py` — Redis counters per platform
- `proxy_manager.py` — Proxy pool round-robin
- `retry_policy.py` — Exponential backoff

## Configuration

Settings in `config/settings.py`:
- Database connection (PostgreSQL URI)
- Redis connection
- OpenAI API key
- Embedding batch size
- Rate limits per adapter
- Dedup thresholds

## Mock Data (for testing)

In `fixtures/`:
- `sample_jobs.json` — 12 realistic tech job postings
- `sample_applications.json` — 15 application records
- `sample_resumes.json` — 3 resume versions with varying ATS scores
- `profiles.json` — Candidate profiles with stacks

## Usage Example

```python
# Discover jobs from Greenhouse
from module2.adapters import get_adapter

adapter_cls = get_adapter("greenhouse")
adapter = adapter_cls()
filters = {
    "company": "acme-corp",
    "location": "USA",
    "job_types": ["contract", "part-time"],
}
raw_jobs = await adapter.discover_jobs(filters)

# Normalize
from module2.normalization import normalize

normalized_jobs = [normalize(raw) for raw in raw_jobs]

# Generate embeddings
from module2.embedding import generate_embeddings

jobs_with_embeddings = await generate_embeddings(normalized_jobs)

# Deduplicate
from module2.deduplication import deduplicate

for job in jobs_with_embeddings:
    dedup_result = await deduplicate(job)
    if not dedup_result.is_duplicate:
        # Filter & match
        from module2.filtering import compute_match
        match = compute_match(job, candidate_profile)
        
        # Store & publish
        from module2.storage import store_job
        await store_job(job, match)
```

## Next Steps

1. Implement fixture loading and mock adapter tests
2. Add PostgreSQL schema and ORM models
3. Implement Celery task orchestration
4. Add comprehensive unit tests
5. Add integration test (full pipeline with fixtures)
6. Add monitoring and metrics
