@echo off
REM ===========================================================================
REM  stop_all.bat — double-click to stop every BD-Automator service that
REM  run_all started (backend, celery worker, celery beat, frontend).
REM ===========================================================================
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_all.ps1" -Stop
pause
