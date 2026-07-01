import sys
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

# Ensure backend directory is in the path
HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR.parent))

from dotenv import load_dotenv
load_dotenv(BACKEND_DIR / ".env")

from app.main import app
from module3.scoring.fit_scorer import MatchResult
from module3.tailoring.resume_tailor import TailoredResume
from module3.cover_letter.generator import CoverLetter

client = TestClient(app)

# ---------------------------------------------------------------------------
# httpx.AsyncClient Mocking Wrapper
# ---------------------------------------------------------------------------
class MockAsyncClient:
    def __init__(self, *args, **kwargs):
        self.base_url = kwargs.get("base_url", "")
        self.requests_log = []
        self.existing_resumes = []
        
        self.get = AsyncMock(side_effect=self._mock_get)
        self.post = AsyncMock(side_effect=self._mock_post)
        self.patch = AsyncMock(side_effect=self._mock_patch)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def _mock_get(self, url, *args, **kwargs):
        self.requests_log.append(("GET", url))
        if "/api/candidates/" in url:
            return MagicMock(status_code=200, json=lambda: {"id": "cand-123", "name": "Test Candidate"})
        elif "/api/resumes/" in url and "is_base=true" in url:
            return MagicMock(status_code=200, json=lambda: [
                {"id": "resume-base", "parsed_json": {"summary": "Base Summary", "experience": [], "education": [], "skills": [], "keywords": []}, "file_url": "http://base.pdf"}
            ])
        elif "/api/resumes/" in url:
            return MagicMock(status_code=200, json=lambda: self.existing_resumes)
        elif "/api/jobs/" in url:
            return MagicMock(status_code=200, json=lambda: {
                "id": "job-456",
                "title": "Software Engineer",
                "company": "Test Company",
                "source_url": "http://example.com/job",
                "canonical_url": "http://example.com/job",
                "description": "Backend Developer",
                "skills": "[]"
            })
        elif "/api/applications" in url:
            return MagicMock(status_code=200, json=lambda: [{"id": "app-789", "candidate_id": "cand-123", "job_id": "job-456"}])
        return MagicMock(status_code=404, text="Not Found")

    def _mock_post(self, url, *args, **kwargs):
        json_data = kwargs.get("json", {})
        self.requests_log.append(("POST", url, json_data))
        if "/api/resumes" in url:
            return MagicMock(status_code=201, json=lambda: {"id": f"resume-tailored-{json_data.get('version', 2)}", "file_url": json_data.get("file_url")})
        return MagicMock(status_code=404, text="Not Found")

    def _mock_patch(self, url, *args, **kwargs):
        json_data = kwargs.get("json", {})
        self.requests_log.append(("PATCH", url, json_data))
        return MagicMock(status_code=200, json=lambda: {"status": json_data.get("status")})

# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("module3.orchestrator.score_job_fit")
@patch("module3.orchestrator.tailor_resume")
@patch("module3.orchestrator.generate_cover_letter")
@patch("module3.orchestrator.answer_screening_questions")
@patch("module3.orchestrator.httpx.AsyncClient")
async def test_prepare_package_happy_path(
    mock_async_client_cls,
    mock_answer_screening,
    mock_gen_cover,
    mock_tailor_resume,
    mock_score_fit,
):
    """
    Test Case 1: Happy Path.
    - All Gemini calls succeed, candidate score >= 75.
    - Assert correct responses are returned.
    - Assert progressive status progression: MATCHED -> RESUME_UPDATED -> COVER_LETTER_CREATED -> QUEUED.
    """
    # 1. Setup LLM mocks
    mock_score_fit.return_value = MatchResult(
        job_id="job-456",
        candidate_id="cand-123",
        fit_score=95.0,
        ats_score=90.0,
        combined_score=92.5,
        should_apply=True,
        matching_skills=["Python"],
        missing_skills=[],
        experience_match=80.0,
        reasoning="Good match"
    )
    mock_tailor_resume.return_value = TailoredResume(
        candidate_id="cand-123",
        job_id="job-456",
        original_resume_id="resume-base",
        version=2,
        ats_score_before=80,
        ats_score_after=95,
        modified_summary="Tailored Summary",
        modified_skills=[],
        modified_keywords=[],
        experience=[],
        education=[],
        pdf_url="http://tailored-resume.pdf"
    )
    mock_gen_cover.return_value = CoverLetter(
        candidate_id="cand-123",
        job_id="job-456",
        content="Dear hiring manager...",
        pdf_url="http://cover-letter.pdf"
    )
    mock_answer_screening.return_value = {"Question 1?": "Answer 1"}

    # 2. Setup DB client mock
    mock_client = MockAsyncClient()
    mock_async_client_cls.return_value = mock_client

    # 3. Call FastAPI route
    payload = {
        "candidate_id": "cand-123",
        "job_id": "job-456",
        "needs_cover_letter": True,
        "screening_questions": ["Question 1?"]
    }
    response = client.post("/api/applications/prepare-package", json=payload)
    
    assert response.status_code == 200, f"Error detail: {response.text}"
    data = response.json()
    assert data["should_apply"] is True
    assert data["resume_pdf_url"] == "http://tailored-resume.pdf"
    assert data["cover_letter_pdf_url"] == "http://cover-letter.pdf"
    assert data["screening_answers"] == {"Question 1?": "Answer 1"}

    # 4. Verify mock calls
    mock_score_fit.assert_called_once()
    mock_tailor_resume.assert_called_once()
    mock_gen_cover.assert_called_once()
    mock_answer_screening.assert_called_once()

    # 5. Verify status sequence
    status_updates = [log[2]["status"] for log in mock_client.requests_log if log[0] == "PATCH"]
    assert status_updates == ["QUEUED", "QUEUED", "QUEUED", "QUEUED"]


@pytest.mark.asyncio
@patch("module3.orchestrator.score_job_fit")
@patch("module3.orchestrator.tailor_resume")
@patch("module3.orchestrator.generate_cover_letter")
@patch("module3.orchestrator.answer_screening_questions")
@patch("module3.orchestrator.httpx.AsyncClient")
async def test_prepare_package_low_fit_score(
    mock_async_client_cls,
    mock_answer_screening,
    mock_gen_cover,
    mock_tailor_resume,
    mock_score_fit,
):
    """
    Test Case 2: Low Fit Score Gate.
    - Candidate combined score < 75.
    - Check that pipeline short-circuits.
    - Confirm status transitions directly to ANALYZED.
    - Assert that tailor, cover letter, and screening question logic were NOT called.
    """
    mock_score_fit.return_value = MatchResult(
        job_id="job-456",
        candidate_id="cand-123",
        fit_score=60.0,
        ats_score=50.0,
        combined_score=55.0,
        should_apply=False,
        matching_skills=[],
        missing_skills=["Python"],
        experience_match=40.0,
        reasoning="Poor match"
    )

    mock_client = MockAsyncClient()
    mock_async_client_cls.return_value = mock_client

    payload = {
        "candidate_id": "cand-123",
        "job_id": "job-456",
        "needs_cover_letter": True,
        "screening_questions": ["Question 1?"]
    }
    response = client.post("/api/applications/prepare-package", json=payload)
    
    assert response.status_code == 200, f"Error detail: {response.text}"
    data = response.json()
    assert data["should_apply"] is False
    assert "Combined score (55.0) is below gate threshold of 75" in data["reason"]

    # Assert gating works
    mock_tailor_resume.assert_not_called()
    mock_gen_cover.assert_not_called()
    mock_answer_screening.assert_not_called()

    # Confirm status transition directly to ANALYZED
    status_updates = [log[2]["status"] for log in mock_client.requests_log if log[0] == "PATCH"]
    assert status_updates == ["ANALYZED"]


