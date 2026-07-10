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

param([switch]$Stop)

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

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
if (Test-Path $PidFile) { Remove-Item $PidFile -Force }

# Read QUEUE_SUFFIX from backend/.env if it exists
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
$Queues = "celery$Suffix,queue:job_discovery$Suffix,queue:job_processing$Suffix,queue:resume_generation$Suffix,queue:application_execution$Suffix,queue:email_scan$Suffix"

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
    $inner = "`$host.UI.RawUI.WindowTitle='$Title'; " +
             "`$env:PYTHONPATH='$Root'; " +
             "`$env:API_URL='http://localhost:$BackendPort'; " +
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

# 1. FastAPI backend (with --reload for dev convenience)
Start-Svc 'BD-Backend-API' $Backend `
    "& '$Python' -m uvicorn app.main:app --host 0.0.0.0 --port $BackendPort --reload --reload-dir '$Backend\app'" | Out-Null

# Give the backend a moment to bind before the worker/frontend hit it
Start-Sleep -Seconds 3

# 2. Celery worker - all queues.
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
    ($WorkerEnv + "& '$Python' -m celery -A app.celery_app worker --loglevel=info --pool=$CeleryPool --concurrency=$CeleryConc -Q $Queues -n worker$Suffix@%h --without-mingle --without-gossip --without-heartbeat") | Out-Null

# 3. Celery beat - scheduled pipelines
Start-Svc 'BD-Celery-Beat' $Backend `
    "& '$Python' -m celery -A app.celery_app beat --loglevel=info" | Out-Null

# 4. Next.js frontend
Start-Svc 'BD-Frontend' $Frontend `
    "npx next dev -p $FrontendPort" | Out-Null

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
