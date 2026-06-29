@echo off
REM ===========================================================================
REM  run_all.bat — double-click to start the entire BD-Automator stack.
REM  (backend + celery worker + celery beat + frontend)
REM  Runs run_all.ps1 with the execution policy bypassed so no setup is needed.
REM ===========================================================================
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_all.ps1" %*
