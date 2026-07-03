# BD Automator Pipeline Audit
**Date:** 2026-07-01  
**Branch:** our-fixes-merged  
**Scope:** Job matching → Module 3 (resume tailoring) → Module 4 (browser automation)

---

## Pipeline Flow

1. **Beat trigger** — `task:daily_job_matching` fires once/day (configured hour, default 8 AM UTC) via Celery Beat (`celery_app.py:168-172`).
2. **Fan-out** — `daily_job_matching` queries all candidate IDs and dispatches one `task:match_single_candidate` per candidate in parallel via a Celery chord (`daily_job_matching.py:66-69`).
3. **Single-candidate matching** — `match_single_candidate` calls `run_matching_for_candidate` which:
   - Runs the stuck-application watchdog inline (`matching.py:128-132`)
   - Fetches the candidate + base resume from DB
   - Self-heals missing embeddings (can trigger PDF re-parse here)
   - Runs a pgvector cosine distance query (`< 0.35`) against all recent jobs
   - Iterates jobs **sequentially**, calling `score_job_fit` (Gemini LLM) per job — with a Redis cache check first
   - Creates an `Application` record (FOUND status) + `ApplicationHistory` record per job, committing after each one
   - Calls `orchestrate_application_package` (Module 3) per qualifying job — also sequentially
4. **Module 3 orchestration** (`module3/orchestrator.py`) per application:
   - Fetches candidate, base resume, and job from API (3 separate HTTP calls)
   - Re-fetches all resumes to compute next version number
   - Optionally calls `score_job_fit` again (second LLM call unless `prefetched_match_result` is provided)
   - Optionally runs `tailor_resume` + `generate_cover_letter` + `answer_screening_questions` concurrently via `asyncio.gather`
   - Uploads tailored resume PDF to Supabase
   - Uploads cover letter PDF to Supabase
   - PATCHes application status to QUEUED
   - Publishes Redis event `application.package_ready`
5. **Browser automation dispatch** — event listener or matching service calls `execute_application.apply_async`
6. **Module 4 execution** (`task:execute_application`):
   - `hydrate_and_execute` fetches application, candidate, and job from API (3 more HTTP calls)
   - Resolves resume URL (possibly another HTTP call to `/resumes/{cand_id}`)
   - Launches `ApplicationExecutor` → `AgentLoop`
   - Vision-driven loop: screenshot + DOM snapshot → LLM (Anthropic/Gemini) → Playwright action, up to 60 steps, 240 s wall timeout
7. **Completion** — publishes `application.submitted` or `application.failed` Redis event; DB status updated.

---

## Critical Bottlenecks

### 1. Sequential per-job LLM scoring inside a single Celery task (HIGHEST IMPACT)
**File:** `backend/app/services/matching.py:354–504`

The `for job in jobs:` loop is fully sequential. For each job it:
- Calls `score_job_fit` (Gemini call, ~2–8 s each)
- Commits two DB rows
- Calls `orchestrate_application_package` which is itself 30–120 s of work (tailoring + cover letter + uploads)

If a candidate matches 10 jobs, this loop takes **10 × (LLM scoring + tailoring + uploads)** = easily 10–20 minutes, all blocking one Celery worker on `queue:job_processing`. During this time no other candidate can be processed on that worker.

**Fix:** Dispatch one `task:prepare_application_package` Celery task per job (already exists in `resume_generation.py`) and return immediately. Let Module 3 run in parallel on `queue:resume_generation`.

---

### 2. Celery concurrency = 2 starves all queues
**File:** `dev.sh` (start command) / `CLAUDE.md:Running`

The dev command specifies `--concurrency=2`. With 5 queues and browser runs that each hold a worker for up to 60 minutes, 2 workers means at most 2 simultaneous browser runs. Every other queue (matching, resume generation, email scan) blocks behind them.

**Fix:** Raise concurrency to at least 4–6 for development, or run dedicated workers per queue:
```bash
# separate workers per queue type
celery -A app.celery_app worker -Q queue:application_execution --concurrency=2
celery -A app.celery_app worker -Q queue:resume_generation,queue:job_processing --concurrency=4
```

---

### 3. Watchdog runs inside the matching hot path (adds latency to every matching run)
**File:** `backend/app/services/matching.py:125-132`

`recover_stuck_applications_async` is called at the START of every `run_matching_for_candidate` invocation. This is a full DB scan for stuck apps. It also runs as a Beat task every 10 minutes on the `celery` queue. Running it inline adds latency before a single job is scored — measured as `t0` in the logs.

**Fix:** Remove the inline call. The Beat schedule (`recover-stuck-applications-every-10m`) already covers it.

---

### 4. Module 3 orchestrator makes redundant HTTP calls and a second LLM score call
**File:** `module3/orchestrator.py:111–195`

