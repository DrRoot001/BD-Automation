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
   * Celery uses --pool=solo because Windows does NOT support the default
     prefork pool. Solo runs one task at a time (safe + predictable). To allow
     parallel tasks set $env:CELERY_POOL='threads' before running.
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

$BackendPort  = if ($env:BACKEND_PORT)  { $env:BACKEND_PORT }  else { '8000' }
$FrontendPort = if ($env:FRONTEND_PORT) { $env:FRONTEND_PORT } else { '3000' }
$CeleryPool   = if ($env:CELERY_POOL)   { $env:CELERY_POOL }   else { 'solo' }

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

$Queues = 'celery,queue:job_discovery,queue:job_processing,queue:resume_generation,queue:application_execution,queue:email_scan'

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

# 2. Celery worker - all queues. --pool=solo is required on Windows.
Start-Svc 'BD-Celery-Worker' $Backend `
    "& '$Python' -m celery -A app.celery_app worker --loglevel=info --pool=$CeleryPool -Q $Queues -n worker@%h" | Out-Null

# 3. Celery beat - scheduled pipelines
Start-Svc 'BD-Celery-Beat' $Backend `
    "& '$Python' -m celery -A app.celery_app beat --loglevel=info" | Out-Null

# 4. Next.js frontend
Start-Svc 'BD-Frontend' $Frontend `
    "npm run dev -- -p $FrontendPort" | Out-Null

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
