import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from module3.parser.resume_parser import ResumeData, ResumeSection, EducationEntry
from module2.normalization.schemas import NormalizedJob
from module3.tailoring.resume_tailor import tailor_resume

@pytest.mark.asyncio
@patch("module3.tailoring.resume_tailor.calculate_ats_score")
@patch("module3.tailoring.resume_tailor.generate_resume_pdf")
@patch("module3.utils.gemini.generate_content_with_retry")
async def test_tailor_resume_education_split(
    mock_generate_content,
    mock_generate_pdf,
    mock_calculate_ats
):
    # Mock ATS Score to be 95 (so we exit the loop immediately)
    mock_ats_obj = MagicMock()
    mock_ats_obj.overall = 95.0
    mock_ats_obj.missing_keywords = []
    mock_calculate_ats.return_value = mock_ats_obj

    # Create mock resume input with degree and field separated
    resume = ResumeData(
        file_url="http://example.com/resume.pdf",
        raw_text="Test resume content",
        sections=ResumeSection(
            summary="Test summary",
            skills=["Python"],
            experience=[],
            education=[
                EducationEntry(
                    institution="Stanford University",
                    degree="BS",
                    field="Computer Science",
                    graduation_year=2020
                )
            ]
        )
    )

    job = NormalizedJob(
        job_id="job-123",
        company="Test Co",
        title="Software Engineer",
        description="Write Python code",
        skills=["Python"],
        location="USA",
        url="http://example.com/job",
        source="greenhouse"
    )

    candidate_profile = {
        "name": "Sabih Haider",
        "email": "sabih@example.com"
    }

    # Run tailor_resume
    # It should:
    # 1. Combine degree & field to "BS in Computer Science" in initial payload.
    # 2. Skip the while loop since ATS score is >= 90.
    # 3. Reconstruct final_education and pdf_education.
    # Since the while loop is skipped, final_resume_json is resume_json (the combined one).
    # Since our parsing splits "BS in Computer Science" back, it should restore them!
    
    result = await tailor_resume(
        resume=resume,
        job=job,
        candidate_profile=candidate_profile,
        output_pdf_dir="backend/data/tailored_resumes"
    )

    # Assertions
    assert len(result.education) == 1
    assert result.education[0].degree == "BS"
    assert result.education[0].field == "Computer Science"
    assert result.education[0].institution == "Stanford University"
    assert result.education[0].graduation_year == 2020

    # Assert pdf_education was generated with combined degree name
    mock_generate_pdf.assert_called_once()
    _, kwargs = mock_generate_pdf.call_args
    pdf_education = kwargs.get("education")
    assert len(pdf_education) == 1
    assert pdf_education[0]["degree"] == "BS in Computer Science"
    assert pdf_education[0]["institution"] == "Stanford University"
