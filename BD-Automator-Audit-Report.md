# BD-Automator-Agent: Production Readiness Audit Report

**Date:** 2026-06-23  
**Auditor:** Gemini (Claude Sonnet 4.6, Antigravity)  
**System Version:** `3f7d410 Merge branch 'origin/main'` (latest commit)  
**Audit Scope:** backend/ (M1), module2/ (M2), module3/ (M3), module4/ (M4)

---

## EXECUTIVE SUMMARY

The BD-Automator-Agent system is a sophisticated, well-intentioned automation platform that is **not production-ready**. The architecture demonstrates genuine engineering ambition — a vision-driven AgentLoop, deterministic fingerprinting, and a structured state machine — but contains **7 CRITICAL blockers** and **9 HIGH-severity issues** that will cause data loss, security breaches, or system-wide outages under any real production load. The most acute problems are: an unauthenticated synchronous API endpoint (`/prepare-package`) that blocks the main HTTP server for minutes per call; a PYTHONPATH hack that wires all five modules into a single process, creating total cascading failure risk; no container or deployment artifact for the browser automation component; dual `publish_event` functions with different signatures creating silent event routing ambiguity; and no dead-letter queue for permanently failed application tasks. The recommended path to production is a two-phase remediation: first harden the three CRITICAL security and reliability blockers (authentication, async blocking, DLQ), then extract module4 into its own deployable process before declaring MVP.

- **Total Blockers (CRITICAL):** 7 findings  
- **Total HIGH severity:** 9 findings  
- **Total Warnings (MEDIUM + LOW):** 11 findings  
- **Must-fix-before-MVP total:** 18 findings  
- **Verdict: NOT READY for production**

---

## SEVERITY LEGEND

| Level | Definition |
|-------|-----------|
| **CRITICAL** | Will cause data loss, outages, or a security breach in production. Must be fixed before any user touches the system. |
| **HIGH** | Will cause failures under real load or multi-user conditions. Will be triggered within the first week of production use. |
| **MEDIUM** | Degrades reliability or operability but won't cause immediate outages. Causes painful debugging and operational toil. |
| **LOW** | Best-practice gaps, tech debt, observability improvements. Non-blocking but accumulates into larger problems. |

---

## AUDIT FINDINGS

---

### [CRITICAL] FINDING-001: Unauthenticated `/prepare-package` Endpoint Blocks the Entire HTTP Server

**Affected Components:** `backend/app/routers/applications.py` (lines 185–222), `module3/orchestrator.py`

**Description:**  
The `POST /api/applications/prepare-package` endpoint has zero authentication — no `Depends(get_current_user)`, no API key check, nothing. Any unauthenticated HTTP caller can trigger the full Module 3 AI pipeline. More critically, because `prepare_package_for_live_application` is an `async def` coroutine that makes multiple sequential Gemini API calls (scoring, tailoring, cover letter generation — each taking 10–60+ seconds), the FastAPI event loop is **blocked** for the entire duration. Uvicorn is single-process by default; this effectively takes down the entire API for all other requests.

**Production Impact:**  
An anonymous attacker can call this endpoint repeatedly, generating unbounded Gemini API costs and starving all other API requests (status updates, dashboard loads, websocket pings) for minutes at a time. Under normal load with even 2 concurrent applications being processed, the API becomes unresponsive.

**Evidence:**
```python
# backend/app/routers/applications.py, lines 185–207
@router.post("/prepare-package", response_model=PreparePackageResponse)
async def prepare_package(request: PreparePackageRequest):
    """
    Synchronous endpoint for M4 to request custom tailored resume,
    cover letter (if needed), and screening question answers.
    """
    import os
    from module3.orchestrator import prepare_package_for_live_application
    # No auth dependency — any caller can trigger this
    try:
        result = await prepare_package_for_live_application(
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            # ... this awaits multiple 10-60s Gemini calls sequentially
        )
```

**Recommended Fix:**  
1. Add `current_user: User = Depends(get_current_user)` to the endpoint signature immediately.  
2. Move the long-running M3 work into a Celery task (`task:prepare_application_package` already exists in `resume_generation.py`). Have this endpoint enqueue the task and return a `202 Accepted` with a task ID. M4 should poll or be notified via Redis pub/sub when the package is ready.

**Effort Estimate:** M  
**Priority:** Must-fix-before-MVP

---

### [CRITICAL] FINDING-002: Dual `publish_event` Functions — Silent Event Bus Fragmentation

**Affected Components:** `backend/app/services/events.py`, `backend/app/tasks/browser_automation.py` (lines 21–50)

**Description:**  
There are two completely different `publish_event` functions registered in the system with the same name but different behaviors:

1. `app.services.events.publish_event` — publishes to `events:{name}` only (wrapped envelope)
2. `app.tasks.browser_automation.publish_event` — publishes to **both** `event:{name}` AND `events:{name}` (the local definition shadows the imported one)

The module-level `from app.services.events import publish_event` import in `browser_automation.py` is immediately overridden by the local `async def publish_event` defined at line 21 in the same file. Any code that imports `publish_event` from `event_consumer.py` (which re-exports it from `browser_automation`) gets the dual-publish version. Code importing from `app.services.events` gets the single-publish version.

**Production Impact:**  
The event consumer in `module4/tasks/event_consumer.py` subscribes to both `event:application.package_ready` AND `events:application.package_ready`. When M3's orchestrator calls `app.services.events.publish_event("application.package_ready", ...)` (single-publish to `events:...`), the consumer receives it. But when `browser_automation.publish_event` is used, it dual-publishes and the consumer receives **two** messages for the same event, causing duplicate task enqueues and double-applying for the same job.

**Evidence:**
```python
# browser_automation.py lines 11-12 (import, immediately shadowed):
from app.services.events import publish_event  # <-- overridden below

# browser_automation.py lines 21-50 (local definition takes over):
async def publish_event(event_name: str, payload: dict) -> None:
    ...
    await redis_client.publish(f"event:{clean_name}", json.dumps(payload))  # extra channel
    await redis_client.publish(f"events:{clean_name}", json.dumps(wrapped))

# module3/orchestrator.py line 325 uses the single-publish version:
await publish_event("application.package_ready", event_payload)

# event_consumer.py lines 36-38 subscribes to BOTH channels:
await pubsub.subscribe(
    "event:application.package_ready",
    "events:application.package_ready",
)
```

**Recommended Fix:**  
Delete the local `publish_event` from `browser_automation.py`. Extend `app.services.events.publish_event` to dual-publish if needed, or pick one channel convention and enforce it everywhere. Ensure `event_consumer.py` subscribes to exactly one channel.

**Effort Estimate:** S  
**Priority:** Must-fix-before-MVP

---

### [CRITICAL] FINDING-003: PYTHONPATH Hack Couples All Modules into a Single Process — Total Cascade Risk

**Affected Components:** `backend/app/main.py` (lines 5–8), `dev.sh` (line 76), `backend/app/tasks/resume_generation.py` (line 3), `backend/app/celery_app.py` (line 44)