Every `orchestrate_application_package` call (even when invoked from `matching.py` which has already fetched everything) makes fresh HTTP calls:
- `GET /api/candidates/{id}` (line 114) — already fetched in `matching.py`
- `GET /api/resumes/{id}?is_base=true` (line 121) — already fetched in `matching.py`
- `GET /api/jobs/{id}` (line 198) — already fetched in `matching.py`
- `GET /api/resumes/{id}` (line 269) — a second all-resumes call just to compute `next_version`

The `score_job_fit` call at line 257 is skipped when `prefetched_match_result` is provided (which `matching.py` does set), so that redundant LLM call is already avoided — but the data-fetch redundancy remains.

**Fix:** Pass `candidate_dict`, `resume_data`, and `job_obj` directly into the orchestrator instead of re-fetching from the API. The objects are already constructed in `matching.py:338–401`.

---

### 5. Per-job DB commits inside the sequential loop (N × 2 commits for N jobs)
**File:** `backend/app/services/matching.py:415-428`

For each job the loop does:
```python
session.add(app_record)
await session.commit()          # commit 1
await session.refresh(app_record)
session.add(initial_history)
await session.commit()          # commit 2
```

Two round-trips to Supabase (across the internet) per job. For 10 jobs = 20 sequential DB commits before any tailoring starts.

**Fix:** Batch the Application + ApplicationHistory inserts and flush once per N jobs, or use `session.flush()` instead of `commit()` to keep within one transaction.

---

### 6. `hydrate_and_execute` makes 3–4 redundant HTTP round-trips at browser-run start
**File:** `backend/app/tasks/browser_automation.py:125-181`

At the start of every browser task, `hydrate_and_execute` makes:
1. `GET /applications/{app_id}` (line 137)
2. `GET /candidates/{cand_id}` (line 144)
3. `GET /jobs/{job_id}` (line 147)
4. Possibly `GET /resumes/{cand_id}` to resolve resume URL (line 166)

Calls 2, 3 are sequential (not parallelized). All the data was already available when `execute_application.apply_async` was called — it should be passed in the `package_dict`.

**Fix:** Enrich `package_dict` with `candidate_profile`, `job_title`, `job_url`, etc. at the dispatch site (in `matching.py` or the orchestrator) so `hydrate_and_execute` needs only the resume URL lookup.

---

### 7. Browser agent wall-clock timeout is 240 s but task soft limit is 3000 s
**File:** `loop.py:55`, `celery_app.py:164`

The agent loop aborts at 240 s + 330 s post-submit grace = 570 s max per form fill. The task soft limit is 3000 s (50 min). There is no issue with the limits themselves, but the wall-clock budget means a single hanging form blocks a worker for up to 570 s even before the Celery timeout fires. With 2 workers this is 570 s × 2 of blocked capacity.

---

### 8. `prepare_package_for_live_application` makes an unfiltered `GET /applications` call
**File:** `module3/orchestrator.py:528-534`

```python
resp = await client.get("/api/applications")
if resp.status_code == 200:
    apps = resp.json()
    for app in apps:
        ...
```

This fetches **all** applications (potentially thousands) and does a Python-side linear scan to find one matching `candidate_id + job_id`. This is an N+1 pattern that gets slower as the application table grows.

**Fix:** Pass `candidate_id` and `job_id` as query params, or look up by index in the DB.

---

## Bugs Found

### BUG-1: `dynamic_apply` task is routed to `queue:application_execution` — blocks browser runs
**File:** `celery_app.py:89`, `dynamic_apply.py:226-229`

```python
queue="queue:application_execution",
```

`task:dynamic_apply` (which scores jobs and triggers M3) shares the same queue as `task:execute_application` (which runs browsers). A long dynamic-apply run (scoring 500 jobs × HTTP calls) will occupy an execution worker and delay actual browser automation tasks behind it.

**Fix:** Route `task:dynamic_apply` to `queue:job_processing` instead.

---

### BUG-2: `task:recover_stuck_applications` is routed to the default `celery` queue, but `match_single_candidate` also calls `recover_stuck_applications_async` inline
**File:** `matching.py:128-132`, `celery_app.py:96`

If the `celery` default queue is not being consumed (no worker subscribed to it), the Beat task never runs. Meanwhile the inline call in `matching.py` still fires every match cycle. If the Beat task IS running, you get double execution.

