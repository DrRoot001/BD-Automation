# Module 1: Data & Orchestration Layer

| Field | Value |
|-------|-------|
| **Owner** | Backend / Platform Engineer |
| **Build Order** | 1 of 5 (Foundation — start first) |
| **Depends On** | Nothing (all other modules depend on this) |

---

## Scope

### This module DOES

- FastAPI application setup (project structure, routers, middleware, config)
- Supabase / PostgreSQL schema design for **all** system tables
- Redis setup (queues, caching, rate limiting keys, pub/sub)
- Celery worker configuration (task routing, beat schedule, result backend)
- Shared Pydantic data models used across all modules
- Application state machine (status enum + valid transitions)
- Authentication (JWT tokens + Google OAuth for Gmail integration)
- Docker Compose for local development (PostgreSQL, Redis, API, workers)
- Health-check endpoints, structured logging, error handling

### This module DOES NOT

- Scraping logic or platform adapters (→ Module 2)
- AI/LLM calls, resume scoring, cover letter generation (→ Module 3)
- Browser automation or form filling (→ Module 4)
- Email parsing or classification (→ Module 5)
- Frontend dashboard UI (→ Module 5)

---

## Public Interface

### 1. Database Schema (PostgreSQL via Supabase)

#### `candidates`

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | `UUID` | PK, default `gen_random_uuid()` |
| `name` | `VARCHAR(255)` | NOT NULL |
| `email` | `VARCHAR(255)` | NOT NULL, UNIQUE |
| `phone` | `VARCHAR(50)` | |
| `location` | `VARCHAR(255)` | default `'US'` |
| `work_auth` | `VARCHAR(50)` | default `'us_authorized'` |
| `tech_stack` | `TEXT[]` | NOT NULL, default `'{}'` |
| `years_exp` | `INTEGER` | |
| `linkedin_url` | `VARCHAR(500)` | |
| `created_at` | `TIMESTAMPTZ` | default `NOW()` |
| `updated_at` | `TIMESTAMPTZ` | default `NOW()` |

#### `resumes`

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | `UUID` | PK |
| `candidate_id` | `UUID` | FK → `candidates.id`, NOT NULL |
| `version` | `INTEGER` | NOT NULL |
| `file_url` | `VARCHAR(1000)` | NOT NULL |
| `parsed_json` | `JSONB` | sections: summary, skills, experience, education |
| `is_base` | `BOOLEAN` | default `false` |
| `tailored_for_job_id` | `UUID` | FK → `jobs.id`, NULLABLE |
| `created_at` | `TIMESTAMPTZ` | default `NOW()` |

**Unique constraint**: `(candidate_id, version)`

#### `jobs`

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | `UUID` | PK |
| `title` | `VARCHAR(500)` | NOT NULL |
| `company` | `VARCHAR(255)` | NOT NULL |
| `location` | `VARCHAR(255)` | |
| `source` | `VARCHAR(100)` | NOT NULL (e.g., `greenhouse`, `linkedin`) |
| `source_url` | `VARCHAR(2000)` | NOT NULL, UNIQUE |
| `canonical_url` | `VARCHAR(2000)` | tracking params stripped |
| `description` | `TEXT` | |
| `skills` | `TEXT[]` | default `'{}'` |
| `salary_min` | `INTEGER` | NULLABLE |
| `salary_max` | `INTEGER` | NULLABLE |
| `pay_period` | `VARCHAR(20)` | `'hourly'` or `'yearly'` |
| `job_type` | `VARCHAR(20)` | `'full-time'`, `'contract'`, `'part-time'` |
| `posted_at` | `TIMESTAMPTZ` | |
| `embedding` | `VECTOR(1536)` | for semantic dedup (pgvector) |
| `is_duplicate` | `BOOLEAN` | default `false` |
| `duplicate_of` | `UUID` | FK → `jobs.id`, NULLABLE |
| `created_at` | `TIMESTAMPTZ` | default `NOW()` |

**Index**: `idx_jobs_company_title` on `(company, title)` for dedup layer 2.

#### `companies`

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | `UUID` | PK |
| `name` | `VARCHAR(255)` | NOT NULL, UNIQUE |
| `domain` | `VARCHAR(255)` | |
| `ats_type` | `VARCHAR(50)` | e.g., `greenhouse`, `lever`, `workday` |
| `rate_limit_config` | `JSONB` | `{max_per_hour, max_per_day}` |
| `created_at` | `TIMESTAMPTZ` | default `NOW()` |