**Description:**  
The project root is inserted into `sys.path` at startup so that `module3`, `module4`, etc. are importable from within the `backend` package. This means any Celery worker — even one intended only for email scanning — imports and initializes ALL module code (Playwright, Gemini clients, PDF parsers) at startup. The Celery configuration in `celery_app.py` includes `"app.tasks.browser_automation"` in the `include` list, which in turn imports `ApplicationExecutor` from the browser automation stack. A crash or import error in ANY module kills ALL Celery workers.

**Production Impact:**  
If `playwright` isn't installed in the production environment, or if any module3/module4 import fails (version mismatch, missing API key at import time, etc.), the entire Celery worker pool fails to start. This isn't a theoretical risk — `module4/requirements.txt` lists `playwright==1.44.*` while `backend/requirements.txt` lists `playwright==1.60.0`, a version mismatch that can cause silent import errors in tightly-pinned environments.

**Evidence:**
```python
# backend/app/main.py lines 5-8:
_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# dev.sh line 76:
export PYTHONPATH="$ROOT:${PYTHONPATH:-}"

# backend/app/celery_app.py lines 38-44:
include=[
    "app.tasks.job_discovery",
    "app.tasks.resume_generation",
    "app.tasks.browser_automation",  # <-- pulls in all of M4
    "app.tasks.email_scan",
    "app.tasks.dynamic_apply",
]

# backend/app/tasks/resume_generation.py line 3:
from module3.orchestrator import orchestrate_application_package  # top-level import from M3
```

**Recommended Fix:**  
Package each module as a proper Python package with its own `setup.py` or `pyproject.toml`. Install them into the shared virtualenv with `pip install -e ./module3 -e ./module4`. Remove the `sys.path` mutation. Separate Celery worker processes: one for `queue:resume_generation` (M3 only), one for `queue:application_execution` (M4 only), each with their own `include` list.

**Effort Estimate:** L  
**Priority:** Must-fix-before-MVP

---

### [CRITICAL] FINDING-004: No Dockerfile or Docker Compose Service for Module 4 Worker

**Affected Components:** `docker-compose.yml`, `module4/` directory

**Description:**  
`module4/` has no `Dockerfile`. The `docker-compose.yml` defines a single `celery_worker` service that uses the `backend/` Dockerfile. The `module4/` directory only contains `requirements.txt`, `__init__.py`, `pyproject.toml`, and `tasks/`. There is no way to deploy the browser automation worker as a container — meaning Playwright's browser binaries, font packages, and system dependencies are not captured anywhere for production deployment.

**Production Impact:**  
When deploying to any cloud environment (EC2, ECS, GCP, etc.), the Playwright worker will fail immediately because `playwright install chromium` was never run in the container image and no image exists that captures the browser binaries. The entire application submission pipeline is dead on arrival in any non-developer machine.

**Evidence:**
```
# docker-compose.yml — no module4-specific service:
services:
  api:        # uses ./backend Dockerfile
  db:         ...
  redis:      ...
  celery_worker:  build: ./backend  # generic backend image, no Playwright
  celery_beat:    build: ./backend  # same
  frontend:   ...

# module4/ directory listing:
module4/
├── __init__.py
├── pyproject.toml
├── requirements.txt    # playwright==1.44.*, etc.
└── tasks/
    ├── celery_app.py
    ├── event_consumer.py
    └── execute_application.py
# NO Dockerfile present
```