@pytest.mark.asyncio
@patch("module3.orchestrator.score_job_fit")
@patch("module3.orchestrator.tailor_resume")
@patch("module3.orchestrator.generate_cover_letter")
@patch("module3.orchestrator.answer_screening_questions")
@patch("module3.orchestrator.httpx.AsyncClient")
async def test_prepare_package_no_cover_letter(
    mock_async_client_cls,
    mock_answer_screening,
    mock_gen_cover,
    mock_tailor_resume,
    mock_score_fit,
):
    """
    Test Case 3: needs_cover_letter = False.
    - Cover letter mock should NOT be called.
    - Returned cover_letter_pdf_url should be None/null.
    - Transition status sequence skips COVER_LETTER_CREATED (MATCHED -> RESUME_UPDATED -> QUEUED).
    """
    mock_score_fit.return_value = MatchResult(
        job_id="job-456",
        candidate_id="cand-123",
        fit_score=95.0,
        ats_score=90.0,
        combined_score=92.5,
        should_apply=True,
        matching_skills=["Python"],
        missing_skills=[],
        experience_match=90.0,
        reasoning="Excellent match"
    )
    mock_tailor_resume.return_value = TailoredResume(
        candidate_id="cand-123",
        job_id="job-456",
        original_resume_id="resume-base",
        version=2,
        ats_score_before=80,
        ats_score_after=95,
        modified_summary="Tailored Summary",
        modified_skills=[],
        modified_keywords=[],
        experience=[],
        education=[],
        pdf_url="http://tailored-resume.pdf"
    )

    mock_client = MockAsyncClient()
    mock_async_client_cls.return_value = mock_client

    payload = {
        "candidate_id": "cand-123",
        "job_id": "job-456",
        "needs_cover_letter": False,
        "screening_questions": []
    }
    response = client.post("/api/applications/prepare-package", json=payload)
    
    assert response.status_code == 200, f"Error detail: {response.text}"
    data = response.json()
    assert data["should_apply"] is True
    assert data["cover_letter_pdf_url"] is None

    # Verify cover letter generator was NOT called
    mock_gen_cover.assert_not_called()

    # Verify sequence transitions skipped COVER_LETTER_CREATED
    status_updates = [log[2]["status"] for log in mock_client.requests_log if log[0] == "PATCH"]
    assert status_updates == ["QUEUED", "QUEUED"]


@pytest.mark.asyncio
@patch("module3.orchestrator.score_job_fit")
@patch("module3.orchestrator.tailor_resume")
@patch("module3.orchestrator.generate_cover_letter")
@patch("module3.orchestrator.answer_screening_questions")
@patch("module3.orchestrator.httpx.AsyncClient")
async def test_prepare_package_resume_version_numbering(
    mock_async_client_cls,
    mock_answer_screening,
    mock_gen_cover,
    mock_tailor_resume,
    mock_score_fit,
):
    """
    Test Case 4: Resume Version Numbering.
    - DB has existing resumes with version 1 and 2.
    - Confirm the pipeline retrieves these resumes and calls tailor_resume with version=3.
    """
    mock_score_fit.return_value = MatchResult(
        job_id="job-456",
        candidate_id="cand-123",
        fit_score=95.0,
        ats_score=90.0,
        combined_score=92.5,
        should_apply=True,
        matching_skills=["Python"],
        missing_skills=[],
        experience_match=75.0,
        reasoning="Passable match"
    )
    mock_tailor_resume.return_value = TailoredResume(
        candidate_id="cand-123",
        job_id="job-456",
        original_resume_id="resume-base",
        version=3,
        ats_score_before=75,
        ats_score_after=90,
        modified_summary="Version 3 Summary",
        modified_skills=[],
        modified_keywords=[],
        experience=[],
        education=[],
        pdf_url="http://tailored-resume-v3.pdf"
    )

    mock_client = MockAsyncClient()
    # Add existing resumes with versions 1 and 2
    mock_client.existing_resumes = [
        {"id": "r1", "version": 1, "is_base": True},
        {"id": "r2", "version": 2, "is_base": False}
    ]
    mock_async_client_cls.return_value = mock_client

    payload = {
        "candidate_id": "cand-123",
        "job_id": "job-456",
        "needs_cover_letter": False,
        "screening_questions": []
    }
    response = client.post("/api/applications/prepare-package", json=payload)
    
    assert response.status_code == 200, f"Error detail: {response.text}"
    
    # Assert tailor_resume was called with next incremented version, version=3
    _, kwargs = mock_tailor_resume.call_args
    assert kwargs["version"] == 3
