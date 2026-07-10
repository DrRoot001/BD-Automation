<#
=============================================================================
 run_all.ps1  -  BD-Automator one-shot dev launcher (Windows / PowerShell)
=============================================================================
 Starts the WHOLE system, each service in its own labelled window so you can
 watch its logs live:

   1. FastAPI backend      ->  http://localhost:8000   (API + WebSocket)
   2. Celery worker        ->  all task queues (modules 2-5 + browser automation)
   3. Celery beat          ->  scheduled pipelines (matching, email scan, watchdog)
   4. Next.js frontend     ->  http://localhost:3000/dashboard

 Redis (Upstash) and Postgres (Supabase) are remote - nothing local to start.

 USAGE
   Start everything:   powershell -ExecutionPolicy Bypass -File run_all.ps1
                       (or just double-click  run_all.bat )
   Stop everything:    powershell -ExecutionPolicy Bypass -File run_all.ps1 -Stop

 NOTES
   * Celery uses --pool=threads (Windows does NOT support the default prefork
     pool, and 'solo' starves the broker connection during long browser runs).
     Override with $env:CELERY_POOL / $env:CELERY_CONCURRENCY.
   * Override ports with $env:BACKEND_PORT / $env:FRONTEND_PORT.
=============================================================================
#>

param([switch]$Stop, [switch]$SkipBootstrap)

$ErrorActionPreference = 'Stop'
$Root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend   = Join-Path $Root 'backend'
$Frontend  = Join-Path $Root 'frontend'
$LogDir    = Join-Path $Root 'logs'
$PidFile   = Join-Path $LogDir 'run_all.pids'

$BackendPort = "8000"
$EnvFile = Join-Path $Backend '.env'
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match '^\s*M1_API_BASE_URL\s*=\s*(.*)$') {
            $val = $Matches[1].Trim().Trim('"').Trim("'")
            if ($val -match ':(\d+)/?.*') {
                $BackendPort = $Matches[1]
            }
        }
    }
}
if ($env:BACKEND_PORT) { $BackendPort = $env:BACKEND_PORT }

$FrontendPort = if ($env:FRONTEND_PORT) { $env:FRONTEND_PORT } else { '3000' }
# 'threads' (NOT 'solo') is the default: the solo pool runs the task inline in the
# SAME thread that services the broker, so a multi-minute browser run starves the
# Upstash connection until it is reaped ("Connection closed by server") and the
# task is redelivered forever. The threads pool runs tasks in worker threads while
# the main thread keeps the broker alive. Override with $env:CELERY_POOL.
$CeleryPool   = if ($env:CELERY_POOL)   { $env:CELERY_POOL }   else { 'threads' }
# Concurrency: number of tasks that may run at once. 4 lets quick email scans run
# alongside one browser application without 2+ Chrome sessions colliding in
# practice. Override with $env:CELERY_CONCURRENCY.
$CeleryConc   = if ($env:CELERY_CONCURRENCY) { $env:CELERY_CONCURRENCY } else { '4' }

# --------------------------------------------------------------------------
# -Stop : kill every service we started (whole process tree) and exit.
# --------------------------------------------------------------------------
if ($Stop) {
    Write-Host "Stopping BD-Automator services..." -ForegroundColor Yellow
    if (Test-Path $PidFile) {
        Get-Content $PidFile | ForEach-Object {
            $procId = $_.Trim()
            if ($procId) {
                # /T kills the child tree (uvicorn reloader, node, celery children)
                taskkill /F /T /PID $procId 2>$null | Out-Null
            }
        }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Write-Host "  All tracked services stopped." -ForegroundColor Green
    } else {
        Write-Host "  No pid file found ($PidFile). Nothing tracked to stop." -ForegroundColor DarkGray
    }
    return
}

# --------------------------------------------------------------------------
# Resolve interpreters
# --------------------------------------------------------------------------
function Resolve-Python {
    foreach ($p in @(
        (Join-Path $Root '.venv\Scripts\python.exe'),
        (Join-Path $Root 'venv\Scripts\python.exe'),
        (Join-Path $Backend '.venv\Scripts\python.exe'),
        (Join-Path $Backend 'venv\Scripts\python.exe')
    )) { if (Test-Path $p) { return $p } }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "Python not found (no venv and 'python' not on PATH)."
}

$Python = Resolve-Python
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "npm not found on PATH (needed for the frontend)." }
if (-not (Test-Path (Join-Path $Backend '.env')))         { throw "backend/.env not found - DATABASE_URL and REDIS_URL are required." }

