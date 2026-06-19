import asyncio
import os
import sys
import logging
from uuid import uuid4
from pathlib import Path

# Ensure root and backend directories are in python search path
HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parent.parent
sys.path.insert(0, str(BACKEND_DIR))         # Path to app
sys.path.insert(0, str(BACKEND_DIR.parent))  # Path to module4

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from dotenv import load_dotenv

# Set dry run env variables
os.environ["DRY_RUN_NO_SUBMIT"] = "true"

# Load env configurations
env_path = str(BACKEND_DIR / ".env")
load_dotenv(dotenv_path=env_path)

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# Configure loggers — INFO to console, DEBUG to file so every field action is captured
LOG_FILE = "/tmp/m4_final.log"
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

_file_handler = logging.FileHandler(LOG_FILE, mode="w")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_fmt)

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(_fmt)

_root = logging.getLogger()
_root.setLevel(logging.DEBUG)
_root.addHandler(_file_handler)
_root.addHandler(_console_handler)

# Enable DEBUG for browser_automation submodules so every fill/upload action is logged to file
for _m in ("app.browser_automation.adapters.greenhouse", "app.browser_automation.forms.filler",
           "app.browser_automation.forms.uploader", "app.browser_automation.forms.detector"):
    logging.getLogger(_m).setLevel(logging.DEBUG)

logger = logging.getLogger("run_live_m4_test")
logger.info(f"Logs will be written to: {LOG_FILE}")

CANDIDATE_ID = "3e8b9e17-755f-4c57-b165-83345c4d2b55"
JOB_ID = "09c52692-5c97-46b8-94a3-e8e3ddaf61e1"
APPLICATION_ID = None
ORIGINAL_APP = None
TAILORED_RESUME_ID = None

