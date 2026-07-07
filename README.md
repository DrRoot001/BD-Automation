# BD-Automator-Agent

Welcome to the **Business Development (BD) Automation System** repository.

This repository serves as the single source of truth for our 5-person development team. The project has been divided into five core modules. Each team member has a dedicated markdown planning file detailing their scope, public interfaces, dependencies, implementation sequence, and integration checklist.

## 🛠️ Team Modules & Assignments

1. **[Module 1: Data & Orchestration Core](module-1-data-orchestration.md)** — Backend / Platform Engineer
2. **[Module 2: Job Discovery Engine](module-2-job-discovery.md)** — Web Scraping / QA Engineer
3. **[Module 3: AI & Resume Tailoring Intelligence](module-3-ai-resume-intelligence.md)** — AI / LLM Engineer
4. **[Module 4: Browser Automation Engine](module-4-browser-automation.md)** — RPA / Browser Automation Specialist
5. **[Module 5: Email Intelligence & Analytics Dashboard](module-5-email-dashboard.md)** — Full-Stack / Integration Engineer

---

## 🚀 Quick Start (New Developers)

### Prerequisites

- **Python 3.11+** — [python.org](https://www.python.org/downloads/)
- **Node.js 20+** — [nodejs.org](https://nodejs.org/) (required for Module 2 scrapers)
- **npm** — bundled with Node.js
- A copy of **`backend/.env`** — ask the team lead for this file (contains DB/Redis/API keys)

---

### 1. Clone & enter the repo

```bash
git clone https://github.com/YOUR_ORG/BD-Automator-Agent.git
cd BD-Automator-Agent
```

### 2. Create and activate a Python virtual environment

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**Windows (Command Prompt / CMD):**
```bat
python -m venv .venv
.venv\Scripts\activate.bat
```

### 3. Install Python dependencies

```bash
pip install -r backend/requirements.txt
pip install -e .
```

> `pip install -e .` installs the repo as an editable package so `module2`, `module3`, `module4`, `module5` are all importable without setting `PYTHONPATH` manually.

### 4. Install Playwright browsers

```bash
playwright install chromium
```

### 5. Install Node.js dependencies (Module 2 scrapers)

**macOS / Linux:**
```bash
cd module2/scraper && npm install && cd ../..
```

**Windows (PowerShell / CMD):**
```powershell
cd module2\scraper
npm install
cd ..\..
```

### 6. Install frontend dependencies

**macOS / Linux:**
```bash
cd frontend && npm install && cd ..
```

**Windows (PowerShell / CMD):**
```powershell
cd frontend
npm install
cd ..
```

### 7. Configure environment variables

```bash
# Copy the sample env and fill in your values:
cp backend/.env.example backend/.env
# Edit backend/.env — add DATABASE_URL, REDIS_URL, GEMINI_API_KEY, etc.

# Frontend env is already committed at frontend/.env.local
# (only contains public Supabase keys — safe to commit)
```

> **Required variables in `backend/.env`:**
> ```
> DATABASE_URL=postgresql+asyncpg://...
> REDIS_URL=rediss://...
> SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
> SUPABASE_URL=https://xxxx.supabase.co
> SUPABASE_SERVICE_ROLE_KEY=...
> SUPABASE_ANON_KEY=...
> SUPABASE_ADMIN_USER_ID=...
> GEMINI_API_KEY=...
> ENCRYPTION_KEY=<generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
> ```

---

### 8. Start the full stack

**macOS / Linux:**
```bash
./run_local.sh
# or for a more verbose dev experience:
./dev.sh
```

**Windows:**
```bat
run_all.bat
```

This starts 4 services in separate terminal windows:
| Service | URL |
|---------|-----|
| FastAPI backend | http://localhost:8000 |
| Celery worker | (background, all queues) |
| Celery beat | (scheduled tasks) |
| Next.js frontend | http://localhost:3000/dashboard |

---

### 9. Run database migrations

```bash
cd backend
alembic upgrade head
cd ..
```

---

## 📁 Project Structure

```
BD-Automator-Agent/
├── backend/               # Module 1 — FastAPI core, DB models, Celery tasks
│   ├── app/
│   │   ├── routers/       # API endpoints
│   │   ├── models/        # SQLAlchemy ORM models
│   │   ├── tasks/         # Celery task definitions
│   │   ├── services/      # Business logic
│   │   └── browser_automation/  # Browser engine (Playwright)
│   ├── requirements.txt   # Production Python deps
│   ├── requirements-dev.txt  # Dev/test deps
│   └── .env               # ⚠️ NOT committed — get from team lead
├── module2/               # Job scraping (Python adapters + Node.js)
│   ├── adapters/          # Greenhouse, Lever, Indeed, RSS
│   └── scraper/           # Node.js FetchFox scrapers (Dice, RemoteRocketship)
├── module3/               # AI resume tailoring + scoring
│   ├── orchestrator.py
│   ├── parser/
│   ├── scoring/
│   └── tailoring/
├── module4/               # Browser automation tasks
│   └── tasks/
├── module5/               # Email scanning + interview tracking
│   ├── scanner.py
│   └── gmail/
├── frontend/              # Next.js dashboard
│   ├── app/               # Pages + server actions
│   ├── components/        # UI components
│   └── hooks/             # React hooks (useWebSocket, etc.)
├── run_local.sh           # macOS/Linux startup script
├── dev.sh                 # Verbose dev startup script
├── run_all.bat            # Windows startup script
├── stop_all.bat           # Windows shutdown script
└── pyproject.toml         # Python package config
```

---

## 🔧 Common Issues

| Error | Fix |
|-------|-----|
| `ModuleNotFoundError: No module named 'module2'` | Run `pip install -e .` from the repo root |
| `playwright._impl._errors.Error: Executable doesn't exist` | Run `playwright install chromium` |
| `Connection refused` on API calls | Backend not running or wrong port — check `frontend/.env.local` has `API_URL=http://localhost:8000` |
| `getaddrinfo failed` / DNS errors | Transient network issue — retry in 5–10 seconds |
| `celery` not found | Activate your venv first: `source .venv/bin/activate` |

---

## 🔒 Environment Files

| File | Committed? | Contains |
|------|-----------|---------|
| `backend/.env` | ❌ **Never commit** | Database URL, Redis URL, API keys |
| `backend/.env.example` | ✅ | Template with empty values |
| `frontend/.env` | ✅ | Public Supabase URL + anon key |
| `frontend/.env.local` | ✅ | API proxy URL (port 8000) |