**Recommended Fix:**  
Create `module4/Dockerfile` based on `mcr.microsoft.com/playwright/python:v1.44.0` (Microsoft's pre-built Playwright image with all system dependencies). Add a `module4_worker` service to `docker-compose.yml` with the correct environment variables and queue subscription (`-Q queue:application_execution` only). Remove browser_automation tasks from the main `celery_worker` service.

**Effort Estimate:** M  
**Priority:** Must-fix-before-MVP

---

### [CRITICAL] FINDING-005: No Dead Letter Queue — Permanently Failed Tasks Are Silently Dropped

**Affected Components:** `backend/app/celery_app.py`, `backend/app/tasks/browser_automation.py` (lines 294–301)

**Description:**  
Celery is configured with `broker=settings.redis_url` and `backend=settings.redis_url`, but there is no dead letter queue (DLQ) configuration. When `execute_application` exhausts its 3 retries, the exception is re-raised one final time (`raise self.retry(exc=exc)` at line 301), but after max retries are exceeded, Celery moves the task result to the result backend as a FAILURE state and the message is dropped. There is no queue, storage location, or alert that captures permanently failed application tasks for human review.

**Production Impact:**  
In production, applications will silently fail after 3 retries (1h45m total wait). The operator will have no visibility that they failed until checking the database and finding applications stuck in `APPLICATION_STARTED` or `QUEUED` state indefinitely. There is no way to replay the task or inspect the original payload without manual database archaeology.

**Evidence:**
```python
# browser_automation.py lines 292-301:
    except Retry:
        raise
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            asyncio.run(publish_application_failed(
                application_id=package_dict.get("application_id", ""),
                error=str(exc),
                retry_eligible=False
            ))
        raise self.retry(exc=exc)  # <-- after max_retries, Celery drops the task
# No DLQ configuration in celery_app.py
```

**Recommended Fix:**  
Add a DLQ queue to `celery_app.py`:
```python
celery_app.conf.task_queues.append(Queue("queue:dead_letter"))
celery_app.conf.task_reject_on_worker_lost = True
celery_app.conf.task_acks_late = True
```
In the `execute_application` exception handler after max retries, explicitly route to the DLQ:
```python
execute_application_dead.apply_async(args=[package_dict], queue="queue:dead_letter")
```
Store dead-lettered tasks in a separate DB table for human review and reprocessing.

**Effort Estimate:** M  
**Priority:** Must-fix-before-MVP

---

### [CRITICAL] FINDING-006: Application Status Can Get Permanently Stuck — No Timeout Recovery

**Affected Components:** `backend/app/services/state_machine.py`, `backend/app/browser_automation/services/state_machine.py`, `backend/app/celery_app.py`

**Description:**  
When a Celery worker crashes while a task is mid-execution (e.g., Playwright kills the process, OOM killer fires), the application record remains in `APPLICATION_STARTED` status permanently. The state machine's `validate_transition` function only blocks invalid transitions — it has no concept of "stuck" detection. There is no Celery Beat task that scans for applications that have been in `APPLICATION_STARTED` or `QUEUED` for more than N minutes and either retries them or marks them `FAILED`.

The `FAILED` and `REJECTED` states are correctly declared as terminal (`[]` allowed transitions), but there is no mechanism to reach them when the worker disappears.

**Production Impact:**  
After any worker crash (which Playwright sessions are notorious for causing), applications accumulate in `APPLICATION_STARTED` forever. The dashboard shows them as "in progress" when they are not. The candidate misses application windows. Manual DB cleanup is required to unstick them.

**Evidence:**
```python
# state_machine.py lines 17-21:
"FAILED":              [],
"BLOCKED":             ["QUEUED"],   # Blocked apps can be retried
"REJECTED":            [],
"OFFER":               [],

# No Celery Beat task defined in celery_app.py to recover stuck applications:
celery_app.conf.beat_schedule = {
    "discover-jobs-every-6h": { ... },
    "scan-inbox-every-15m": { ... },
    "refresh-analytics-every-1h": { ... },
}
# Missing: "recover-stuck-applications-every-30m"
```

**Recommended Fix:**  
Add a Celery Beat task `task:recover_stuck_applications` scheduled every 30 minutes. It should query the database for applications with `status IN ('APPLICATION_STARTED', 'QUEUED')` and `updated_at < NOW() - INTERVAL '45 minutes'`. For each, either re-enqueue `execute_application` (if `retry_count < 3`) or force-transition to `FAILED`.

**Effort Estimate:** M  
**Priority:** Must-fix-before-MVP

---

### [CRITICAL] FINDING-007: `asyncio.get_event_loop()` in Celery Task — Will Fail on Python 3.12

**Affected Components:** `backend/app/tasks/resume_generation.py` (lines 14–19)

**Description:**  
The `prepare_application_package` Celery task calls `asyncio.get_event_loop()` to run the async orchestrator. In Python 3.10+, calling `get_event_loop()` in a non-main thread (which is what Celery workers use) raises a `DeprecationWarning` and in 3.12+ raises a `RuntimeError: There is no current event loop in thread`. The rest of the codebase correctly uses `asyncio.run()` (browser_automation.py lines 276, 282, 296), making this inconsistency a latent crash.

**Production Impact:**  
Any invocation of `task:prepare_application_package` on Python 3.12 (which `dev.sh` uses — `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3`) will raise `RuntimeError` and crash the task without a retry on the first call. This kills the M3 pipeline path entirely.

**Evidence:**
```python
# resume_generation.py lines 14-19:
loop = asyncio.get_event_loop()            # deprecated/broken in Python 3.12
result = loop.run_until_complete(
    orchestrate_application_package(
        candidate_id=str(candidate_id),
        job_id=str(job_id)
    )
)
```

**Recommended Fix:**  
Replace with `asyncio.run(orchestrate_application_package(...))` — exactly as `browser_automation.py` does on line 276.

**Effort Estimate:** S  
**Priority:** Must-fix-before-MVP

---

### [HIGH] FINDING-008: Synchronous M3 Call Blocks M4's Playwright Browser Thread

**Affected Components:** `backend/app/browser_automation/services/executor.py` (lines 400–445)

**Description:**  
During form filling, `executor.py` calls `POST /api/applications/prepare-package` via `httpx.AsyncClient` with a 300-second timeout (line 403). This is an HTTP call back to the same FastAPI process, which then calls the full M3 Gemini pipeline (scoring, tailoring, cover letter). While this await is pending, the Playwright browser session is open, consuming memory, holding a browser process, and unable to make forward progress. With `--concurrency=2` (dev.sh line 165), two concurrent applications exhausts the worker pool entirely.

**Production Impact:**  
At 10 concurrent applications, all workers are blocked on M3 HTTP calls for 2-10 minutes each. The `queue:application_execution` queue grows unboundedly. Browser sessions accumulate in memory (each Playwright browser context uses 100-300MB). The worker process OOMs and crashes, leaving all in-flight applications stuck (see FINDING-006).

**Evidence:**
```python
# executor.py lines 400-412:
if open_questions:
    api_base = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api")
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:  # 5-minute timeout!
            resp = await client.post(
                f"{api_base}/applications/prepare-package",
                json={
                    "candidate_id": package.candidate_id,
                    "job_id": package.job_id,
                    ...
                },
            )
```

**Recommended Fix:**  
Pre-compute screening answers in the M3 pipeline before the `event:application.package_ready` event is fired. The event payload should carry `screening_answers` (it already has this field in `orchestrator.py` line 323). Eliminate the mid-execution callback to `/prepare-package` from `executor.py` entirely.

**Effort Estimate:** M  
**Priority:** Must-fix-before-MVP

---

### [HIGH] FINDING-009: No Redis Connection Pooling — New Connection Created Per Event Publish

**Affected Components:** `backend/app/tasks/browser_automation.py` (lines 21–50), `module4/tasks/event_consumer.py` (line 34)

**Description:**  
The local `publish_event` in `browser_automation.py` creates a new `aioredis.from_url()` connection object on every call and closes it in a `finally` block. This means every event publish (which happens ~6 times per application: progress, status_changed, submitted, failed, etc.) opens and closes a Redis TCP connection. The shared `redis_client` in `backend/app/redis_client.py` has no pool size limit configured (`pool_size` defaults to 10 in redis-py, but no `max_connections` is set).

**Production Impact:**  
At 50 concurrent applications, each publishing 6 events = 300 Redis connection open/close cycles. On cloud Redis (Upstash, Redis Cloud), each new connection incurs latency and can hit connection limits. Connection exhaustion returns `ConnectionError` from Redis, which crashes the event publish silently (the `finally: await redis_client.aclose()` masks errors).

**Evidence:**
```python
# browser_automation.py lines 27-50:
async def publish_event(event_name: str, payload: dict) -> None:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    redis_client = aioredis.from_url(redis_url, **kwargs)  # new connection every call
    try:
        ...
        await redis_client.publish(...)
        await redis_client.publish(...)
    finally:
        await redis_client.aclose()   # immediate close
```

**Recommended Fix:**  
Use the shared singleton `redis_client` from `app.redis_client` everywhere. Configure explicit pool sizing: `redis.from_url(url, max_connections=20)`. The `publish_event` in `browser_automation.py` should be deleted and replaced with the canonical one from `app.services.events`.

**Effort Estimate:** S  
**Priority:** Must-fix-before-MVP

---

### [HIGH] FINDING-010: Database `echo=True` in Production — Full SQL Logged to stdout, PII Exposure

**Affected Components:** `backend/app/database.py` (line 15)

**Description:**  
SQLAlchemy's async engine is created with `echo=True`, which logs every SQL statement — including all SELECT/INSERT/UPDATE queries with their bound parameters — to stdout/stderr. This is a development debugging aid that is catastrophically inappropriate for production.

**Production Impact:**  
All SQL queries including those containing candidate PII (names, emails, phone numbers), resume content, and job application data are written to the application log in plaintext. In any cloud environment where logs are shipped to a log aggregation service (CloudWatch, Datadog, etc.), this constitutes a PII data breach. It also significantly degrades query performance (the echo path serializes and formats every query) and floods the log volume with noise.

**Evidence:**
```python
# database.py line 13-18:
engine = create_async_engine(
    DATABASE_URL, 
    echo=True,      # <-- logs ALL SQL including parameter values
    future=True,
    pool_pre_ping=True,
    pool_recycle=1800
)
```

**Recommended Fix:**  
`echo=True` must become `echo=False` (or `echo=settings.debug_sql` where `debug_sql` defaults to `False`). This is a one-line fix.

**Effort Estimate:** S  
**Priority:** Must-fix-before-MVP

---

### [HIGH] FINDING-011: No Process Supervisor — Any Worker Crash Requires Manual Restart

**Affected Components:** `dev.sh`, `docker-compose.yml`

**Description:**  
The `dev.sh` script starts the Celery worker as a background process with `&`. If the worker crashes (which Playwright sessions routinely cause through memory pressure, browser process kills, or unhandled exceptions), the process dies silently. The `stream_log` helper just tails the log file — it does not detect worker death or restart it. The `docker-compose.yml` defines Celery worker/beat services without `restart: always` or any health check.

**Production Impact:**  
A Playwright crash (which will happen — every third or fourth application attempt has some probability of crashing the browser process) kills the application execution worker. All subsequent `task:execute_application` tasks queue up in Redis, never execute, and eventually timeout. The operator has no alert and may not discover this for hours.

**Evidence:**
```yaml
# docker-compose.yml — no restart policy:
celery_worker:
  build: ./backend
  command: celery -A app.celery_app worker --loglevel=info
  environment:
    - DATABASE_URL=...
    - REDIS_URL=...
  depends_on:
    - db
    - redis
  volumes:
    - ./backend:/app
# Missing: restart: always, healthcheck:
```

**Recommended Fix:**  
Add `restart: always` to all Celery services in `docker-compose.yml`. For the module4 worker specifically (once containerized per FINDING-004), add a health check that verifies the worker is consuming from `queue:application_execution`. For production, use Kubernetes Deployment restartPolicy or ECS task restart policies.

**Effort Estimate:** S  
**Priority:** Must-fix-before-MVP

---

### [HIGH] FINDING-012: CORS `allow_origins` Hardcoded to `localhost` — Will Block Production Frontend

**Affected Components:** `backend/app/main.py` (lines 35–41)

**Description:**  
The FastAPI CORS middleware is configured with `allow_origins=["http://localhost:3000"]`. This is hardcoded, not configurable via environment variable. When the frontend is deployed to any production domain (e.g., `https://app.bdautomator.com`), all cross-origin requests from the browser will be blocked with a CORS error.

**Production Impact:**  
The entire frontend becomes non-functional from any production deployment. Login, dashboard data, and application status updates all require CORS-allowed cross-origin fetch calls. 100% of users are locked out.

**Evidence:**
```python
# main.py lines 35-41:
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # hardcoded, not env-configurable
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

**Recommended Fix:**  
Add `cors_origins: list[str] = ["http://localhost:3000"]` to `Settings` in `config.py`, read from `CORS_ORIGINS` env var (comma-separated list). Pass `settings.cors_origins` to the middleware.

**Effort Estimate:** S  
**Priority:** Must-fix-before-MVP

---

### [HIGH] FINDING-013: Rate Limiter Has TOCTOU Race Condition — Limit Exceeded Under Concurrency

**Affected Components:** `backend/app/browser_automation/services/rate_limiter.py` (lines 31–47)

**Description:**  
The `RateLimiter.check_and_increment` method reads current counts, checks them against limits, then increments if allowed. Under concurrent Celery workers, two workers can both read `count=9` (limit=10), both pass the check, both increment to 10 and 11, resulting in one over-limit application being sent. The pipeline read at lines 31-33 is also broken — a `pipeline` context manager is opened but `redis.get()` is called directly (not via the pipeline), making the pipeline a no-op.

**Production Impact:**  
Under 2+ concurrent workers processing the same candidate+platform combination, the rate limit is consistently exceeded by 1-N applications per hour window. For LinkedIn (limit=10/hour), this leads to account suspension.

**Evidence:**
```python
# rate_limiter.py lines 30-47:
async with self.redis.pipeline() as pipe:      # pipeline opened but never used
    hourly_count = await self.redis.get(hourly_key)   # direct, not pipelined
    daily_count  = await self.redis.get(daily_key)    # direct, not pipelined

hourly_count = int(hourly_count or 0)
daily_count  = int(daily_count  or 0)

if hourly_count >= limits["hourly"]:           # check (read)
    return False
...
await self.redis.incr(hourly_key)              # increment (separate round trip) — TOCTOU gap
```

**Recommended Fix:**  
Use a Lua script executed atomically via `redis.eval()` to check-and-increment in a single atomic operation:
```lua
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
if count > tonumber(ARGV[2]) then return 0 end
return 1
```

**Effort Estimate:** S  
**Priority:** Must-fix-before-MVP

---

### [HIGH] FINDING-014: `list_applications` Endpoint Has No Pagination — Full Table Scan Per Request

**Affected Components:** `backend/app/routers/applications.py` (lines 72–77), `backend/app/tasks/dynamic_apply.py` (line 90)

**Description:**  
`GET /api/applications` performs `SELECT * FROM applications` with no `WHERE`, no `LIMIT`, no `OFFSET`. In `dynamic_apply.py` (line 90), this endpoint is called with a `candidate_id` query param that the router doesn't implement — the router ignores all query params and returns all records from all candidates.

**Production Impact:**  
With 1000 applications in the database (reachable within the first week of operation), this query returns the full table to every caller. `dynamic_apply._already_applied_job_ids()` calls this endpoint on every `task:dynamic_apply` run. Memory pressure on both the API server and the Celery worker is unbounded. Response times grow linearly with application count.

**Evidence:**
```python
# applications.py lines 72-77:
@router.get("", response_model=List[ApplicationResponse])
async def list_applications(
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Application))  # full table scan, no limit
    return result.scalars().all()

