# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

BD Automator is a multi-module job application automation system. It discovers job listings, scores them against a candidate's resume, tailors documents using AI, then fills and submits application forms autonomously via browser automation. The full pipeline is:

**Module 2** (job discovery via web scraping) → **Module 3** (AI resume tailoring + scoring) → **Module 4** (browser automation engine) → **Module 5** (email inbox scanner for interview detection)

**Module 1** is the shared backend: FastAPI REST API, PostgreSQL via SQLAlchemy async, Celery task queue, Redis broker.

---

## Running the Stack Locally

```bash
./dev.sh
```

This starts all four services (FastAPI on `:8002`, Celery worker, Celery beat, Next.js on `:3000`) with colour-coded log tails. Logs land in `./logs/`. `PYTHONPATH` is set to the repo root so all five modules resolve.

Individual services:
```bash
# Backend only (from backend/)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload --reload-dir app

# Celery worker (from backend/)
python -m celery -A app.celery_app worker --loglevel=info --concurrency=2 \
  -Q celery,queue:job_discovery,queue:job_processing,queue:resume_generation,queue:application_execution,queue:email_scan

# Celery beat scheduler (from backend/)
python -m celery -A app.celery_app beat --loglevel=info

# Frontend (from frontend/)
npm run dev
```

Docker Compose (production-style):
```bash
docker-compose up --build
```

---

## Environment Setup

Copy `backend/.env.example` to `backend/.env` and fill in:
- `DATABASE_URL` — Supabase Postgres via asyncpg (`postgresql+asyncpg://...`)
- `REDIS_URL` — Upstash Redis (`rediss://...`)
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY`
- `SECRET_KEY` — JWT signing key (hex, 64 chars)
- `ENCRYPTION_KEY` — Fernet key for token encryption
- `GEMINI_API_KEY` — primary LLM (Gemini is the default for all AI tasks)
- `ANTHROPIC_API_KEY` — fallback LLM for browser agent loop
- `M1_API_BASE_URL` — URL Celery workers use to call FastAPI (default `http://localhost:8000/api`)

The `.env` is loaded by `backend/app/config.py` via `pydantic-settings` and also via `load_dotenv` so `os.getenv()` calls in modules 3–5 work correctly.

---

## Database Migrations

```bash
# From backend/ directory
alembic upgrade head
alembic revision --autogenerate -m "description"
```

Alembic config is in `backend/alembic.ini`; env at `backend/app/migrations/env.py`.

**Supabase pooler constraint:** the pool is capped at 6 connections per process (`DB_POOL_SIZE=4`, `DB_MAX_OVERFLOW=2`) to stay under PgBouncer's session-mode 15-client limit. Celery tasks use `task_session()` from `backend/app/database.py` (NullPool) instead of the shared pool.

---

## Testing

```bash
# From repo root (sets PYTHONPATH automatically via pyproject.toml)
pytest

# Single file
pytest test_full_pipeline_e2e.py -v

# E2E scripts (require a running backend + real DB)
python backend/app/scripts/agent_e2e_local.py
python backend/app/scripts/e2e_m1_to_m4_test.py
```

---

## Architecture

### Module 1 — Backend / Data Layer (`backend/`)
- `backend/app/main.py` — FastAPI app factory. Registers all routers, CORS, rate limiting (slowapi), correlation-ID middleware, and a startup watchdog that recovers stuck applications.
- `backend/app/celery_app.py` — Celery instance with all task routing, beat schedule, and Upstash-specific transport tuning (visibility timeout 2 h, health-check keepalive every 25 s, early ack to prevent re-delivery on connection blip).
- `backend/app/routers/` — REST endpoints: `candidates`, `resumes`, `jobs`, `applications`, `analytics`, `auth`, `companies`, `dashboard`, `websocket`.
- `backend/app/services/state_machine.py` — Application status FSM. Every status transition is validated here. States: `FOUND → ANALYZED → MATCHED → RESUME_UPDATED → COVER_LETTER_CREATED → QUEUED → APPLICATION_STARTED → FORM_COMPLETED → SUBMITTED → CONFIRMED → INTERVIEW_R1/R2 → OFFER`, plus `FAILED`, `BLOCKED`, `REJECTED`, `GHOSTED`, `WITHDRAWN`.
- `backend/app/tasks/` — Celery tasks wiring the modules together: `dynamic_apply.py` (M2→M4 pipeline), `browser_automation.py`, `resume_generation.py`, `job_discovery.py`, `email_scan.py`, `daily_job_matching.py`.