# --------------------------------------------------------------------------
# One-time bootstrap so a fresh machine "just works" on a double-click. Every
# step is a no-op when already satisfied, so re-runs are fast. Skip with
# -SkipBootstrap once the environment is known-good.
# --------------------------------------------------------------------------
function Ensure-Deps {
    Write-Host "Checking dependencies..." -ForegroundColor Cyan

    # Python packages — probe a representative set; install only if missing.
    & $Python -c "import playwright, fastapi, celery, httpx" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  installing Python dependencies (backend/requirements.txt)..." -ForegroundColor Yellow
        & $Python -m pip install -r (Join-Path $Backend 'requirements.txt')
    } else {
        Write-Host "  python deps OK" -ForegroundColor DarkGray
    }

    # Playwright Chromium — `install` is idempotent (fast no-op when present).
    Write-Host "  ensuring Playwright Chromium is installed..." -ForegroundColor DarkGray
    & $Python -m playwright install chromium 2>$null

    # Frontend node_modules
    if (-not (Test-Path (Join-Path $Frontend 'node_modules'))) {
        Write-Host "  installing frontend node_modules..." -ForegroundColor Yellow
        Push-Location $Frontend; npm install; Pop-Location
    } else {
        Write-Host "  frontend node_modules OK" -ForegroundColor DarkGray
    }

    # module2 scraper node_modules (Node scraper used by job discovery)
    $scraperDir = Join-Path $Root 'module2\scraper'
    if ((Test-Path (Join-Path $scraperDir 'package.json')) -and
        -not (Test-Path (Join-Path $scraperDir 'node_modules'))) {
        Write-Host "  installing scraper node_modules..." -ForegroundColor Yellow
        Push-Location $scraperDir; npm install; Pop-Location
    }
}

if (-not $SkipBootstrap) {
    try { Ensure-Deps }
    catch { Write-Host "  bootstrap step failed (continuing): $_" -ForegroundColor Yellow }
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
if (Test-Path $PidFile) { Remove-Item $PidFile -Force }

# Read QUEUE_SUFFIX from backend/.env if it exists (per-developer queue isolation)
$QueueSuffix = ""
$EnvFile = Join-Path $Backend '.env'
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match '^\s*QUEUE_SUFFIX\s*=\s*(.*)$') {
            $QueueSuffix = $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
}
if (-not $QueueSuffix) {
    $QueueSuffix = $env:USERNAME.ToLower()
}
$Suffix = if ($QueueSuffix) { "_$QueueSuffix" } else { "" }

# Two worker pools so browser applies are NEVER starved by the bulk email/
# interview scans. The MAIN worker handles discovery, matching, resume-gen and
# the (many, slow) inbox scans. A DEDICATED browser worker handles ONLY
# queue:application_execution, so an Auto-Apply's execute_application always has
# a free thread the moment it is dispatched instead of queuing behind 40+ Gmail
# scans on a shared pool. Both pools use this developer's suffixed queues.
$MainQueues    = "celery$Suffix,queue:job_discovery$Suffix,queue:job_processing$Suffix,queue:resume_generation$Suffix,queue:email_scan$Suffix"
$BrowserQueues = "queue:application_execution$Suffix"
# Concurrency for the dedicated browser worker (simultaneous browser applies).
$BrowserConc   = if ($env:BROWSER_CONCURRENCY) { $env:BROWSER_CONCURRENCY } else { '2' }

Write-Host ""
Write-Host "BD-Automator - starting full stack" -ForegroundColor Cyan
Write-Host ("-" * 50) -ForegroundColor DarkGray
Write-Host "  Root     : $Root"
Write-Host "  Python   : $Python"
Write-Host "  Backend  : http://localhost:$BackendPort"
Write-Host "  Frontend : http://localhost:$FrontendPort"
Write-Host "  Celery   : pool=$CeleryPool"
Write-Host ""

# --------------------------------------------------------------------------
# Launch a service in its OWN PowerShell window (-NoExit keeps it open so a
# crash is visible). Returns the spawned PID, which we track for -Stop.
# PYTHONPATH=$Root exposes module3 / module5; cwd=backend exposes the 'app' pkg.
# --------------------------------------------------------------------------
function Start-Svc([string]$Title, [string]$WorkDir, [string]$Command) {
    # Celery worker/beat call FastAPI back via M1_API_BASE_URL (status
    # transitions, artifact fetch, job POST) and module2 reads API_BASE_URL.
    # Derive both from the chosen backend port so a non-default BACKEND_PORT
    # does not silently break worker->backend callbacks (they used to hard-code
    # :8000 and ignored the override).
    $inner = "`$host.UI.RawUI.WindowTitle='$Title'; " +
             "`$env:PYTHONPATH='$Root'; " +
             "`$env:API_URL='http://localhost:$BackendPort'; " +
             "`$env:M1_API_BASE_URL='http://localhost:$BackendPort/api'; " +
             "`$env:API_BASE_URL='http://localhost:$BackendPort/api'; " +
             "Set-Location '$WorkDir'; " +
             "Write-Host '=== $Title ===' -ForegroundColor Cyan; " +
             $Command
    $p = Start-Process powershell -PassThru -ArgumentList @(
        '-NoExit', '-ExecutionPolicy', 'Bypass', '-Command', $inner
    )
    Add-Content -Path $PidFile -Value $p.Id
    Write-Host ("  started {0,-22} (pid {1})" -f $Title, $p.Id) -ForegroundColor Green
    return $p
}

