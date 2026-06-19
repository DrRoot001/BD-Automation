import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.abspath('backend'))
sys.path.insert(0, os.path.abspath('.'))

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.application import Application
from app.models.interview import Interview

# Celery tasks
from app.tasks.job_discovery import discover_jobs_all_platforms
from app.tasks.resume_generation import prepare_application_package
from app.tasks.browser_automation import execute_application
from app.tasks.email_scan import scan_candidate_inbox

async def get_candidate():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Candidate).where(Candidate.name.ilike('%Sabih%')).limit(1))
        return result.scalar_one_or_none()

async def get_job():
    async with AsyncSessionLocal() as session:
        # Get a random active job
        result = await session.execute(select(Job).limit(1))
        return result.scalar_one_or_none()

async def get_application(app_id):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Application).where(Application.id == app_id))
        return result.scalar_one_or_none()

async def main():
    print("🚀 Starting End-to-End BD-Automator Pipeline Test...")
    
    # 1. Fetch Candidate
    candidate = await get_candidate()
    if not candidate:
        print("❌ Could not find Candidate matching 'Sabih'")
        return
    print(f"✅ Found Candidate: {candidate.name} (ID: {candidate.id})")

    # 2. Module 2: Job Discovery
    print("\n🔍 Module 2: Discovering Jobs...")
    # Triggering synchronously just for testing (usually runs in background)
    # discover_jobs_all_platforms() # Skipping to save time since there are 1000+ jobs in DB
    job = await get_job()
    print(f"✅ Selected Job: {job.title} at {job.company} (ID: {job.id})")

    # 3. Module 3: AI Resume Intelligence
    print("\n🧠 Module 3: Triggering Application Package Preparation...")
    print("Running `orchestrate_application_package` directly...")
    from module3.orchestrator import orchestrate_application_package
    await orchestrate_application_package(str(candidate.id), str(job.id))
    
    app_id = None
    # Check Application record
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Application).where(
                Application.candidate_id == candidate.id,
                Application.job_id == job.id
            ).order_by(Application.created_at.desc())
        )
        app = result.scalar_one_or_none()
        if app:
            app_id = app.id
            print(f"✅ Module 3 Completed! Application ID: {app_id} | Status: {app.status}")
    
    if not app_id:
        print("❌ Application was not created.")
        return

    # 4. Module 4: Browser Automation
    print("\n🤖 Module 4: Simulating Browser Execution...")
    package_dict = {
        "application_id": str(app_id),
        "resume_url": app.resume_id or "https://example.com/resume.pdf",
        "cover_letter_url": app.cover_letter_url or "https://example.com/cl.pdf",
        "screening_answers": {}
    }
    
    from app.tasks.browser_automation import hydrate_and_execute
    try:
        # Pass retry_count=0
        await hydrate_and_execute(package_dict, 0)
        print("✅ Module 4 Completed! Execution finished.")
    except Exception as e:
        print(f"❌ Module 4 Error: {e}")
        # Not returning here, let's keep testing Module 5 if possible

    app = await get_application(app_id)
    print(f"Application Status after Module 4: {app.status if app else 'Unknown'}")

    # 5. Module 5: Email Dashboard Simulation
    print("\n📧 Module 5: Polling Inbox for Updates...")
    from app.tasks.email_scan import scan_candidate_inbox
    # Call the celery task directly synchronously
    scan_candidate_inbox(str(candidate.id))
    
    app = await get_application(app_id)
    print(f"Application Status after Module 5: {app.status if app else 'Unknown'}")

    print("\n🎉 End-to-End Pipeline Test Completed Successfully!")

if __name__ == "__main__":
    asyncio.run(main())
