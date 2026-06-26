#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# dev.sh — BD Automator local dev runner
#
# Starts all services:
#   1. FastAPI backend      (port 8000)
#   2. Celery worker        (modules 2-5 tasks)
#   3. Celery beat          (scheduled pipelines)
#   4. Next.js frontend     (port 3000)
#
# Logs go to ./logs/<service>.log
# Press Ctrl+C to stop everything cleanly.
# ────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
NODE_BIN="$HOME/.nvm/versions/node/v20.19.5/bin"

BACKEND_PORT=8002
FRONTEND_PORT=3000
HEALTH_TIMEOUT=30   # seconds to wait for backend to become healthy

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

# ── Colours ──────────────────────────────────────────────────────────────────

RESET='\033[0m'
BOLD='\033[1m'
DIM='\033[2m'

C_API='\033[36m'      # cyan   — backend
C_WORKER='\033[33m'   # yellow — celery worker
C_BEAT='\033[35m'     # magenta — celery beat
C_FRONT='\033[34m'    # blue   — frontend
C_OK='\033[32m'       # green
C_ERR='\033[31m'      # red
C_INFO='\033[90m'     # dark grey

log()  { echo -e "${DIM}[$(date +%H:%M:%S)]${RESET} $*"; }
ok()   { echo -e "${C_OK}${BOLD}  ✓${RESET}  $*"; }
err()  { echo -e "${C_ERR}${BOLD}  ✗${RESET}  $*"; }
info() { echo -e "${C_INFO}  →${RESET}  $*"; }

# ── Validate environment ──────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}BD Automator — Development Stack${RESET}"
echo -e "${DIM}────────────────────────────────────────────────${RESET}"
echo ""

if [ ! -f "$PYTHON" ]; then
  err "Python not found at $PYTHON"
  echo "    Set PYTHON= at the top of this script to your python3 path."
  exit 1
fi

if [ ! -f "$ROOT/backend/.env" ]; then
  err "backend/.env not found — DATABASE_URL and REDIS_URL are required."
  exit 1
fi

# Make sure Node is resolvable
export PATH="$NODE_BIN:/usr/local/bin:$PATH"
if ! command -v node &>/dev/null; then
  err "node not found. Update NODE_BIN= at the top of this script."
  exit 1
fi

# ── PYTHONPATH — expose all modules to the backend and celery workers ─────────

export PYTHONPATH="$ROOT:${PYTHONPATH:-}"

info "Project root : $ROOT"
info "Python       : $($PYTHON --version 2>&1)"
info "Node         : $(node --version)"
info "PYTHONPATH   : $PYTHONPATH"
info "Logs         : $LOG_DIR"
echo ""

# ── PID tracking ─────────────────────────────────────────────────────────────

declare -a PIDS=()

cleanup() {
  echo ""
  log "Shutting down all services..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  # Give them a moment then force-kill stragglers
  sleep 1
  for pid in "${PIDS[@]}"; do
    kill -9 "$pid" 2>/dev/null || true
  done
  log "All services stopped."
  exit 0
}
trap cleanup SIGINT SIGTERM EXIT

# ── Helper: stream a log file with a coloured prefix ─────────────────────────

stream_log() {
  local label="$1"
  local color="$2"
  local file="$3"
  tail -f "$file" 2>/dev/null | while IFS= read -r line; do
    echo -e "${color}${BOLD}[${label}]${RESET} ${line}"
  done &
  PIDS+=($!)
}

# ── 1. FastAPI backend ────────────────────────────────────────────────────────

log "Starting FastAPI backend on :$BACKEND_PORT ..."
(
  cd "$ROOT/backend"
  "$PYTHON" -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "$BACKEND_PORT" \
    --reload \
    --reload-dir "$ROOT/backend/app" \
    --log-level info
) > "$LOG_DIR/backend.log" 2>&1 &
PIDS+=($!)
BACKEND_PID=$!
stream_log "API   " "$C_API" "$LOG_DIR/backend.log"

# ── 2. Wait for backend health ────────────────────────────────────────────────

echo -ne "${DIM}  Waiting for backend to become healthy${RESET}"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
healthy=0
while [ $(date +%s) -lt $deadline ]; do
  if curl -sf "http://localhost:$BACKEND_PORT/healthz" >/dev/null 2>&1 || \
     curl -sf "http://localhost:$BACKEND_PORT/health" >/dev/null 2>&1 || \
     curl -sf "http://localhost:$BACKEND_PORT/api/health" >/dev/null 2>&1 || \
     curl -sf "http://localhost:$BACKEND_PORT/api/candidates" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  echo -n "."
  sleep 1
done
echo ""

if [ $healthy -eq 1 ]; then
  ok "Backend is up"
else
  echo -e "${C_INFO}  Backend health check timed out — it may still be starting. Continuing.${RESET}"
fi

# ── 3. Celery worker — all task modules ──────────────────────────────────────

log "Starting Celery worker (modules 2-5) ..."
(
  cd "$ROOT/backend"
  "$PYTHON" -m celery \
    -A app.celery_app worker \
    --loglevel=info \
    --concurrency=2 \
    -n "worker@%h" \
    -Q celery,queue:job_discovery,queue:job_processing,queue:resume_generation,queue:application_execution,queue:email_scan
) > "$LOG_DIR/celery-worker.log" 2>&1 &
PIDS+=($!)
stream_log "WORKER" "$C_WORKER" "$LOG_DIR/celery-worker.log"

# ── 4. Celery beat — scheduled pipelines ─────────────────────────────────────

log "Starting Celery beat scheduler ..."
(
  cd "$ROOT/backend"
  "$PYTHON" -m celery \
    -A app.celery_app beat \
    --loglevel=info
) > "$LOG_DIR/celery-beat.log" 2>&1 &
PIDS+=($!)
stream_log "BEAT  " "$C_BEAT" "$LOG_DIR/celery-beat.log"

# Short pause so workers register before frontend starts
sleep 1

# ── 5. Next.js frontend ───────────────────────────────────────────────────────

log "Starting Next.js frontend on :$FRONTEND_PORT ..."
export API_URL="http://localhost:$BACKEND_PORT"
(
  cd "$ROOT/frontend"
  npm run dev -- --port "$FRONTEND_PORT"
) > "$LOG_DIR/frontend.log" 2>&1 &
PIDS+=($!)
stream_log "FRONT " "$C_FRONT" "$LOG_DIR/frontend.log"

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo -e "${DIM}────────────────────────────────────────────────${RESET}"
echo -e "${BOLD}All services launched.${RESET} Press ${BOLD}Ctrl+C${RESET} to stop."
echo ""
echo -e "  ${C_FRONT}${BOLD}Frontend${RESET}   http://localhost:$FRONTEND_PORT/dashboard"
echo -e "  ${C_API}${BOLD}API docs${RESET}    http://localhost:$BACKEND_PORT/docs"
echo -e "  ${C_API}${BOLD}API root${RESET}    http://localhost:$BACKEND_PORT/api"
echo ""
echo -e "  ${DIM}Logs → $LOG_DIR/${RESET}"
echo -e "    backend.log   celery-worker.log   celery-beat.log   frontend.log"
echo ""

# ── Keep alive ────────────────────────────────────────────────────────────────

wait