# dynamic_apply.py lines 88-94:
async def _already_applied_job_ids(client, candidate_id):
    r = await client.get(f"{API_BASE}/applications",
                         params={"candidate_id": candidate_id})  # param is ignored by router!
```

**Recommended Fix:**  
Add `candidate_id: Optional[UUID] = None`, `status: Optional[str] = None`, `limit: int = 100`, `offset: int = 0` parameters to the endpoint. Apply `WHERE candidate_id = :cid` filter when provided. Add a DB index on `applications.candidate_id`.

**Effort Estimate:** S  
**Priority:** Must-fix-before-MVP

---

### [HIGH] FINDING-015: No Circuit Breaker on Gemini API Calls — Cascade Failure When API Is Down

**Affected Components:** `module3/orchestrator.py`, `module3/scoring/`, `module3/tailoring/`, `module3/cover_letter/`

**Description:**  
All Gemini API calls in Module 3 are made without a circuit breaker. While `tenacity` is listed in `backend/requirements.txt` (v9.1.4), it is not used in the M3 orchestration code. When Gemini is rate-limited (HTTP 429) or unavailable (HTTP 503), the orchestrator raises an unhandled exception, the application state machine is left in `MATCHED` or `RESUME_UPDATED`, and the Celery task retries up to 3 times — hammering an already-struggling API.

**Production Impact:**  
During Gemini outages (which occur during quota limit periods), all in-flight M3 tasks fail simultaneously. All applications in the pipeline are stuck in mid-pipeline states. No exponential backoff is applied to Gemini calls themselves, only to the Celery task, which means rapid re-submission during rate limiting worsens the situation.

**Evidence:**
```python
# orchestrator.py lines 221-231:
tasks = [
    tailor_resume(resume_data, job, candidate, version=next_version),
    generate_cover_letter(resume_data, job, candidate)
]
results = await asyncio.gather(*tasks)  # no circuit breaker, no tenacity retry
# If Gemini is rate-limited, this raises immediately and the application
# state is left in MATCHED (intermediate state) with no recovery path
```

**Recommended Fix:**  
Wrap all Gemini API calls with `@retry(wait=wait_exponential(min=4, max=60), stop=stop_after_attempt(5), retry=retry_if_exception_type(ResourceExhausted))` from `tenacity`. Implement a module-level circuit breaker that opens after 5 consecutive failures and half-opens after 60 seconds.

**Effort Estimate:** M  
**Priority:** Fix-in-MVP

---

### [HIGH] FINDING-016: `dynamic_apply` Creates Applications in `QUEUED` Status — Bypasses M3 Pipeline

**Affected Components:** `backend/app/tasks/dynamic_apply.py` (line 103)

**Description:**  
`_create_application()` in `dynamic_apply.py` posts `{"status": "QUEUED"}` directly when creating new application records. The canonical pipeline requires: `FOUND → ANALYZED → MATCHED → RESUME_UPDATED → COVER_LETTER_CREATED → QUEUED`. Jumping directly to `QUEUED` means these applications go to M4 with no tailored resume, no cover letter, no fit score, and no screening answers. The `hydrate_and_execute` function attempts to recover by fetching the latest resume from the DB, but falls back to the base (untailored) resume.

**Production Impact:**  
Applications sourced via `task:dynamic_apply` are submitted with the candidate's base resume, not a tailored one. This directly reduces application quality and defeats the core value proposition of the system. Fit scores are `null` in the database for these applications.

**Evidence:**
```python
# dynamic_apply.py lines 98-111:
async def _create_application(
    client: httpx.AsyncClient, candidate_id: str, job_id: str
) -> Optional[Dict[str, Any]]:
    r = await client.post(
        f"{API_BASE}/applications",
        json={"candidate_id": candidate_id, "job_id": job_id, "status": "QUEUED"},
        # Missing: pipeline through M3 — no tailoring, no scoring, no cover letter
    )