### Module 2 — Job Discovery (`module2/`)
- Scraper (`module2/scraper/scrape.js`) — Node.js/Playwright scraper that hits job boards.
- Python side calls Node via subprocess or runs Python adapters. Output is normalized via `module2/normalization/schemas.py` into `NormalizedJob` objects stored in the DB.

### Module 3 — AI & Resume Intelligence (`module3/`)
- `module3/orchestrator.py` — Main pipeline entrypoint imported by `POST /api/applications/prepare-package`. Runs: parse resume PDF → score fit → tailor resume → generate cover letter → answer screening questions → upload artifacts to Supabase storage.
- Sub-packages: `parser/`, `scoring/`, `tailoring/`, `cover_letter/`, `qa/`, `utils/`.
- Primary LLM is Gemini; Anthropic is a fallback. The `sys.path` insert in `backend/app/main.py` ensures `module3` is importable when uvicorn runs from `backend/`.

### Module 4 — Browser Automation (`backend/app/browser_automation/`)
- `agent/loop.py` — Vision-driven agentic loop: screenshot + scoped DOM → LLM decides ONE action → Playwright executes → repeat. Max steps enforced; stall detection via DOM-hash comparison.
- `agent/page_agent.py` — Classifies the current page state (`FORM`, `LISTING`, `SUCCESS`, `BLOCKED`, `ERROR`, `UNKNOWN`) using Gemini vision before committing an action.
- `forms/` — Field detector, filler, AI resolver, LLM filler, form field memory.
- `learned_fixes/` — JSON files per platform (e.g. `www.remoterocketship.com.json`) that cache winning selectors discovered by the agent so future runs skip LLM calls.
- `llm/claude_client.py` — Anthropic client used by the agent loop; `llm/` also provides a `get_llm()` factory with `LLMUnavailable` fallback.
- Celery task: `task:execute_application` (queue `queue:application_execution`, soft 50 min / hard 60 min limit).

### Module 5 — Email Intelligence (`module5/`)
- `module5/scanner.py` + `module5/gmail/` — Polls Gmail for interview invites on a 15-minute schedule. Updates `interview_tracking` table.

### Frontend (`frontend/`)
- Next.js 14 App Router, TypeScript, Tailwind CSS, Recharts for analytics, TanStack Query for data fetching, Supabase JS for auth.
- Pages under `frontend/app/`: `dashboard`, `candidates`, `jobs`, `applications`, `matches`, `automation`, `analytics`, `admin`.
- API base configured via `NEXT_PUBLIC_API_BASE` env var (default `http://localhost:8002/api`).

---

## Key Design Constraints

- **`PYTHONPATH` must include repo root** for `module3` / `module4` imports to work inside the FastAPI process and Celery workers. `dev.sh` sets this; in Docker it's handled by the working-directory mount.
- **Celery tasks use `task_session()` (NullPool)**, not the shared `AsyncSessionLocal`, to avoid asyncio event-loop conflicts across tasks.
- **Browser agent acks tasks early** (`task_acks_late=False`) to prevent Upstash connection drops from re-delivering in-flight browser runs; a watchdog (`task:recover_stuck_applications`, every 10 min) re-queues genuinely stuck jobs instead.
- **LLM calls are optional in the browser agent** — `LLMUnavailable` is caught everywhere and the pipeline falls back to heuristics rather than failing hard.
