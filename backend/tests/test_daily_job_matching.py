import pytest
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure backend directory is in the path
HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR.parent))

from app.main import app
from app.database import get_db
from app.models.candidate import Candidate
from app.models.resume import Resume
from app.models.job import Job
from app.models.application import Application
from app.services.matching import run_matching_for_candidate

@pytest.mark.asyncio
@patch("app.services.matching.score_job_fit")
@patch("app.services.matching.orchestrate_application_package")
@patch("app.services.matching.execute_application", create=True)
async def test_run_matching_no_base_resume(mock_execute, mock_prep, mock_score):
    # Test candidate with no base resume is handled gracefully
    db_session = AsyncMock()
    cand = Candidate(id=uuid.uuid4(), name="Alice", email="alice@test.com", tech_stack=["Python"])
    db_session.get.return_value = cand
    
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    db_session.execute.return_value = mock_result
    
    res = await run_matching_for_candidate(cand.id, db_session)
    assert res == {"error": "no_base_resume"}

@pytest.mark.asyncio
@patch("app.services.matching.score_job_fit")
@patch("app.services.matching.orchestrate_application_package")
@patch("app.services.matching.execute_application", create=True)
async def test_run_matching_happy_path(mock_execute, mock_prep, mock_score):
    db_session = AsyncMock()
    cand_id = uuid.uuid4()
    cand = Candidate(id=cand_id, name="Bob", email="bob@test.com", tech_stack=["Python"])
    
    # Mock base resume return
    mock_resume = Resume(
        id=uuid.uuid4(),
        candidate_id=cand_id,
        is_base=True,
        embedding=[0.1]*1536,
        parsed_json={
            "summary": "FastAPI dev",
            "skills": ["Python"],
            "experience": [],
            "education": []
        }
    )
    
    # Mock job return
    mock_job = Job(
        id=uuid.uuid4(),
        title="Python Developer",
        company="Greenhouse",
        source="greenhouse",
        source_url="http://jobs.com/1",
        embedding=[0.1]*1536,
        is_duplicate=False
    )
    
    # Mock the pre-created Application record
    mock_app = Application(
        id=uuid.uuid4(),
        candidate_id=cand_id,
        job_id=mock_job.id,
        status="QUEUED"
    )
    
    # db.get returns candidate, then the Application record when checking for FAILED transition
    db_session.get.side_effect = [cand, mock_app, mock_app]
    
    mock_resume_result = MagicMock()
    mock_resume_result.scalars.return_value.first.return_value = mock_resume
    
    mock_apps_result = MagicMock()
    mock_apps_result.scalars.return_value.all.return_value = []
    
    mock_jobs_result = MagicMock()
    mock_jobs_result.scalars.return_value.all.return_value = [mock_job]
    
    # New flow execute order:
    # 1. Resume query (select Resume)
    # 2. get_active_application_count → select Application (for manual_limit check)
    # 3. All apps for skipped_jobs → select Application
    # 4. Jobs query
    db_session.execute.side_effect = [
        mock_resume_result,  # Resume query
        mock_apps_result,    # get_active_application_count (manual_limit path)
        mock_apps_result,    # Fetch all apps (skipped_jobs)
        mock_jobs_result,    # Jobs query
        mock_apps_result,    # existing_stmt query (returns empty/None)
    ]
    
    # Mock score_job_fit
    mock_match_result = MagicMock()
    mock_match_result.combined_score = 85.0
    mock_match_result.fit_score = 85.0
    mock_match_result.ats_score = 85.0
    mock_match_result.should_apply = True
    mock_match_result.reasoning = "Great fit"
    mock_score.return_value = mock_match_result
    
    # Mock orchestrate_application_package — must accept existing_app_id kwarg
    mock_prep.return_value = {
        "status": "QUEUED",
        "application_id": str(mock_app.id),
        "resume_pdf_url": "http://resume.pdf",
        "cover_letter_url": "http://cover_letter.pdf",
        "screening_answers": {"q1": "a1"}
    }
    
    res = await run_matching_for_candidate(cand_id, db_session, manual_limit=5)
    
    assert res["enqueued_count"] == 1
    assert len(res["details"]) == 1
    assert res["details"][0]["job_title"] == "Python Developer"
    assert res["details"][0]["passed"] is True
    
    mock_score.assert_called_once()
    # orchestrate_application_package must be called with existing_app_id
    mock_prep.assert_called_once()
    call_kwargs = mock_prep.call_args.kwargs
    assert "existing_app_id" in call_kwargs, "orchestrate_application_package must receive existing_app_id"
    # No apply_async — automation is now dispatched via background thread

@pytest.mark.asyncio
@patch("app.services.matching.score_job_fit")
@patch("app.services.matching.orchestrate_application_package")
@patch("app.services.matching.execute_application", create=True)
async def test_run_matching_lookback_windows(mock_execute, mock_prep, mock_score):
    from datetime import datetime, timezone
    
    db_session = AsyncMock()
    cand_id = uuid.uuid4()
    cand = Candidate(id=cand_id, name="Bob", email="bob@test.com", tech_stack=["Python"])
    db_session.get.return_value = cand
    
    mock_resume = Resume(
        id=uuid.uuid4(),
        candidate_id=cand_id,
        is_base=True,
        embedding=[0.1]*1536,
        parsed_json={"summary": "FastAPI dev", "skills": ["Python"], "experience": [], "education": []}
    )
    mock_resume_result = MagicMock()
    mock_resume_result.scalars.return_value.first.return_value = mock_resume
    
    mock_apps_result = MagicMock()
    mock_apps_result.scalars.return_value.all.return_value = []
    
    mock_jobs_result = MagicMock()
    mock_jobs_result.scalars.return_value.all.return_value = []
    
    db_session.execute.side_effect = [
        mock_resume_result,  # Resume query
        mock_apps_result,    # Count apps query
        mock_apps_result,    # Fetch all apps query
        mock_jobs_result     # Jobs query
    ]
    
    # Monday (June 22, 2026)
    monday_now = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    
    class MondayDateTime:
        @classmethod
        def now(cls, tz=None):
            return monday_now
    
    with patch("app.services.matching.datetime", MondayDateTime):
        await run_matching_for_candidate(cand_id, db_session)
        
        # Verify the jobs query is called with Monday lookback
        jobs_stmt = db_session.execute.call_args_list[3][0][0]
        where_clause = str(jobs_stmt.whereclause)
        assert "jobs.created_at >=" in where_clause
    
    # Tuesday (June 23, 2026)
    db_session_tues = AsyncMock()
    db_session_tues.get.return_value = cand
    db_session_tues.execute.side_effect = [
        mock_resume_result,
        mock_apps_result,
        mock_apps_result,
        mock_jobs_result
    ]
    tuesday_now = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)
    
    class TuesdayDateTime:
        @classmethod
        def now(cls, tz=None):
            return tuesday_now
            
    with patch("app.services.matching.datetime", TuesdayDateTime):
        await run_matching_for_candidate(cand_id, db_session_tues)
        
        jobs_stmt_tues = db_session_tues.execute.call_args_list[3][0][0]
        assert "jobs.created_at >=" in str(jobs_stmt_tues.whereclause)
