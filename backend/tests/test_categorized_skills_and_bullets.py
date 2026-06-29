import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from module3.parser.resume_parser import ResumeData, ResumeSection, ExperienceEntry, EducationEntry, SkillGroup
from module2.normalization.schemas import NormalizedJob
from module3.tailoring.resume_tailor import tailor_resume
from module3.tailoring.pdf_generator import generate_resume_pdf

def test_experience_entry_synchronization():
    # 1. Test populating bullets from description
    entry1 = ExperienceEntry(
        company="Company A",
        title="Engineer",
        start_date="2020",
        description="• Line one\n• Line two\n- Line three",
        technologies=[]
    )
    assert entry1.bullets == ["Line one", "Line two", "Line three"]

    # 2. Test populating description from bullets
    entry2 = ExperienceEntry(
        company="Company B",
        title="Developer",
        start_date="2021",
        description="",
        bullets=["First bullet", "Second bullet"],
        technologies=[]
    )
    assert "• First bullet" in entry2.description
    assert "• Second bullet" in entry2.description


def test_resume_section_skills_synchronization():
    # 1. Test backwards compatibility: populate skills_categorized from skills
    section1 = ResumeSection(
        summary="Summary text",
        skills=["Python", "Go", "Docker"],
        experience=[],
        education=[]
    )
    assert len(section1.skills_categorized) == 1
    assert section1.skills_categorized[0].category == "Core Skills"
    assert section1.skills_categorized[0].keywords == ["Python", "Go", "Docker"]

    # 2. Test new format: populate skills from skills_categorized
    section2 = ResumeSection(
        summary="Summary text",
        skills=[],
        skills_categorized=[
            SkillGroup(category="Languages", keywords=["Python", "Go"]),
            SkillGroup(category="DevOps", keywords=["Docker"])
        ],
        experience=[],
        education=[]
    )
    assert section2.skills == ["Python", "Go", "Docker"]


@pytest.mark.asyncio
@patch("module3.tailoring.resume_tailor.calculate_ats_score")
@patch("module3.tailoring.resume_tailor.generate_resume_pdf")
@patch("module3.utils.gemini.generate_content_with_retry")
async def test_tailor_resume_categorized_skills_and_bullets(
    mock_generate_content,
    mock_generate_pdf,
    mock_calculate_ats
):
    # Mock ATS Score to exit immediately
    mock_ats_obj = MagicMock()
    mock_ats_obj.overall = 95.0
    mock_ats_obj.missing_keywords = []
    mock_calculate_ats.return_value = mock_ats_obj

    # Base resume with categorized skills and bulleted experience
    resume = ResumeData(
        file_url="http://example.com/resume.pdf",
        raw_text="Test content",
        sections=ResumeSection(
            summary="Test summary",
            skills=[],
            skills_categorized=[
                SkillGroup(category="Languages", keywords=["Python", "C++"]),
                SkillGroup(category="Databases", keywords=["PostgreSQL"])
            ],
            experience=[
                ExperienceEntry(
                    company="Mavericks United",
                    title="Senior Engineer",
                    start_date="2025",
                    bullets=["Led development.", "Architected DB."],
                    technologies=["Python"]
                )
            ],
            education=[]
        )
    )

    job = NormalizedJob(
        job_id="job-abc",
        company="Tech Corp",
        title="Full Stack Engineer",
        description="Requirement: Python",
        skills=["Python"],
        location="Remote",
        url="http://example.com/job",
        source="lever"
    )

    result = await tailor_resume(
        resume=resume,
        job=job,
        candidate_profile={"name": "Ammar Hanif", "email": "ammar@example.com"},
        output_pdf_dir="backend/data/tailored_resumes"
    )

    # Verify return object maps properties correctly
    assert result.modified_skills == ["Python", "C++", "PostgreSQL"]
    assert len(result.modified_skills_categorized) == 2
    assert result.modified_skills_categorized[0].category == "Languages"
    assert result.modified_skills_categorized[0].keywords == ["Python", "C++"]
    
    assert len(result.experience) == 1
    assert result.experience[0].bullets == ["Led development.", "Architected DB."]

    # Verify generate_resume_pdf was called with structured skills and bullets
    mock_generate_pdf.assert_called_once()
    _, kwargs = mock_generate_pdf.call_args
    assert kwargs.get("skills") == [
        {"category": "Languages", "keywords": ["Python", "C++"]},
        {"category": "Databases", "keywords": ["PostgreSQL"]}
    ]
    assert kwargs.get("experience")[0]["bullets"] == ["Led development.", "Architected DB."]