```

**Recommended Fix:**  
Change `_create_application` to create the record in `FOUND` status, then dispatch `task:prepare_application_package` (the M3 Celery task) rather than `task:execute_application` directly. Only after M3 completes and publishes `event:application.package_ready` should M4 be triggered.

**Effort Estimate:** M  
**Priority:** Fix-in-MVP

---

### [MEDIUM] FINDING-017: Playwright Version Mismatch Between module4 and backend

**Affected Components:** `module4/requirements.txt` (line 1), `backend/requirements.txt` (line 137)

**Description:**  
`module4/requirements.txt` specifies `playwright==1.44.*` while `backend/requirements.txt` pins `playwright==1.60.0`. Both are installed into the same virtual environment (`.venv/` at the project root). Pip will resolve to one version, with the other silently losing. Since `backend/requirements.txt` is the one that gets installed (it's the main requirements file), `playwright==1.60.0` wins. The `module4/requirements.txt` file is misleading and will cause confusion about what version is actually running.

**Evidence:**
```
# module4/requirements.txt line 1:
playwright==1.44.*

# backend/requirements.txt line 137:
playwright==1.60.0
```

**Recommended Fix:**  
Remove `module4/requirements.txt` and consolidate into `backend/requirements.txt`. Or, if module4 is ever extracted (FINDING-004), pin to the same version in both files and add a CI check.

**Effort Estimate:** S  
**Priority:** Fix-in-MVP

---

### [MEDIUM] FINDING-018: `state_machine.py` Allows Same-Status Transitions — Creates Duplicate History Rows

**Affected Components:** `backend/app/services/state_machine.py` (line 29)

**Description:**  
`validate_transition` explicitly allows same-status transitions as a pass-through condition (`target != current` check). This means any module can re-transition an application to its current status (e.g., `SUBMITTED → SUBMITTED`), which creates duplicate `application_history` entries and double-fires `event:application.status_changed`. For idempotent retries this is convenient, but it masks bugs where the same status event is published multiple times.

**Evidence:**
```python
# state_machine.py line 29:
if target not in allowed and target != current:  # same-status is always allowed
    raise InvalidTransitionError(...)