#### `applications`

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | `UUID` | PK |
| `candidate_id` | `UUID` | FK → `candidates.id`, NOT NULL |
| `job_id` | `UUID` | FK → `jobs.id`, NOT NULL |
| `resume_id` | `UUID` | FK → `resumes.id` |
| `cover_letter_url` | `VARCHAR(1000)` | |
| `status` | `application_status` | ENUM, NOT NULL, default `'FOUND'` |
| `fit_score` | `NUMERIC(5,2)` | 0-100 |
| `ats_score` | `NUMERIC(5,2)` | 0-100 |
| `combined_score` | `NUMERIC(5,2)` | weighted average |
| `screenshot_url` | `VARCHAR(1000)` | |
| `submitted_at` | `TIMESTAMPTZ` | |
| `error_message` | `TEXT` | |
| `retry_count` | `INTEGER` | default `0` |
| `created_at` | `TIMESTAMPTZ` | default `NOW()` |
| `updated_at` | `TIMESTAMPTZ` | default `NOW()` |

**Unique constraint**: `(candidate_id, job_id)` — prevents duplicate applications.

#### `application_history`

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | `UUID` | PK |
| `application_id` | `UUID` | FK → `applications.id`, NOT NULL |
| `from_status` | `application_status` | NULLABLE (NULL for initial) |
| `to_status` | `application_status` | NOT NULL |
| `metadata` | `JSONB` | any additional context |
| `created_at` | `TIMESTAMPTZ` | default `NOW()` |

#### `emails`

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | `UUID` | PK |
| `candidate_id` | `UUID` | FK → `candidates.id`, NOT NULL |
| `application_id` | `UUID` | FK → `applications.id`, NULLABLE |
| `gmail_id` | `VARCHAR(255)` | UNIQUE, NOT NULL |
| `from_addr` | `VARCHAR(255)` | |
| `subject` | `TEXT` | |
| `body_text` | `TEXT` | |
| `classification` | `email_classification` | ENUM |
| `confidence` | `NUMERIC(3,2)` | 0.00-1.00 |
| `raw_json` | `JSONB` | |
| `received_at` | `TIMESTAMPTZ` | |
| `processed_at` | `TIMESTAMPTZ` | |

#### `interviews`

| Column | Type | Constraints |
|--------|------|-------------|
| `id` | `UUID` | PK |
| `application_id` | `UUID` | FK → `applications.id`, NOT NULL |
| `round` | `INTEGER` | 1 or 2 |
| `type` | `VARCHAR(50)` | `phone`, `video`, `onsite`, `assessment` |
| `scheduled_at` | `TIMESTAMPTZ` | |
| `meeting_url` | `VARCHAR(1000)` | |
| `interviewer_name` | `VARCHAR(255)` | |
| `notes` | `TEXT` | |
| `created_at` | `TIMESTAMPTZ` | default `NOW()` |

### 2. Enums

```sql
CREATE TYPE application_status AS ENUM (
    'FOUND',
    'ANALYZED',
    'MATCHED',
    'RESUME_UPDATED',
    'COVER_LETTER_CREATED',
    'QUEUED',
    'APPLICATION_STARTED',
    'FORM_COMPLETED',
    'SUBMITTED',
    'CONFIRMED',
    'INTERVIEW_R1',
    'INTERVIEW_R2',
    'REJECTED',
    'OFFER'
);

CREATE TYPE email_classification AS ENUM (
    'APPLIED_CONFIRMATION',
    'INTERVIEW_R1',
    'INTERVIEW_R2',
    'ASSESSMENT',
    'REJECTED',
    'OFFER',
    'UNKNOWN'
);
```

### 3. Application State Machine

```
Valid transitions:
  FOUND → ANALYZED
  ANALYZED → MATCHED
  MATCHED → RESUME_UPDATED
  RESUME_UPDATED → COVER_LETTER_CREATED
  COVER_LETTER_CREATED → QUEUED
  QUEUED → APPLICATION_STARTED
  APPLICATION_STARTED → FORM_COMPLETED
  FORM_COMPLETED → SUBMITTED
  SUBMITTED → CONFIRMED
  CONFIRMED → INTERVIEW_R1
  INTERVIEW_R1 → INTERVIEW_R2
  INTERVIEW_R2 → OFFER

  // Rejection can happen from any active state
  SUBMITTED → REJECTED
  CONFIRMED → REJECTED
  INTERVIEW_R1 → REJECTED
  INTERVIEW_R2 → REJECTED

  // Failure and retry
  APPLICATION_STARTED → QUEUED  (retry)
  FORM_COMPLETED → QUEUED      (retry)
```

