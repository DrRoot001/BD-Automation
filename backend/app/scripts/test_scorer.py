import asyncio
import json
import os
import sys
from pathlib import Path

# Setup paths
HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "backend"))

# Load dotenv to get GEMINI_API_KEY
from dotenv import load_dotenv
load_dotenv(ROOT_DIR / "backend" / ".env")

from module3.parser.resume_parser import ResumeData, ResumeSection
from module2.normalization.schemas import NormalizedJob
from module3.scoring.fit_scorer import score_job_fit

async def main():
    # Load resume
    resume_json_path = ROOT_DIR / "parsed_resume.json"
    if not resume_json_path.exists():
        print(f"✗ Parsed resume JSON not found at: {resume_json_path}. Please run test_resume_parser.py first.")
        return
        
    with open(resume_json_path, "r", encoding="utf-8") as f:
        resume_section_data = json.load(f)
    
    sections = ResumeSection(**resume_section_data)
    resume_data = ResumeData(
        candidate_id="candidate-sabih",
        resume_id="resume-sabih",
        file_url=str(ROOT_DIR / "Sabih Haider — Software Engineer _ Full-Stack Web Developer-compressed.pdf"),
        sections=sections,
        raw_text="[Mock parsed resume raw text]"
    )

    # Load job
    jobs_json_path = ROOT_DIR / "scraped_jobs.json"
    if not jobs_json_path.exists():
        print(f"✗ Scraped jobs JSON not found at: {jobs_json_path}. Please run scrape_and_save.py first.")
        return
        
    with open(jobs_json_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)
        
    if not jobs:
        print("✗ No jobs in scraped_jobs.json")
        return
        
    # Get the first job from the scraped list (Stripe)
    job_data = jobs[26]
    
    # Set skills to list if stored as string, parse list if needed
    skills = job_data.get("skills")
    if isinstance(skills, str):
        skills = json.loads(skills)
    elif not skills:
        skills = []
        
    job = NormalizedJob(
        title=job_data.get("title", "Software Engineer"),
        company=job_data.get("company", "Stripe"),
        location=job_data.get("location", "Remote"),
        url=job_data.get("url", ""),
        description=job_data.get("description", "Requires React, TypeScript, APIs and Postgres."),
        source=job_data.get("source", "greenhouse"),
        skills=skills,
        salary_min=job_data.get("salary_min"),
        salary_max=job_data.get("salary_max"),
        pay_period=job_data.get("pay_period", "yearly"),
        job_type=job_data.get("job_type", "full-time"),
        posted_at=datetime.utcnow() if not job_data.get("posted_at") else datetime.fromisoformat(job_data.get("posted_at")),
        canonical_url=job_data.get("canonical_url", ""),
        embedding=job_data.get("embedding"),
        source_url=job_data.get("source_url", "")
    )
    job.job_id = "job-stripe-test"

    # Mock candidate profile matching the parsed resume
    candidate = {
        "id": "candidate-sabih",
        "name": "Sabih Haider",
        "email": "sabih@example.com",
        "tech_stack": sections.skills,
        "years_exp": 2,
        "location": "US",
        "work_auth": "us_authorized"
    }

    print("--------------------------------------------------")
    print(f"Scoring fit for Candidate '{candidate['name']}' against Job '{job.title}' at '{job.company}'")
    print("--------------------------------------------------")
    
    try:
        match_result = await score_job_fit(candidate, resume_data, job)
        print("\n=== MATCH RESULT ===")
        print("Fit Score:", match_result.fit_score)
        print("ATS Score:", match_result.ats_score)
        print("Combined Score:", match_result.combined_score)
        print("Should Apply:", match_result.should_apply)
        print("Matching Skills:", match_result.matching_skills)
        print("Missing Skills:", match_result.missing_skills)
        print("Reasoning:", match_result.reasoning)
    except Exception as e:
        print(f"✗ Scoring failed: {e}")

if __name__ == "__main__":
    from datetime import datetime
    asyncio.run(main())
