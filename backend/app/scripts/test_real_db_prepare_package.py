import sys
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Ensure backend directory is in the path
HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parent.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR.parent))

from dotenv import load_dotenv
load_dotenv(BACKEND_DIR / ".env")

import httpx
from app.main import app
from app.database import AsyncSessionLocal
from sqlalchemy import text
from module3.scoring.fit_scorer import MatchResult
from module3.tailoring.resume_tailor import TailoredResume
from module3.cover_letter.generator import CoverLetter
from module3.orchestrator import prepare_package_for_live_application

async def get_first_ids():
    """Query the real DB for the first candidate and job ID."""
    async with AsyncSessionLocal() as session:
        cand_result = await session.execute(text("SELECT id FROM candidates LIMIT 1"))
        cand_row = cand_result.fetchone()
        
        job_result = await session.execute(text("SELECT id FROM jobs LIMIT 1"))
        job_row = job_result.fetchone()
        
        if not cand_row or not job_row:
            raise ValueError("Must have at least one candidate and one job in the database to run this check.")
            
        return str(cand_row[0]), str(job_row[0])

# Store the original class to prevent infinite recursion when instantiation occurs inside the mock
_original_async_client = httpx.AsyncClient

# Wrapper to return httpx client targeting our FastAPI app
def mock_async_client_routing_to_app(*args, **kwargs):
    # Route all HTTP requests in-process to the FastAPI app using ASGITransport
    transport = httpx.ASGITransport(app=app)
    return _original_async_client(transport=transport, base_url="http://test", timeout=120.0)