```python
VALID_TRANSITIONS: dict[str, list[str]] = {
    "FOUND": ["ANALYZED"],
    "ANALYZED": ["MATCHED"],
    "MATCHED": ["RESUME_UPDATED"],
    "RESUME_UPDATED": ["COVER_LETTER_CREATED"],
    "COVER_LETTER_CREATED": ["QUEUED"],
    "QUEUED": ["APPLICATION_STARTED"],
    "APPLICATION_STARTED": ["FORM_COMPLETED", "QUEUED"],
    "FORM_COMPLETED": ["SUBMITTED", "QUEUED"],
    "SUBMITTED": ["CONFIRMED", "REJECTED"],
    "CONFIRMED": ["INTERVIEW_R1", "REJECTED"],
    "INTERVIEW_R1": ["INTERVIEW_R2", "REJECTED"],
    "INTERVIEW_R2": ["OFFER", "REJECTED"],
}

def transition_status(current: str, target: str) -> bool:
    """Returns True if transition is valid, raises InvalidTransition otherwise."""
```

### 4. API Endpoints

| Method | Path | Request Body | Response | Used By |
|--------|------|-------------|----------|---------|
| `POST` | `/api/candidates` | `CreateCandidateRequest` | `CandidateResponse` | Setup |
| `GET` | `/api/candidates/{id}` | — | `CandidateResponse` | M3, M4, M5 |
| `POST` | `/api/resumes` | `CreateResumeRequest` (multipart) | `ResumeResponse` | M3 |
| `GET` | `/api/resumes/{candidate_id}` | query: `?is_base=true` | `list[ResumeResponse]` | M3, M4 |
| `POST` | `/api/jobs` | `list[CreateJobRequest]` | `list[JobResponse]` | M2 |
| `GET` | `/api/jobs` | query: filters | `PaginatedResponse[JobResponse]` | M3, M5 |
| `GET` | `/api/jobs/{id}` | — | `JobResponse` | M3, M4 |
| `POST` | `/api/applications` | `CreateApplicationRequest` | `ApplicationResponse` | M3 |
| `GET` | `/api/applications` | query: `?status=` | `PaginatedResponse[ApplicationResponse]` | M4, M5 |
| `PATCH` | `/api/applications/{id}/status` | `{status: str, metadata?: dict}` | `ApplicationResponse` | M3, M4, M5 |
| `GET` | `/api/analytics/summary` | query: `?candidate_id=` | `AnalyticsSummary` | M5 |
| `POST` | `/api/auth/login` | `{email, password}` | `{access_token, token_type}` | All |
| `POST` | `/api/auth/google` | `{code}` | `{access_token, refresh_token}` | M5 |
| `GET` | `/api/health` | — | `{status: "ok"}` | Ops |

### 5. Celery Task Queues

| Queue Name | Purpose | Consumer |
|-----------|---------|----------|
| `queue:job_discovery` | Trigger scraping runs | Module 2 workers |
| `queue:job_processing` | Normalization, dedup, filtering | Module 2 workers |
| `queue:resume_generation` | Resume tailoring + cover letter | Module 3 workers |
| `queue:application_execution` | Browser automation tasks | Module 4 workers |
| `queue:email_scan` | Email polling + classification | Module 5 workers |

### 6. Celery Beat Schedule

| Task | Schedule | Queue |
|------|----------|-------|
| `task:discover_jobs_all_platforms` | Every 6 hours | `queue:job_discovery` |
| `task:scan_candidate_inbox` | Every 15 minutes | `queue:email_scan` |
| `task:refresh_analytics` | Every 1 hour | `queue:email_scan` |

### 7. Redis Key Patterns

| Key Pattern | Type | Purpose | TTL |
|------------|------|---------|-----|
| `rate_limit:{platform}:{candidate_id}` | String (counter) | Rate limiting | 1 hour |
| `rate_limit_daily:{platform}:{candidate_id}` | String (counter) | Daily rate limit | 24 hours |
| `session:{candidate_id}:{platform}` | Hash | Browser session/cookie cache | 7 days |
| `job_seen:{hash}` | String | Dedup bloom filter | 30 days |
| `last_email_scan:{candidate_id}` | String (timestamp) | Track last Gmail poll | No TTL |

### 8. Events (Redis Pub/Sub)

| Event Name | Payload | Published By | Consumed By |
|-----------|---------|-------------|-------------|
| `event:job.discovered` | `{job_id, source, title, company}` | M2 | M5 (dashboard) |
| `event:job.passed_filters` | `{job_id}` | M2 | M3 (triggers matching) |
| `event:job.matched` | `{job_id, candidate_id, combined_score, should_apply}` | M3 | M5 (dashboard) |
| `event:application.package_ready` | `{application_id, resume_url, cover_letter_url}` | M3 | M4 (triggers execution) |
| `event:application.status_changed` | `{application_id, from_status, to_status, timestamp}` | M1 (auto) | M5 (WebSocket push) |
| `event:application.submitted` | `{application_id, screenshot_url}` | M4 | M5 (start email watch) |
| `event:application.failed` | `{application_id, error, retry_eligible}` | M4 | M5 (dashboard) |
| `event:email.classified` | `{email_id, classification, application_id}` | M5 | M5 (dashboard) |
| `event:interview.detected` | `{interview_id, application_id, scheduled_at}` | M5 | M5 (dashboard) |