**Fix:** Remove the inline call from `matching.py` (see Bottleneck #3). Ensure the default `celery` queue is consumed.

---

### BUG-3: New event loop created per Celery task (`asyncio.new_event_loop()`) — potential memory leak
**Files:** `daily_job_matching.py:16`, `match_single_candidate.py:19`

Each helper function creates a new event loop and runs it to completion, which is correct. However, if an exception propagates before `loop.close()` (e.g., a `BaseException` like `SystemExit`), the loop leaks. The `try/finally` pattern in these files handles normal exceptions, but `asyncio.run()` (used in `browser_automation.py:327`) is safer as it always cleans up.

**Fix:** Replace the `_run()` helper pattern with `asyncio.run()` throughout.

---

### BUG-4: `publish_event` in `browser_automation.py` opens a new Redis connection on every call
**File:** `backend/app/tasks/browser_automation.py:28-52`

```python
async def publish_event(event_name: str, payload: dict) -> None:
    redis_client = aioredis.from_url(redis_url, **kwargs)
    try:
        ...
    finally:
        await redis_client.aclose()
```

A new Redis connection is created and torn down for EVERY event publish. During a browser run, `publish_event` is called 4–5 times. Each call = new TCP connection to Upstash (TLS handshake over the internet). This is slow and burns through connection quota.

**Fix:** Use a module-level connection pool or reuse the same client within a task invocation.

---

### BUG-5: `prepare_application_package` Celery task (`resume_generation.py`) does not pass `prefetched_match_result`
**File:** `backend/app/tasks/resume_generation.py:3-22`

```python
result = asyncio.run(
    orchestrate_application_package(
        candidate_id=str(candidate_id),
        job_id=str(job_id)
    )
)
```

When called as a standalone task, no `prefetched_match_result` is passed, so the orchestrator always makes a second LLM `score_job_fit` call. The matching pipeline (`matching.py:467`) already computed and cached the score, but the Celery task signature doesn't thread it through.

**Fix:** Add `prefetched_match_result` as a serializable parameter to the task, or rely on the Redis score cache (which `matching.py` already populates — the orchestrator should read it before calling the LLM).

---

### BUG-6: `skipped_jobs` set in `matching.py` includes ALL historical job IDs, not just active ones
**File:** `backend/app/services/matching.py:257-263`

```python
all_apps_stmt = select(Application).where(Application.candidate_id == candidate_id)
all_apps = (await session.execute(all_apps_stmt)).scalars().all()
for app in all_apps:
    skipped_jobs.add(app.job_id)
```

Jobs with `FAILED`, `WITHDRAWN`, or `REJECTED` status are also added to `skipped_jobs`, preventing the pipeline from retrying previously failed applications — even ones that failed due to transient infra errors. A user who fixes a configuration issue can never re-apply to a job from the automated pipeline.

**Fix:** Filter to terminal-positive statuses (`SUBMITTED`, `CONFIRMED`, `INTERVIEW_*`, `OFFER`) or explicitly allow retry on certain failure reasons.

---

### BUG-7: `retry_failed_application` adds a fixed 5-minute countdown even for non-transient failures
**File:** `backend/app/tasks/browser_automation.py:510-521`

```python
def retry_failed_application(package_dict: dict):
    execute_application.apply_async(args=[package_dict], countdown=300)
```

This task dispatches a retry unconditionally with a 300 s delay. There's no check on the failure reason — it will blindly retry `BOT_DETECTED`, `ROBOTS_BLOCKED`, `JOB_EXPIRED`, and `LOGIN_REQUIRED` failures, all of which are already marked terminal in `execute_application`. The retry will just fail again immediately, wasting a worker slot.

**Fix:** Check `package_dict.get("failure_reason")` before dispatching, or remove this task entirely (the `max_retries=3` in `execute_application` already handles retries).

---

### BUG-8: `_fetch_open_jobs` in `dynamic_apply.py` uses `AsyncSessionLocal` instead of `task_session` for DB work
**File:** `backend/app/tasks/dynamic_apply.py:182-189`

```python
from app.database import AsyncSessionLocal
async with AsyncSessionLocal() as session:
    result = await run_matching_for_candidate(...)
```

`AsyncSessionLocal` uses the shared connection pool (sized for the FastAPI web process). Celery tasks should use `task_session()` (NullPool) per the architecture constraints in `CLAUDE.md`. Using `AsyncSessionLocal` inside a Celery worker can exhaust the PgBouncer session limit.

**Fix:** Replace with `task_session()`.

---

## Quick Wins

### QW-1: Move `task:dynamic_apply` to `queue:job_processing`
**1-line change in `celery_app.py:89`.**  
Stops dynamic-apply from consuming browser-automation worker slots. Time-to-fix: ~2 minutes.

### QW-2: Remove inline watchdog from `matching.py`
**Delete lines 125-132 in `matching.py`.**  
Removes a full DB scan from the hot path of every match run. Time-to-fix: ~2 minutes.

### QW-3: Fix `dynamic_apply.py` to use `task_session()` instead of `AsyncSessionLocal`
**1-line change in `dynamic_apply.py:182`.**  
Prevents connection pool exhaustion. Time-to-fix: ~2 minutes.

### QW-4: Parallelize the 3 HTTP fetches in `hydrate_and_execute`
**File:** `browser_automation.py:137-149`  
Replace three sequential `await client.get(...)` calls with `asyncio.gather`:
```python
app_resp, cand_resp, job_resp = await asyncio.gather(
    client.get(f"{api_base}/applications/{app_id}"),
    client.get(f"{api_base}/candidates/{cand_id}"),
    client.get(f"{api_base}/jobs/{job_id}"),
)
```
Saves ~2 × network RTT per browser run start. Time-to-fix: 10 minutes.

### QW-5: Raise Celery worker concurrency in `dev.sh`
Change `--concurrency=2` to `--concurrency=6` (or use per-queue workers).  
Immediately unblocks queues that are starved behind long browser runs.

### QW-6: Fix `prepare_package_for_live_application` all-applications scan (BUG-8)
**File:** `module3/orchestrator.py:528`  
Add query params: `client.get("/api/applications", params={"candidate_id": candidate_id, "job_id": job_id})`  
Prevents a full-table scan in Python as the application table grows.

### QW-7: Reuse Redis client in `browser_automation.py` instead of reconnecting per-publish
Create a module-level helper that keeps one connection open per task execution rather than calling `aioredis.from_url` on every event. Time-to-fix: 15 minutes.

---

## Bigger Optimizations

### BO-1: Parallelize per-job scoring and orchestration
**Estimated impact: 5–15× throughput improvement for multi-job matching runs**

Current flow is fully sequential: score job 1 → tailor job 1 → score job 2 → tailor job 2 → ...

Refactor `run_matching_for_candidate` to:
1. Run all LLM `score_job_fit` calls in parallel via `asyncio.gather` (they're already async and independent).
2. For qualifying jobs, dispatch `task:prepare_application_package` as a Celery task instead of calling `orchestrate_application_package` inline.

This means `run_matching_for_candidate` completes in seconds (LLM scoring in parallel) and the slow M3 work happens in parallel across many workers.

### BO-2: Eliminate M3's redundant HTTP round-trips by passing data directly
**Files:** `matching.py:338-401`, `orchestrator.py:111-195`

`matching.py` already has `candidate_dict`, `resume_data`, and `job_obj` as Python objects. Serializing and passing these into `orchestrate_application_package` (or into the Celery task payload) avoids 3–4 HTTP calls and 1 extra DB read per application. The orchestrator's `httpx.AsyncClient` block would only be needed for operations the caller can't pre-compute (e.g., uploading to Supabase).

### BO-3: Batch LLM scoring with a single multi-job prompt
**File:** `module3/scoring/fit_scorer.py` (not audited directly)

Each `score_job_fit` call makes one Gemini API call. For a candidate matching 20 jobs, that's 20 serial (or parallel with rate limits) LLM calls. A batched prompt that scores N jobs against one resume in a single call would be faster and cheaper — many LLMs support multi-item evaluation in one request.

### BO-4: Add pgvector index and tune the similarity threshold
**File:** `matching.py:300-306`

The cosine distance query `Job.embedding.cosine_distance(base_resume.embedding) < 0.35` runs on every match cycle. Without an `ivfflat` or `hnsw` index on `jobs.embedding`, this is a sequential scan over all jobs. As the job table grows, this becomes the dominant query cost.

Ensure the following index exists:
```sql
CREATE INDEX CONCURRENTLY jobs_embedding_hnsw
  ON jobs USING hnsw (embedding vector_cosine_ops);
```

### BO-5: Run cover letter and resume tailoring in parallel (already partially done)
**File:** `module3/orchestrator.py:293-300`

The `asyncio.gather(*tasks)` at line 300 already runs `tailor_resume` and `generate_cover_letter` concurrently when both are needed. This is good. However, the Supabase uploads that follow (lines 319-325 and 361-366) are sequential. These can also be parallelized with `asyncio.gather` since they upload to different buckets.

### BO-6: Cache resume parsed_json to avoid repeated Supabase storage fetches
**File:** `module3/orchestrator.py:121-165`

Every call to `orchestrate_application_package` fetches and potentially re-parses the base resume. For a candidate applying to 10 jobs in one run, the resume is fetched 10 times and potentially parsed 10 times. A short-lived in-memory or Redis cache keyed on `(candidate_id, resume_id)` would eliminate all but the first fetch.

### BO-7: Use Celery chains instead of inline sequential orchestration
**File:** `backend/app/services/matching.py:354+`

Replace the inline sequential `for job in jobs: ... orchestrate_application_package(...)` loop with a Celery chain/chord:
```
match_single_candidate 
  → for each job: task:prepare_application_package
      → on_success: task:execute_application
```

This distributes work across all available workers and allows horizontal scaling by simply adding more workers.

---

*Audit generated from static code review of the files listed above. Line numbers reference the state at commit `40a4fe3`.*