async def test_real_db_run():
    print("[TEST] Fetching candidate and job from real Supabase DB...")
    try:
        candidate_id, job_id = await get_first_ids()
        print(f"[TEST] Using Candidate: {candidate_id} | Job: {job_id}")
    except Exception as e:
        print(f"[TEST] Error fetching candidate/job: {e}")
        return

    # Check original application state before test execution
    original_app = None
    async with AsyncSessionLocal() as session:
        db_res = await session.execute(
            text("SELECT id, status, resume_id, cover_letter_url FROM applications WHERE candidate_id = :candidate_id AND job_id = :job_id"),
            {"candidate_id": candidate_id, "job_id": job_id}
        )
        row = db_res.fetchone()
        if row:
            original_app = {
                "id": str(row[0]),
                "status": row[1],
                "resume_id": str(row[2]) if row[2] else None,
                "cover_letter_url": row[3]
            }
            print(f"[TEST] Found existing application ID {original_app['id']} in status '{original_app['status']}'")

    # Setup LLM mocks
    mock_score_fit = AsyncMock(return_value=MatchResult(
        job_id=job_id,
        candidate_id=candidate_id,
        fit_score=90.0,
        ats_score=85.0,
        combined_score=87.5,
        should_apply=True,
        matching_skills=["Python"],
        missing_skills=[],
        experience_match=90.0,
        reasoning="Good match"
    ))
    # Version 999 to guarantee uniqueness for clean-up
    mock_tailor_resume = AsyncMock(return_value=TailoredResume(
        candidate_id=candidate_id,
        job_id=job_id,
        original_resume_id="resume-base",
        version=999, 
        ats_score_before=80,
        ats_score_after=95,
        modified_summary="Tailored Summary",
        modified_skills=[],
        modified_keywords=[],
        experience=[],
        education=[],
        pdf_url="http://supabase-storage-mock/resumes/tailored-real-db-v999.pdf"
    ))
    mock_gen_cover = AsyncMock(return_value=CoverLetter(
        candidate_id=candidate_id,
        job_id=job_id,
        content="Dear hiring manager, real DB integration test.",
        pdf_url="http://supabase-storage-mock/cover_letters/cl-real-db.pdf"
    ))
    mock_answer_screening = AsyncMock(return_value={"Screening Question 1?": "Real DB Mock Answer"})

    # Patch AsyncClient to point to FastAPI app in-process, and patch LLM calls in module3.orchestrator
    with patch("module3.orchestrator.httpx.AsyncClient", side_effect=mock_async_client_routing_to_app), \
         patch("module3.orchestrator.score_job_fit", mock_score_fit), \
         patch("module3.orchestrator.tailor_resume", mock_tailor_resume), \
         patch("module3.orchestrator.generate_cover_letter", mock_gen_cover), \
         patch("module3.orchestrator.answer_screening_questions", mock_answer_screening):
         
        try:
            print("[TEST] Calling prepare_package_for_live_application synchronously...")
            result = await prepare_package_for_live_application(
                candidate_id=candidate_id,
                job_id=job_id,
                needs_cover_letter=True,
                screening_questions=["Screening Question 1?"],
                api_base_url="http://test" # Matches mock client
            )
            
            print("\n==================================================")
            print("REAL DB INTEGRATION RUN COMPLETED")
            print("==================================================")
            print("Result should_apply:", result.get("should_apply"))
            print("Resume URL written:", result.get("resume_pdf_url"))
            print("Cover Letter URL written:", result.get("cover_letter_pdf_url"))
            print("Screening Answers:", result.get("screening_answers"))
            
            # Verify status in DB
            async with AsyncSessionLocal() as session:
                db_res = await session.execute(
                    text("SELECT status, resume_id, cover_letter_url FROM applications WHERE candidate_id = :candidate_id AND job_id = :job_id"),
                    {"candidate_id": candidate_id, "job_id": job_id}
                )
                app_row = db_res.fetchone()
                print("\n[DB VERIFICATION] Current Application Row in Supabase:")
                print("  Status in DB:", app_row[0] if app_row else "Not found")
                print("  Resume ID in DB:", app_row[1] if app_row else "Not found")
                print("  Cover Letter URL in DB:", app_row[2] if app_row else "Not found")
        finally:
            # Cleanup phase (guaranteed execution)
            print("\n[TEST-CLEANUP] Initiating database cleanup...")
            async with AsyncSessionLocal() as session:
                # 1. Reset or delete application status changes first (clears the foreign key reference)
                if original_app:
                    print(f"  Restoring original status of application ID {original_app['id']} to '{original_app['status']}'...")
                    await session.execute(
                        text("UPDATE applications SET status=:status, resume_id=:resume_id, cover_letter_url=:cl_url WHERE id=:id"),
                        {
                            "status": original_app["status"],
                            "resume_id": original_app["resume_id"],
                            "cl_url": original_app["cover_letter_url"],
                            "id": original_app["id"]
                        }
                    )
                else:
                    # Dynamically query for any application created during execution
                    print("  Checking for any application record created during this test run...")
                    db_check = await session.execute(
                        text("SELECT id FROM applications WHERE candidate_id = :candidate_id AND job_id = :job_id"),
                        {"candidate_id": candidate_id, "job_id": job_id}
                    )
                    created_row = db_check.fetchone()
                    if created_row:
                        created_app_id = str(created_row[0])
                        print(f"  Deleting newly created application record ID {created_app_id}...")
                        # Delete related history first (table: application_history)
                        await session.execute(
                            text("DELETE FROM application_history WHERE application_id = :app_id"),
                            {"app_id": created_app_id}
                        )
                        await session.execute(
                            text("DELETE FROM applications WHERE id = :app_id"),
                            {"app_id": created_app_id}
                        )

                # 2. Delete the tailored resume (version 999) now that the FK reference is cleared
                print(f"  Deleting tailored resume (version 999) for candidate {candidate_id}...")
                await session.execute(
                    text("DELETE FROM resumes WHERE candidate_id = :candidate_id AND version = 999"),
                    {"candidate_id": candidate_id}
                )
                
                await session.commit()
            print("[TEST-CLEANUP] Database restored to original state successfully.")

if __name__ == "__main__":
    asyncio.run(test_real_db_run())