# Launch ALL FOUR services CONCURRENTLY. None of them needs the backend's HTTP
# server to be up in order to START: the Celery worker/beat connect to Redis
# (the broker), not to FastAPI, and only call the backend later at task time;
# the Next.js dev server serves immediately and proxies API calls lazily (and
# its first compile takes longer than the backend's DB warmup anyway). Starting
# them in parallel — instead of gating them behind a backend health poll —
# means all four warm up at the same time rather than one-after-another.

# 1. FastAPI backend (with --reload for dev convenience)
Start-Svc 'BD-Backend-API' $Backend `
    "& '$Python' -m uvicorn app.main:app --host 0.0.0.0 --port $BackendPort --reload --reload-dir '$Backend\app'" | Out-Null

# 2. Celery MAIN worker - everything EXCEPT browser applies.
#    --without-mingle/--without-gossip/--without-heartbeat: with a single worker
#      these only add ~25s of "searching for neighbors" dead time and extra
#      Upstash connections on every reconnect — pure overhead here.
#    Timeouts: the AgentLoop fills (~90-150s) AND then runs the post-submit
#      email-verification phase (Gmail code polling, up to ~3×90s). The default
#      240s wall-clock cuts that off mid-poll — the "clicks submit but never
#      fetches the code" failure. 900s gives fill + verification ample room;
#      the executor's hard per-application deadline sits above it at 1500s.
$WorkerEnv = "`$env:AGENT_LOOP_WALL_TIMEOUT_S='900'; `$env:APPLICATION_EXEC_TIMEOUT_S='1500'; `$env:STRICT_MEMORY_ISOLATION='true'; "
$WorkerTitle = if ($QueueSuffix) { "BD-Celery-Worker ($QueueSuffix)" } else { "BD-Celery-Worker" }
Start-Svc $WorkerTitle $Backend `
    ($WorkerEnv + "& '$Python' -m celery -A app.celery_app worker --loglevel=info --pool=$CeleryPool --concurrency=$CeleryConc -Q $MainQueues -n main$Suffix@%h --without-mingle --without-gossip --without-heartbeat") | Out-Null

# 3. Celery BROWSER worker - dedicated to queue:application_execution ONLY, so an
#    Auto-Apply's execute_application always gets a free thread immediately and is
#    never stuck behind the bulk Gmail/interview scans on the main worker.
Start-Svc 'BD-Browser-Worker' $Backend `
    ($WorkerEnv + "& '$Python' -m celery -A app.celery_app worker --loglevel=info --pool=$CeleryPool --concurrency=$BrowserConc -Q $BrowserQueues -n browser@%h --without-mingle --without-gossip --without-heartbeat") | Out-Null

# 4. Celery beat - scheduled pipelines
Start-Svc 'BD-Celery-Beat' $Backend `
    "& '$Python' -m celery -A app.celery_app beat --loglevel=info" | Out-Null

# 4. Next.js frontend
Start-Svc 'BD-Frontend' $Frontend `
    "npx next dev -p $FrontendPort" | Out-Null

# Backend readiness is now reported for information only (it does NOT hold up the
# other services, which are already launching above). This just tells you when
# it's safe to hit Auto Apply. Runs in this launcher window while the four
# service windows warm up in parallel.
Write-Host ""
Write-Host "  waiting for backend to accept requests (services already starting in parallel)..." -ForegroundColor DarkGray
$healthy = $false
for ($i = 0; $i -lt 40; $i++) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 `
             -Uri "http://localhost:$BackendPort/api/health"
        if ($r.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}
if ($healthy) {
    Write-Host "  backend is healthy - safe to use Auto Apply." -ForegroundColor Green
} else {
    Write-Host "  backend not confirmed healthy after 40s - it may still be warming up." -ForegroundColor Yellow
}

Write-Host ""
Write-Host ("-" * 50) -ForegroundColor DarkGray
Write-Host "All services launched in separate windows." -ForegroundColor Cyan
Write-Host ""
Write-Host "  Dashboard : http://localhost:$FrontendPort/dashboard"
Write-Host "  API docs  : http://localhost:$BackendPort/docs"
Write-Host ""
Write-Host "  To STOP everything:  powershell -ExecutionPolicy Bypass -File run_all.ps1 -Stop"
Write-Host "                       (or double-click  stop_all.bat )"
Write-Host ""