```

**Recommended Fix:**  
Log a warning (not an error) on same-status transitions. Do not write a duplicate `application_history` row. Return the current application state without committing a new history entry.

**Effort Estimate:** S  
**Priority:** Fix-in-MVP

---

### [MEDIUM] FINDING-019: Hardcoded `api_base_url = "http://127.0.0.1:8000"` Breaks M3 in Docker

**Affected Components:** `module3/orchestrator.py` (lines 31, 346)

**Description:**  
Both `orchestrate_application_package` and `prepare_package_for_live_application` default to `api_base_url="http://127.0.0.1:8000"`. When these functions are called from a Celery worker running in a Docker container (as in `docker-compose.yml`), `127.0.0.1` resolves to the container's own loopback, not the `api` service. The API calls fail with `ConnectionRefusedError` immediately.

**Evidence:**
```python
# orchestrator.py line 31:
async def orchestrate_application_package(
    ...
    api_base_url: str = "http://127.0.0.1:8000",  # fails in Docker
```

**Recommended Fix:**  
Remove the default value. Read from `os.getenv("M1_API_BASE_URL", "http://localhost:8000")` and always pass it explicitly. The `M1_API_BASE_URL` env var is already used in `browser_automation.py` (line 107) and `dynamic_apply.py` (line 29).

**Effort Estimate:** S  
**Priority:** Fix-in-MVP

---

### [MEDIUM] FINDING-020: Structured Logging Not Used — Global Exception Handler Leaks Internal Details to HTTP Callers

**Affected Components:** `backend/app/main.py` (lines 70–76)

**Description:**  
The global exception handler returns `str(exc)` as the HTTP response body. This leaks internal error details, file paths, and potentially stack fragments to unauthenticated callers. The system has `loguru`, `python-json-logger`, and `structlog` all installed but none configured at the application level. Logging throughout the codebase is a mix of `print()` statements (orchestrator.py uses `print()` throughout) and `logging.getLogger(__name__)`.

**Evidence:**
```python
# main.py lines 70-76:
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    import traceback, sys
    print(f"GLOBAL ERROR: {type(exc)} {exc}", file=sys.stderr)  # print, not logger
    traceback.print_exc(file=sys.stderr)
    return JSONResponse(status_code=500, content={
        "detail": "Internal Server Error",
        "msg": str(exc)    # leaks internal exception message to callers
    })
```

**Recommended Fix:**  
Configure `structlog` at application startup with JSON formatting and correlation IDs. Remove `str(exc)` from HTTP responses (return only `"Internal Server Error"`). Replace all `print()` in `orchestrator.py` with `structlog.get_logger().info()`. Propagate a `request_id` header through all internal calls for distributed tracing.

**Effort Estimate:** M  
**Priority:** Fix-in-MVP

---

### [MEDIUM] FINDING-021: No Supabase Row-Level Security Assessment — Unscoped Multi-Tenant Data Access

**Affected Components:** Supabase database configuration (not fully auditable from code alone)

**Description:**  
The database schema specifies a multi-user system with `candidate_id` foreign keys on `applications`, `resumes`, `jobs`, etc. The backend uses a single service-role Supabase connection (`supabase_service_role_key` in `config.py`) which bypasses all RLS policies. Whether RLS is configured in Supabase at all cannot be determined from the codebase — it requires direct DB inspection. The `resumes.py` and `applications.py` routers make no tenant-scoping check — they return data for any `candidate_id` passed in the path without verifying the calling user owns that candidate.

**Evidence:**
```python
# config.py line 20:
supabase_service_role_key: str = ""  # bypasses all RLS when used

# applications.py lines 79-90 — no ownership check:
@router.get("/{application_id}", response_model=ApplicationResponse)
async def get_application(
    application_id: UUID,
    db: AsyncSession = Depends(get_db)  # no current_user dependency
):
    result = await db.execute(
        select(Application).where(Application.id == application_id)
    )  # returns ANY application to ANY authenticated caller
```

**Recommended Fix:**  
Enable RLS on all tables in Supabase. Add router-level authorization checks that verify the requesting user owns the resource (`WHERE candidate_id IN (SELECT id FROM candidates WHERE user_id = :current_user_id)`). This is required before any multi-tenant deployment.

**Effort Estimate:** L  
**Priority:** Must-fix-before-MVP

---

### [MEDIUM] FINDING-022: Screenshot Bucket Uses Public Reads — Candidate PII Exposed via Predictable URLs

**Affected Components:** `backend/app/browser_automation/services/screenshot.py` (lines 81, 94)

**Description:**  
Screenshots are uploaded to Supabase Storage and the returned URL is the **public** path: `/storage/v1/object/public/{bucket}/{remote_name}`. Screenshots of submitted applications include filled form data: candidate's full name, email, phone number, resume content visible in upload confirmation modals, and potentially SSN fields on some ATS systems. These are accessible to anyone with the URL, which follows a predictable pattern: `{application_id}/{timestamp}.png`.

**Evidence:**
```python
# screenshot.py lines 81, 94:
upload_url = f"{project_url}/storage/v1/object/{bucket}/{remote_name}"
...
public_url = f"{project_url}/storage/v1/object/public/{bucket}/{remote_name}"
# "public" path — no auth required to view
```

**Recommended Fix:**  
Use private bucket + signed URLs. Upload using the service role key (not anon key). Generate signed URLs with `supabase.storage.from_(bucket).create_signed_url(path, expires_in=3600)` for display in the dashboard. Do NOT store the raw public URL in the database.

**Effort Estimate:** M  
**Priority:** Must-fix-before-MVP

---

### [MEDIUM] FINDING-023: Bare `except: pass` Masks Critical Browser Automation Failures

**Affected Components:** `backend/app/browser_automation/services/executor.py` (lines 231, 274–275, 284–286, 629–631, 649–650, 667–675)

**Description:**  
Multiple `except Exception: pass` and `except Exception: continue` patterns throughout the executor silence errors that could indicate critical browser automation failures. Particularly concerning are the stealth application failure (line 231) and the `wait_for_load_state` failures (lines 284, 630) — if the stealth patches fail silently, the browser is not stealthed and will be detected immediately by platforms like LinkedIn.

**Evidence:**
```python
# executor.py lines 227-232:
try:
    from playwright_stealth import Stealth
    await Stealth().apply_stealth_async(page)
    logger.info("[M4] playwright-stealth v2 applied")
except Exception as exc:
    logger.debug(f"[M4] playwright-stealth unavailable: {exc}")
    # Continues without stealth — will be detected as bot on LinkedIn

# executor.py lines 274-275 (inside apply_button click loop):
            except Exception:
                continue   # silent failure, no logging
```

**Recommended Fix:**  
Demote stealth failure from `debug` to `warning`. If `playwright-stealth` is unavailable and `USE_STEALTH` is `true`, return `BLOCKED` status rather than continuing. Remove bare `except: pass` patterns and replace with specific exception types with proper logging.

**Effort Estimate:** S  
**Priority:** Fix-in-MVP

---

### [LOW] FINDING-024: No Celery Worker Health Monitoring (No Flower or Equivalent)

**Affected Components:** `docker-compose.yml`, `dev.sh`

**Description:**  
There is no Celery monitoring tool deployed. Flower (the standard Celery monitoring dashboard) is not in `requirements.txt`, not in `docker-compose.yml`, and not mentioned anywhere in the codebase. The operator has no visibility into: queue depths, active tasks, task failure rates, worker heartbeats, or task execution times.

**Evidence:**  
`backend/requirements.txt` — `flower` not present.  
`docker-compose.yml` — no `flower` service defined.

**Recommended Fix:**  
Add `flower==2.*` to `backend/requirements.txt`. Add a `flower` service to `docker-compose.yml`:
```yaml
flower:
  build: ./backend
  command: celery -A app.celery_app flower --port=5555
  ports: ["5555:5555"]
```
Or use Prometheus + `celery-exporter` for production-grade metrics.

**Effort Estimate:** S  
**Priority:** Fix-in-MVP

---

### [LOW] FINDING-025: No Distributed Tracing Across Module Boundaries

**Affected Components:** All modules

**Description:**  
There is no trace ID or correlation ID propagated across the M1→M3→M4 pipeline. When an application fails in M4, there is no way to correlate the M4 error with the M3 Gemini call that produced the resume, or the M2 job discovery that found the posting. Each module logs independently. Debugging a failed application requires manually correlating `application_id` across four separate log files.

**Recommended Fix:**  
Use `opentelemetry-sdk` with Jaeger or Honeycomb. Propagate `x-trace-id: {application_id}` as an HTTP header on all internal API calls. Inject it as a logging context in Celery tasks via `celery.signals.task_prerun`.

**Effort Estimate:** L  
**Priority:** Post-MVP

---

### [LOW] FINDING-026: `dev.sh` Has Hardcoded Absolute Paths — Non-Portable

**Affected Components:** `dev.sh` (lines 20–21)

**Description:**  
`dev.sh` hardcodes the Python interpreter path to `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3` and Node binary path to `$HOME/.nvm/versions/node/v20.19.5/bin`. These paths are specific to the author's machine and will fail for any other developer or in any CI/CD environment.

**Evidence:**
```bash
# dev.sh lines 20-21:
PYTHON="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
NODE_BIN="$HOME/.nvm/versions/node/v20.19.5/bin"
```

**Recommended Fix:**  
Replace with `PYTHON="${PYTHON:-$(which python3)}"` and `NODE_BIN="${NODE_BIN:-$(dirname $(which node))}"`. Add a `README.md` section documenting required Python and Node versions.

**Effort Estimate:** S  
**Priority:** Fix-in-MVP

---

### [LOW] FINDING-027: Test/Debug Scripts and PII Files Committed to Project Root

**Affected Components:** Project root directory

**Description:**  
The project root contains multiple test, debug, and cleanup scripts: `test_m4_ai_first_real.py` (29,960 bytes), `test_modules_1_4.py` (22,502 bytes), `test_pipeline.py`, `test_capsolver.py`, `diag_consent.py`, `diag_fields.py`, `check_buckets.py`, `fix_buckets.py`, `scratch.py`, `scratch2.py`, `upload_resume.py`, `dummy.pdf`, a real resume PDF (`Sabih Haider — Software Engineer...pdf`), and a `scraped_jobs.json` (207,377 bytes of raw job data).

**Production Impact:**  
`fix_buckets.py` and `check_buckets.py` appear to modify Supabase storage buckets. If run accidentally in production they could delete or corrupt live data. The real resume PDF is a PII leak in the git repository. `scraped_jobs.json` is 202KB of unfiltered raw data that may contain PII.

**Recommended Fix:**  
Move all test/debug scripts to a `scripts/dev/` directory. Remove all PDF files and `scraped_jobs.json` from the repository. Add them to `.gitignore`. Audit git history for committed secrets using `git-filter-repo`.

**Effort Estimate:** S  
**Priority:** Fix-in-MVP

---

## FINDINGS MATRIX

| ID | Title | Severity | Component | Effort | Priority |
|----|-------|----------|-----------|--------|----------|
| FINDING-001 | Unauthenticated `/prepare-package` blocks HTTP server | CRITICAL | M1 backend, M3 | M | Must-fix-before-MVP |
| FINDING-002 | Dual `publish_event` functions fragment event bus | CRITICAL | M1 backend, M4 | S | Must-fix-before-MVP |
| FINDING-003 | PYTHONPATH hack couples all modules in single process | CRITICAL | All | L | Must-fix-before-MVP |
| FINDING-004 | No Dockerfile for Module 4 worker | CRITICAL | M4, DevOps | M | Must-fix-before-MVP |
| FINDING-005 | No Dead Letter Queue for failed tasks | CRITICAL | M1 Celery | M | Must-fix-before-MVP |
| FINDING-006 | Applications permanently stuck on worker crash | CRITICAL | M1 state machine | M | Must-fix-before-MVP |
| FINDING-007 | `asyncio.get_event_loop()` crashes Python 3.12 | CRITICAL | M1 tasks | S | Must-fix-before-MVP |
| FINDING-008 | M3 call blocks M4 Playwright thread for 5 minutes | HIGH | M4 executor | M | Must-fix-before-MVP |
| FINDING-009 | New Redis connection per event publish | HIGH | M1 tasks, M4 | S | Must-fix-before-MVP |
| FINDING-010 | `echo=True` in SQLAlchemy engine leaks PII to logs | HIGH | M1 database | S | Must-fix-before-MVP |
| FINDING-011 | No process supervisor — worker crash = manual restart | HIGH | DevOps | S | Must-fix-before-MVP |
| FINDING-012 | CORS hardcoded to localhost — production frontend blocked | HIGH | M1 backend | S | Must-fix-before-MVP |
| FINDING-013 | Rate limiter TOCTOU race — limits exceeded under concurrency | HIGH | M4 rate_limiter | S | Must-fix-before-MVP |
| FINDING-014 | `list_applications` full table scan, no pagination | HIGH | M1 router | S | Must-fix-before-MVP |
| FINDING-015 | No circuit breaker on Gemini API calls | HIGH | M3 | M | Fix-in-MVP |
| FINDING-016 | `dynamic_apply` creates apps in QUEUED, bypasses M3 | HIGH | M4 dynamic_apply | M | Fix-in-MVP |
| FINDING-017 | Playwright version mismatch (1.44 vs 1.60) | MEDIUM | M4, backend | S | Fix-in-MVP |
| FINDING-018 | Same-status transitions create duplicate history rows | MEDIUM | M1 state machine | S | Fix-in-MVP |
| FINDING-019 | Hardcoded `127.0.0.1` breaks M3 in Docker | MEDIUM | M3 orchestrator | S | Fix-in-MVP |
| FINDING-020 | No structured logging; tracebacks leaked to HTTP callers | MEDIUM | M1 backend | M | Fix-in-MVP |
| FINDING-021 | No RLS; unscoped multi-tenant data access | MEDIUM | DB, M1 | L | Must-fix-before-MVP |
| FINDING-022 | Screenshot bucket uses public reads — PII exposure | MEDIUM | M4 screenshot | M | Must-fix-before-MVP |
| FINDING-023 | Bare `except: pass` masks critical stealth failures | MEDIUM | M4 executor | S | Fix-in-MVP |
| FINDING-024 | No Celery monitoring (no Flower) | LOW | DevOps | S | Fix-in-MVP |
| FINDING-025 | No distributed tracing across module boundaries | LOW | All | L | Post-MVP |
| FINDING-026 | `dev.sh` hardcoded absolute paths — non-portable | LOW | DevOps | S | Fix-in-MVP |
| FINDING-027 | Test scripts and PII files in project root | LOW | All | S | Fix-in-MVP |

---

### Summary Counts

- **Total CRITICAL:** 7  
- **Total HIGH:** 9  
- **Total MEDIUM:** 7  
- **Total LOW:** 4  
- **Must-fix-before-MVP:** 18 findings

---

## SCALABILITY ANALYSIS

### Current Architecture Limits

| Bottleneck | Current Limit | Breaks At | Failure Mode |
|------------|--------------|-----------|--------------|
| Playwright sessions per worker | 1 per task (sequential via `asyncio.run`) | 3 concurrent applications | Third task waits in queue; no parallelism within a worker |
| Celery worker concurrency | 2 (`dev.sh --concurrency=2`) | 3 simultaneous M4 tasks | Queue backlog; applications wait indefinitely |
| M3 blocking HTTP call from M4 | 300s timeout per call | 2 concurrent applications | Both workers blocked on M3; entire queue stalls |
| Redis connections (publish_event) | 1 new TCP connection per publish | ~100 events/min | Connection pool exhaustion on cloud Redis |
| Supabase DB pool | asyncpg default (5 connections) | 6 concurrent API requests | `asyncpg.TooManyConnectionsError` |
| `list_applications` full scan | No limit | ~500 applications | Response time >5s; OOM on large datasets |
| Gemini API rate limit | ~60 RPM free tier | 5 concurrent M3 pipelines | HTTP 429; cascading M3 failures |
| Screenshot local storage (`./screenshots`) | Disk space only | ~10,000 screenshots | `ENOSPC`; executor crash; application stuck |
| Auth token cache (`_token_cache` dict) | Unbounded in-memory dict | Thousands of unique tokens/day | Memory leak; OOM on auth router |

### Scaling Roadmap

**MVP (1-10 concurrent users, <50 applications/day)**

The architecture can support this tier with the following changes from current state:

- Fix all 18 Must-fix-before-MVP findings (non-negotiable prerequisite)
- Set Celery concurrency: `--concurrency=4 --pool=prefork` for M4 (Playwright workers are CPU-bound per browser process)
- Set Celery concurrency: `--concurrency=8 --pool=gevent` for M2/M3/M5 (I/O-bound)
- Deploy separate worker processes: one process subscribing to `queue:application_execution` only, one for all remaining queues
- Configure `max_connections=20` on the SQLAlchemy engine pool
- Add `restart: always` to all Docker Compose services
- Configure all env vars via `.env.production` (not in git); use Docker secrets or AWS Secrets Manager for API keys

**Growth (10-100 concurrent users, <500 applications/day)**

Changes required beyond MVP tier:

- Extract Module 3 into its own Python service with its own FastAPI app and Celery worker cluster (3-5 workers)
- Extract Module 4 into its own containerized service using `mcr.microsoft.com/playwright/python` base image (2-4 replicas, 1 worker process per replica to keep Playwright memory bounded at ~500MB/replica)
- Replace Redis pub/sub event bus with Redis Streams for durability (pub/sub drops messages if no consumer is subscribed at publish time)
- Add PgBouncer in Transaction mode for Supabase connection pooling (Supabase provides this natively in Pro tier)
- Implement Flower or Grafana+celery-exporter for operational visibility
- Configure Gemini API quota increase; implement `tenacity` retry with exponential backoff on all LLM calls
- Add application-level caching (Redis) for frequently-read job and candidate records

**Scale (100+ users, 1000+ applications/day)**

Full microservices at this tier:

- Kubernetes (EKS or GKE) with separate Deployments for: API (3-5 replicas), M3 workers (3-10 replicas auto-scaled on queue depth), M4 workers (5-20 replicas, 1 Playwright process each), M2 workers (2-5 replicas), Celery Beat (1 replica with leader election)
- Replace Supabase with a self-managed PostgreSQL cluster (Aurora PostgreSQL or Cloud SQL) with read replicas for analytics queries
- Redis Cluster or ElastiCache for Redis Cluster mode (horizontal sharding)
- Object storage (S3 or GCS) for screenshots and resume PDFs instead of Supabase Storage
- Distributed tracing with OpenTelemetry + Jaeger
- Celery results backend migrated from Redis to PostgreSQL (Redis results backend has no durability guarantee across restarts)
- Rate limit enforcement moved to API gateway level (Kong or AWS API Gateway)

---

## RECOMMENDED REMEDIATION SEQUENCE

Execute in this exact order to unblock production:

**1. [FINDING-007] Fix `asyncio.get_event_loop()` crash**  
*Rationale: One-line fix. Unblocks the M3 Celery path. Do this first because it's the cheapest and affects everything else.*

**2. [FINDING-002] Unify the event bus — delete duplicate `publish_event`**  
*Rationale: Prevents duplicate task execution which would amplify all other bugs. Single-file change.*

**3. [FINDING-010, FINDING-012] Kill `echo=True` and fix CORS**  
*Rationale: Both are one-line environment-specific fixes that must not reach production. Takes 10 minutes combined.*

**4. [FINDING-001] Authenticate `/prepare-package` endpoint and make it async**  
*Rationale: Security blocker and scalability blocker. Also create the `202 Accepted` async pattern to eliminate the blocking HTTP call from within the Playwright browser thread.*

**5. [FINDING-008] Eliminate mid-execution M3 callback from M4 executor**  
*Rationale: Depends on FINDING-001 being resolved first (the callback goes to the now-async endpoint). This is the primary scalability blocker for M4.*

**6. [FINDING-013, FINDING-009] Fix rate limiter race condition and Redis connection leak**  
*Rationale: Both are small code changes. The rate limiter TOCTOU is a LinkedIn account-suspension risk.*

**7. [FINDING-014] Add pagination to `list_applications`**  
*Rationale: Prevents the full-table scan that will starve the database within days of launch.*

**8. [FINDING-022, FINDING-021] Screenshot bucket privacy + RLS audit**  
*Rationale: Both are PII/security findings that create legal liability. Complete together.*

**9. [FINDING-005, FINDING-006] Add Dead Letter Queue and stuck-application recovery**  
*Rationale: Operational integrity. Without these, the first worker crash requires manual DB cleanup to resume operations.*

**10. [FINDING-004, FINDING-011] Create module4 Dockerfile and add `restart: always`**  
*Rationale: Nothing in M4 can deploy to production without a container image. Process restart policy is the minimum viable ops story.*

**11. [FINDING-003] Begin module extraction (PYTHONPATH removal)**  
*Rationale: This is the largest effort (L) but enables all future scaling. Start with extracting M4's task registration as the first step. Complete after all other must-fix items are done.*

**12. [FINDING-016, FINDING-019] Fix dynamic_apply pipeline bypass and hardcoded localhost**  
*Rationale: Quality and Docker correctness fixes. Will visibly degrade application quality if skipped.*

---

## APPENDIX: WHAT IS WORKING WELL

The following components are production-appropriate and should be preserved without modification:

**State Machine Design (`backend/app/services/state_machine.py`):**  
The transition table is clean, the `validate_transition` function is correct, and the HTTP 422 response on invalid transitions is properly implemented. The `BLOCKED → QUEUED` escape hatch is a thoughtful design. This is one of the better-built components in the system.

**`ApplicationHistory` audit logging (`backend/app/routers/applications.py` lines 150–156):**  
Every status transition is written to `application_history` with `from_status`, `to_status`, `metadata`, and timestamp. This is essential for debugging and compliance and is correctly implemented.

**Deterministic Browser Fingerprinting (module-4-browser-automation.md `StealthConfig`):**  
The design decision to use `hash(candidate_id)` to deterministically select viewport, user agent, and timezone is architecturally sound. It prevents cross-run fingerprint drift without requiring state persistence.

**`acks_late=True` on `execute_application` Task:**  
The `acks_late=True` configuration on the browser automation task (`browser_automation.py` line 269) is correct — it prevents Celery from acknowledging the message until the task completes, ensuring failed tasks are re-queued by the broker. This is the right choice for long-running, non-idempotent work.

**Supabase Auth Integration (`backend/app/routers/auth.py`):**  
The token validation with a 2-minute in-memory cache, connection pooling via a singleton `AsyncClient`, and retry logic on Supabase timeouts is well-implemented. The admin user auto-promotion via `SUPABASE_ADMIN_USER_ID` env var is a clean pattern.

**`pool_pre_ping=True` and `pool_recycle=1800` on Database Engine:**  
These are the correct SQLAlchemy settings to handle cloud database connection resets. Pre-ping prevents stale connection errors; recycle prevents connections from timing out on the cloud provider's idle timeout.

**AgentLoop Vision Fallback Architecture (`executor.py` lines 303–363):**  
The design of AgentLoop as a primary path with graceful fallback to the scripted deterministic pipeline is excellent defensive engineering. If Gemini is unavailable or returns a bad response, the system falls back to CSS-selector-based form filling rather than failing. This dual-mode design should be preserved.

**Rate Limiting Design (rate_limiter.py):**  
The design of per-platform, per-candidate hourly AND daily counters in Redis with proper TTLs is exactly right. The `PLATFORM_LIMITS` dict with different limits per ATS type reflects real operational knowledge. Fix the TOCTOU race (FINDING-013) but preserve this design.

**`application_history` Table Schema:**  
The history table correctly uses `from_status` as nullable (for initial creation), has a clear FK, and the router correctly logs history on both creation and status update. This is audit-ready as-is.

**Dual-channel event consumer in `event_consumer.py`:**  
Subscribing to both `event:application.package_ready` and `events:application.package_ready` (lines 36-38) is a pragmatic approach to handle legacy publishers. Once FINDING-002 is resolved and a single channel convention is adopted, this pattern can be simplified.