### 9. Project Structure

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app factory
│   ├── config.py               # Settings via pydantic-settings
│   ├── database.py             # Supabase/PostgreSQL connection
│   ├── redis_client.py         # Redis connection
│   ├── celery_app.py           # Celery configuration
│   ├── auth/
│   │   ├── jwt.py              # JWT token creation/validation
│   │   ├── google_oauth.py     # Google OAuth flow
│   │   └── dependencies.py     # FastAPI auth dependencies
│   ├── models/                 # SQLAlchemy / Supabase models
│   │   ├── candidate.py
│   │   ├── resume.py
│   │   ├── job.py
│   │   ├── company.py
│   │   ├── application.py
│   │   ├── email.py
│   │   └── interview.py
│   ├── schemas/                # Pydantic request/response schemas
│   │   ├── candidate.py
│   │   ├── resume.py
│   │   ├── job.py
│   │   ├── application.py
│   │   └── analytics.py
│   ├── routers/                # FastAPI route handlers
│   │   ├── candidates.py
│   │   ├── resumes.py
│   │   ├── jobs.py
│   │   ├── applications.py
│   │   ├── analytics.py
│   │   └── auth.py
│   ├── services/
│   │   ├── state_machine.py    # Application status transitions
│   │   └── events.py           # Redis pub/sub publisher
│   └── migrations/             # Alembic migrations
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── celery_worker.py
```

---

## Dependencies

**None.** This is the foundation module. All other modules depend on it.

### External Dependencies (pip)

```
fastapi==0.115.*
uvicorn[standard]
sqlalchemy[asyncio]==2.0.*
asyncpg
alembic
pydantic==2.*
pydantic-settings
python-jose[cryptography]   # JWT
httpx                       # HTTP client for OAuth
celery[redis]==5.*
redis[hiredis]
python-multipart            # File uploads
pgvector                    # Vector similarity for dedup
```

---

## Implementation Sequence

| Step | Task | Estimated Time |
|------|------|---------------|
| 1 | Docker Compose with PostgreSQL 15, Redis 7, pgvector extension | 0.5 day |
| 2 | FastAPI project structure with routers, config, CORS middleware | 0.5 day |
| 3 | PostgreSQL migrations: all 8 tables + 2 enums + indexes | 1 day |
| 4 | Pydantic schemas for all request/response types | 1 day |
| 5 | State machine implementation with audit logging to `application_history` | 0.5 day |
| 6 | JWT auth + Google OAuth middleware | 1 day |
| 7 | Celery + Redis configuration with 5 queues and task routing | 0.5 day |
| 8 | Redis pub/sub event publisher helper | 0.5 day |
| 9 | Health check, structured logging, global error handler | 0.5 day |
| 10 | Seed data scripts for local development | 0.5 day |

**Total: ~6.5 days**

---

## Integration Checklist

> Every item must pass before declaring this module ready for integration.

- [ ] **M2 → Jobs API**: Module 2 can `POST /api/jobs` with a list of normalized jobs and receive stored records with IDs
- [ ] **M2 → Redis Dedup**: Module 2 can read/write `job_seen:{hash}` keys for fast deduplication
- [ ] **M3 → Read Data**: Module 3 can `GET /api/candidates/{id}` and `GET /api/jobs/{id}` to fetch data for scoring
- [ ] **M3 → Store Resumes**: Module 3 can `POST /api/resumes` to store tailored resume versions
- [ ] **M3 → Create Applications**: Module 3 can `POST /api/applications` to create application records
- [ ] **M4 → Status Updates**: Module 4 can `PATCH /api/applications/{id}/status` and the state machine validates transitions
- [ ] **M4 → Rate Limits**: Module 4 can increment and read `rate_limit:{platform}:{candidate_id}` in Redis
- [ ] **M4 → Session Cache**: Module 4 can store/retrieve browser sessions via `session:{candidate_id}:{platform}`
- [ ] **M5 → Analytics**: Module 5 can `GET /api/analytics/summary` and receive correct KPI counts
- [ ] **M5 → Google OAuth**: Module 5 can use tokens from `POST /api/auth/google` to access Gmail API
- [ ] **Events**: All modules can subscribe to Redis pub/sub channels and receive events
- [ ] **Celery**: Workers for each of the 5 queues can be started independently without errors
- [ ] **State Machine**: Invalid status transitions return HTTP 422 with descriptive error message
- [ ] **Duplicate Prevention**: `(candidate_id, job_id)` unique constraint on `applications` prevents duplicate applications
- [ ] **Docker**: `docker compose up` starts PostgreSQL, Redis, FastAPI, and Celery beat/workers
