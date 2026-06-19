import asyncio
import os
import sys
import httpx
import json
from pathlib import Path
from datetime import datetime

# Setup paths
HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "backend"))

# Load dotenv to get GEMINI_API_KEY
from dotenv import load_dotenv
load_dotenv(ROOT_DIR / "backend" / ".env")

from module3.orchestrator import orchestrate_application_package

async def setup_candidate_and_job() -> tuple[str, str]:
    """Ensure a matching candidate and job exist in the database and return their UUIDs."""
    api_base = "http://127.0.0.1:8000"
    
    async with httpx.AsyncClient(base_url=api_base, timeout=60.0) as client:
        # 1. Create a candidate
        candidate_payload = {
            "name": "Sabih Haider Test",
            "email": f"sabih.test.{int(datetime.now().timestamp())}@example.com",
            "phone": "+923000000000",
            "location": "US",
            "work_auth": "us_authorized",
            "tech_stack": ["Python", "JavaScript", "TypeScript", "React", "Next.js", "PostgreSQL", "MySQL", "Git"],
            "years_exp": 2,
            "linkedin_url": "https://linkedin.com/in/sabihhaider"
        }
        resp = await client.post("/api/candidates", json=candidate_payload)
        assert resp.status_code == 201, f"Failed candidate setup: {resp.text}"
        candidate = resp.json()
        candidate_id = candidate["id"]
        print(f"[SETUP] Created candidate '{candidate_payload['name']}' with ID: {candidate_id}")
        
        # 2. Create a matching job (Web/Full-Stack Software Engineer)
        job_payload = [{
            "title": "Software Engineer (Python/React)",
            "company": "Stripe US",
            "location": "Remote, US",
            "source": "greenhouse",
            "source_url": f"https://stripe.com/jobs/test-engineer-{int(datetime.now().timestamp())}",
            "canonical_url": f"https://stripe.com/jobs/test-engineer-{int(datetime.now().timestamp())}",
            "description": "We are seeking a Software Engineer with experience in Python backend development, building REST APIs, React frontend interfaces, Next.js, and relational databases like PostgreSQL. Familiarity with GitHub Actions or other CI/CD tools is a plus.",
            "skills": ["python", "react", "javascript", "typescript", "postgresql", "next.js"],
            "salary_min": 140000,
            "salary_max": 180000,
            "pay_period": "yearly",
            "job_type": "full-time"
        }]
        resp = await client.post("/api/jobs", json=job_payload)
        assert resp.status_code == 201, f"Failed job setup: {resp.text}"
        job = resp.json()[0]
        job_id = job["id"]
        print(f"[SETUP] Created matching job '{job_payload[0]['title']}' with ID: {job_id}")
        
        return candidate_id, job_id

async def test_run():
    candidate_id, job_id = await setup_candidate_and_job()
    
    resume_path = ROOT_DIR / "Sabih Haider — Software Engineer _ Full-Stack Web Developer.pdf"
    screening_qs = [
        "How many years of professional software engineering experience do you have?",
        "Do you require sponsorship to work in the United States?",
        "What are your salary expectations for this position?"
    ]
    
    try:
        result = await orchestrate_application_package(
            candidate_id=candidate_id,
            job_id=job_id,
            base_resume_pdf_path=str(resume_path),
            screening_questions=screening_qs
        )
        print("\n==================================================")
        print("ORCHESTRATION PIPELINE RUN COMPLETED")
        print("==================================================")
        print("Final Status:", result["status"])
        print("Application ID:", result["application_id"])
        print("Fit Score:", result["match_result"]["fit_score"])
        print("ATS Score:", result["match_result"]["ats_score"])
        print("Combined Score:", result["match_result"]["combined_score"])
        print("Tailored Resume PDF:", result["tailored_resume_id"], "->", result.get("tailored_resume_id") is not None)
        print("Cover Letter PDF Path:", result["cover_letter_url"])
        print("\nScreening Answers:")
        for q, a in result["screening_answers"].items():
            print(f"  Q: {q}")
            print(f"  A: {a}")
            
    except Exception as e:
        print(f"\n✗ Orchestrator pipeline failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_run())
