#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "🚀 Starting BD-Automator Stack (Local Mode)..."

# Ensure we clean up all background processes when the script is stopped
trap 'echo -e "\n🛑 Stopping all services..."; kill $(jobs -p) 2>/dev/null || true; exit' SIGINT SIGTERM EXIT

# Check if required environment files exist
if [ ! -f "backend/.env" ]; then
    echo "⚠️  Warning: backend/.env not found. Make sure DATABASE_URL and REDIS_URL are set."
fi

# Ensure Node is in PATH
export PATH="/usr/local/bin:$HOME/.nvm/versions/node/v20.19.5/bin:$PATH"

# Determine python command
PYTHON="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
CELERY="$PYTHON -m celery"
UVICORN="$PYTHON -m uvicorn"

echo "Using Python env: $PYTHON"

# Export PYTHONPATH so the backend can find module3 and module5
export PYTHONPATH="$(pwd):$PYTHONPATH"

# 1. Start FastAPI Backend
echo "📦 Starting FastAPI Backend (Port 8000)..."
(cd backend && $UVICORN app.main:app --host 0.0.0.0 --port 8000 --reload) &

# Wait a moment for backend to initialize
sleep 2

# 2. Start Celery Worker
echo "👷 Starting Celery Worker..."
(cd backend && $CELERY -A app.celery_app worker --loglevel=info -Q celery,queue:job_discovery,queue:job_processing,queue:resume_generation,queue:application_execution,queue:email_scan) &

# 3. Start Celery Beat
echo "⏱️ Starting Celery Beat Scheduler..."
(cd backend && $CELERY -A app.celery_app beat --loglevel=info) &

# 4. Start Next.js Frontend
echo "💻 Starting Next.js Frontend (Port 3000)..."
(cd frontend && npm run dev) &

echo ""
echo "✅ All services are launching! Press Ctrl+C at any time to stop everything."
echo "   - 📊 Frontend Dashboard: http://localhost:3000/dashboard"
echo "   - 🔌 Backend API Docs:   http://localhost:8000/docs"
echo ""

# Wait for all background jobs to finish (which is never, until interrupted)
wait