async def run_test():
    global APPLICATION_ID, ORIGINAL_APP, TAILORED_RESUME_ID
    
    if not DATABASE_URL:
        print("Error: DATABASE_URL is not set.")
        sys.exit(1)
        
    engine = create_async_engine(DATABASE_URL)
    
    print(f"\n[TEST] Running M4 browser execution against Real Supabase DB & Real Gemini...")
    print(f"[TEST] Target Candidate: {CANDIDATE_ID} | Target Job: {JOB_ID}")
    
    try:
        # 1. Inspect existing state in DB
        async with engine.connect() as conn:
            # Check candidate details
            cand_res = await conn.execute(
                text("SELECT name, email, phone FROM candidates WHERE id = :cid"),
                {"cid": CANDIDATE_ID}
            )
            candidate = cand_res.fetchone()
            if not candidate:
                raise ValueError(f"Candidate {CANDIDATE_ID} not found in DB.")
            print(f"[TEST] Using Candidate: {candidate.name} ({candidate.email})")

            # Check job details
            job_res = await conn.execute(
                text("SELECT title, company, source_url FROM jobs WHERE id = :jid"),
                {"jid": JOB_ID}
            )
            job = job_res.fetchone()
            if not job:
                raise ValueError(f"Job {JOB_ID} not found in DB.")
            print(f"[TEST] Using Job: {job.title} at {job.company} ({job.source_url})")

            # Fetch or check pre-existing application
            app_res = await conn.execute(
                text("SELECT id, status, resume_id, cover_letter_url FROM applications WHERE candidate_id = :cid AND job_id = :jid"),
                {"cid": CANDIDATE_ID, "jid": JOB_ID}
            )
            app_row = app_res.fetchone()
            if app_row:
                ORIGINAL_APP = {
                    "id": app_row.id,
                    "status": app_row.status,
                    "resume_id": app_row.resume_id,
                    "cover_letter_url": app_row.cover_letter_url
                }
                APPLICATION_ID = app_row.id
                print(f"[TEST] Found existing application ID {APPLICATION_ID} with status '{app_row.status}'")
                
                # RESET APPLICATION STATUS TO QUEUED TO ALLOW STATE TRANSITIONS
                async with engine.begin() as wconn:
                    await wconn.execute(
                        text("UPDATE applications SET status = 'QUEUED' WHERE id = :aid"),
                        {"aid": APPLICATION_ID}
                    )
                print("[TEST] Reset existing application status to 'QUEUED' for clean start transitions.")
            else:
                APPLICATION_ID = str(uuid4())
                print(f"[TEST] No existing application found. Preparing to cleanup new application row ID: {APPLICATION_ID}")

        # 2. Trigger M4 ApplicationExecutor via direct import (runs Playwright + calls /prepare-package REST API)
        from app.browser_automation.services.executor import ApplicationExecutor, ApplicationPackage
        
        REAL_RESUME = "/Users/sabihhaider/Documents/BD-Automator-Agent/Sabih Haider — Software Engineer _ Full-Stack Web Developer.pdf"

        # Candidate profile — uses real identity for form filling
        candidate_profile = {
            "first_name": "Sabih",
            "last_name": "Haider",
            "email": candidate.email,  # sabih0364@gmail.com
            "phone": candidate.phone or "+923000000000",
            "linkedin_url": "https://www.linkedin.com/in/sabihhaider/",
            "website": "https://sabihhaider.dev",
            "location": "Remote",
        }

        package = ApplicationPackage(
            application_id=str(APPLICATION_ID),
            candidate_id=CANDIDATE_ID,
            job_id=JOB_ID,
            job_url=job.source_url,
            platform="greenhouse",
            resume_url=REAL_RESUME,
            candidate_profile=candidate_profile
        )
        
        executor = ApplicationExecutor()
        print("\n--- BEGIN BROWSER AUTOMATION RUN ---")
        result = await executor.execute(package)
        print("--- END BROWSER AUTOMATION RUN ---\n")
        
        print("==================================================")
        print("REAL PLAYWRIGHT & M3 RUN COMPLETED")
        print("==================================================")
        print(f"Result status: {result.status}")
        print(f"Screenshot URL: {result.screenshot_url}")
        print(f"Error Message: {result.error_message}")
        print(f"Confirmation: {result.confirmation_text}")
        print("==================================================")

        # 3. Verify values updated in DB
        async with engine.connect() as conn:
            updated_res = await conn.execute(
                text("SELECT status, resume_id, cover_letter_url FROM applications WHERE candidate_id = :cid AND job_id = :jid"),
                {"cid": CANDIDATE_ID, "jid": JOB_ID}
            )
            up_row = updated_res.fetchone()
            if up_row:
                print("\n[DB VERIFICATION] Current Application Row in Supabase:")
                print(f"  Executor-reported status: {result.status}")
                print(f"  Actual DB status: {up_row.status}")
                print(f"  Resume ID in DB: {up_row.resume_id}")
                print(f"  Cover Letter URL in DB: {up_row.cover_letter_url}")
                TAILORED_RESUME_ID = up_row.resume_id
                assert result.status == up_row.status, f"Status mismatch: Executor-reported={result.status}, DB={up_row.status}"
            else:
                print("\n[DB VERIFICATION] Warning: Application row not found in DB!")
                raise AssertionError("Application row not found in DB at verification step!")

    finally:
        print("\n[TEST-CLEANUP] Initiating database cleanup...")
        async with engine.begin() as conn:
            # 1. Clean history
            if APPLICATION_ID:
                hist_del = await conn.execute(
                    text("DELETE FROM application_history WHERE application_id = :aid"),
                    {"aid": APPLICATION_ID}
                )
                print(f"  Deleted {hist_del.rowcount} application_history records.")
            
            # 2. Reset or delete application
            if ORIGINAL_APP:
                print(f"  Restoring original status of application ID {ORIGINAL_APP['id']} to '{ORIGINAL_APP['status']}'...")
                await conn.execute(
                    text("UPDATE applications SET status = :status, resume_id = :rid, cover_letter_url = :cl WHERE id = :aid"),
                    {
                        "status": ORIGINAL_APP["status"],
                        "rid": ORIGINAL_APP["resume_id"],
                        "cl": ORIGINAL_APP["cover_letter_url"],
                        "aid": ORIGINAL_APP["id"]
                    }
                )
            else:
                if APPLICATION_ID:
                    print(f"  Deleting newly created application row ID: {APPLICATION_ID}")
                    await conn.execute(
                        text("DELETE FROM applications WHERE id = :aid"),
                        {"aid": APPLICATION_ID}
                    )
            
            # 3. Delete tailored resumes that were created
            res_del = await conn.execute(
                text("DELETE FROM resumes WHERE candidate_id = :cid AND tailored_for_job_id = :jid AND is_base = false"),
                {"cid": CANDIDATE_ID, "jid": JOB_ID}
            )
            print(f"  Deleted {res_del.rowcount} tailored resume rows.")
            
        print("[TEST-CLEANUP] Database restored to original state successfully.\n")
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(run_test())
