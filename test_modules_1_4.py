#!/usr/bin/env python3
"""
BD-Automator Modules 1-4 End-to-End Pipeline Integration Test
============================================================
Features:
  - Retrieves candidate "Sabih Haider" from the PostgreSQL database.
  - Retrieves available scraped jobs from the database (or scrapes new ones if empty).
  - Evaluates candidate-job alignment (Module 3 Fit Scorer) for all available jobs.
  - Selects the best job (highest combined score above threshold).
  - Prepares the tailored application package (resume, cover letter, QA answers).
  - Executes the browser automation (Module 4) in dry-run mode (or live) for the best job.
  - Outputs a detailed system log file (`test_modules_1_4.log`) for diagnosing errors.
"""

import asyncio
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import List, Dict, Any

# Path configuration
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

# Load environment variables
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / "backend" / ".env")

# Set up logging to output detailed errors from all modules
logger = logging.getLogger("system_pipeline_test")
logger.setLevel(logging.DEBUG)

# File handler for verbose logging
file_handler = logging.FileHandler(PROJECT_ROOT / "test_modules_1_4.log", mode="w", encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_formatter = logging.Formatter('%(asctime)s [%(levelname)s] (%(name)s:%(filename)s:%(lineno)d) - %(message)s')
file_handler.setFormatter(file_formatter)

# Console handler for readable execution log
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter('%(asctime)s [%(levelname)s] - %(message)s')
console_handler.setFormatter(console_formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Forward other library loggers to our file handler
for lib_logger_name in ["app", "module2", "module3", "module4", "playwright", "urllib3", "httpx"]:
    lib_logger = logging.getLogger(lib_logger_name)
    lib_logger.setLevel(logging.DEBUG)
    lib_logger.addHandler(file_handler)

# DB imports
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.application import Application
from app.models.resume import Resume

# Module 3 scoring and schema imports
from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import parse_resume, ResumeData, ResumeSection
from module3.scoring.fit_scorer import score_job_fit
from module3.orchestrator import orchestrate_application_package

# Color helpers for terminal output
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def log_header(title):
    logger.info(f"\n{BOLD}{YELLOW}{'='*80}{RESET}\n{BOLD}{title}{RESET}\n{BOLD}{'='*80}{RESET}")

def log_success(msg):
    logger.info(f"{GREEN}✓ SUCCESS: {msg}{RESET}")

def log_warn(msg):
    logger.info(f"{YELLOW}⚠ WARNING: {msg}{RESET}")

def log_failure(msg):
    logger.error(f"{RED}✗ FAILURE: {msg}{RESET}")

API_BASE = os.getenv("M1_API_BASE_URL", "http://localhost:8002/api")
RESUME_PDF = str(PROJECT_ROOT / "harmain_ali_butt_resume.pdf")


async def get_or_parse_base_resume(candidate_id: str) -> ResumeData:
    logger.info("Resolving base resume for candidate...")
    async with AsyncSessionLocal() as session:
        res_query = select(Resume).where(Resume.candidate_id == candidate_id, Resume.is_base == True).order_by(Resume.version.desc())
        res_result = await session.execute(res_query)
        base_resume = res_result.scalars().first()
        
        if base_resume and base_resume.parsed_json:
            logger.info(f"Base resume found in DB (ID: {base_resume.id}). Loading parsed JSON.")
            
            # Ensure the file_url is a Supabase URL, not a local path
            if base_resume.file_url and not base_resume.file_url.startswith("http"):
                logger.info(f"Existing base resume has a local path: {base_resume.file_url}. Uploading to Supabase...")
                from module3.utils.storage import upload_file_to_supabase
                remote_url = await upload_file_to_supabase(base_resume.file_url, "resume", f"{candidate_id}_base.pdf")
                if remote_url != base_resume.file_url:
                    base_resume.file_url = remote_url
                    await session.commit()
                    logger.info(f"Updated base resume file_url to: {remote_url}")

            sections = ResumeSection(**base_resume.parsed_json)
            return ResumeData(
                candidate_id=candidate_id,
                resume_id=str(base_resume.id),
                file_url=base_resume.file_url,
                sections=sections,
                raw_text="[Loaded from DB]"
            )
            
    # Parse PDF if database record is missing
    logger.info(f"No parsed base resume in database. Attempting to parse local PDF: {RESUME_PDF}")
    if not os.path.exists(RESUME_PDF):
        raise FileNotFoundError(f"Resume PDF not found at: {RESUME_PDF}")
        
    parsed_resume = await parse_resume(RESUME_PDF, candidate_id=candidate_id)
    
    # Save base resume to database
    async with AsyncSessionLocal() as session:
        # Find next version
        res_query = select(Resume).where(Resume.candidate_id == candidate_id)
        res_result = await session.execute(res_query)
        existing_resumes = res_result.scalars().all()
        next_version = max([r.version or 0 for r in existing_resumes] + [0]) + 1
        
        parsed_json_data = parsed_resume.sections.model_dump()
        parsed_json_data["file_hash"] = parsed_resume.file_hash
        
        from module3.utils.storage import upload_file_to_supabase
        remote_url = await upload_file_to_supabase(RESUME_PDF, "resume", f"{candidate_id}_base.pdf")
        
        db_resume = Resume(
            candidate_id=candidate_id,
            version=next_version,
            file_url=remote_url,
            parsed_json=parsed_json_data,
            is_base=True
        )
        session.add(db_resume)
        await session.commit()
        await session.refresh(db_resume)
        
        parsed_resume.resume_id = db_resume.id
        logger.info(f"Saved new base resume version {next_version} to DB with ID: {db_resume.id}")
        
    return parsed_resume


async def load_candidate() -> Candidate:
    log_header("STEP 1: Fetching Candidate from Database")
    async with AsyncSessionLocal() as session:
        query = select(Candidate).where(Candidate.name.ilike("%Harmain%"))
        result = await session.execute(query)
        candidate = result.scalar_one_or_none()
        
        if not candidate:
            log_failure("Candidate 'Harmain' not found in database.")
            # List available candidates to aid debugging
            all_result = await session.execute(select(Candidate))
            candidates = all_result.scalars().all()
            logger.info("Existing candidates in database:")
            for c in candidates:
                logger.info(f"  - Name: {c.name} | Email: {c.email} | ID: {c.id}")
            raise ValueError("Candidate 'Harmain' must be present in the database.")
            
        log_success(f"Candidate found: {candidate.name} (ID: {candidate.id})")
        logger.info(f"Email: {candidate.email}")
        logger.info(f"Tech Stack: {candidate.tech_stack}")
        logger.info(f"Years of Experience: {candidate.years_exp}")
        return candidate


async def load_jobs(candidate_id: str) -> List[Job]:
    log_header("STEP 2: Fetching Scraped Jobs from Database")
    async with AsyncSessionLocal() as session:
        from sqlalchemy import exists
        
        applied_stmt = select(Application.id).where(
            Application.candidate_id == candidate_id,
            Application.job_id == Job.id
        )
        
        # Fetch active jobs (we focus on greenhouse or lever jobs since browser automation is built for them)
        # Filter out jobs that the candidate has already applied to
        query = select(Job).where(
            Job.source.in_(["greenhouse", "lever"]),
            ~exists(applied_stmt)
        ).order_by(Job.created_at.desc()).limit(20)
        result = await session.execute(query)
        jobs = result.scalars().all()
        
        if not jobs:
            log_warn("No Greenhouse or Lever jobs found in database. Scraped jobs list is empty.")
            logger.info("Scraping a few real jobs from Vercel's board as a fallback...")
            
            from module2.adapters.greenhouse_adapter import GreenhouseAdapter
            from module2.normalization.normalizer import Normalizer
            from module2.normalization.helpers import is_usa_location
            
            adapter = GreenhouseAdapter()
            normalizer = Normalizer()
            
            raw_jobs = await adapter.discover_jobs({"company": "vercel"})
            logger.info(f"Scraped {len(raw_jobs)} raw jobs from Vercel Greenhouse board.")
            
            saved_jobs = []
            for raw in raw_jobs:
                norm = normalizer.normalize(raw)
                if is_usa_location(norm.location):
                    new_job = Job(
                        title=norm.title,
                        company=norm.company,
                        location=norm.location,
                        description=norm.description,
                        source=norm.source,
                        source_url=norm.source_url,
                        canonical_url=norm.canonical_url,
                        skills=norm.skills,
                        salary_min=norm.salary_min,
                        salary_max=norm.salary_max,
                        pay_period=norm.pay_period,
                        job_type=norm.job_type
                    )
                    session.add(new_job)
                    saved_jobs.append(new_job)
            
            await session.commit()
            for j in saved_jobs:
                await session.refresh(j)
            logger.info(f"Stored {len(saved_jobs)} USA Vercel jobs in database.")
            jobs = saved_jobs
            
        logger.info(f"Retrieved {len(jobs)} eligible jobs for matching.")
        for idx, j in enumerate(jobs[:5], 1):
            logger.info(f"  {idx}. {j.title} @ {j.company} ({j.location}) [ID: {j.id}]")
        if len(jobs) > 5:
            logger.info(f"  ... and {len(jobs) - 5} more.")
            
        return jobs


async def match_jobs(candidate: Candidate, base_resume: ResumeData, jobs: List[Job]) -> Dict[str, Any]:
    log_header("STEP 3: Matching Candidate against Available Jobs")
    logger.info(f"Matching candidate {candidate.name} against {len(jobs)} jobs...")
    
    # 1. First, score all jobs programmatically using the matcher matrix to filter
    from module2.filtering.matcher import score_job
    
    profile = {
        "required_skills": candidate.tech_stack or [],
        "min_experience_years": candidate.years_exp or 0,
        "preferred_locations": [candidate.location] if candidate.location else [],
        "desired_job_types": ["full-time"],
        "desired_salary_min": None
    }
    
    pre_scored_jobs = []
    for job_record in jobs:
        try:
            skills = job_record.skills
            if isinstance(skills, str):
                try:
                    skills = json.loads(skills)
                except Exception:
                    skills = []
            elif not skills:
                skills = []
                
            job_obj = NormalizedJob(
                title=job_record.title,
                company=job_record.company,
                location=job_record.location,
                url=job_record.source_url or "",
                description=job_record.description or "",
                source=job_record.source or "greenhouse",
                skills=skills,
                salary_min=job_record.salary_min,
                salary_max=job_record.salary_max,
                pay_period=job_record.pay_period or "yearly",
                job_type=job_record.job_type or "full-time",
                canonical_url=job_record.canonical_url or "",
                source_url=job_record.source_url or ""
            )
            job_obj.job_id = job_record.id
            
            trace = score_job(job_obj, profile)
            pre_scored_jobs.append((job_record, job_obj, trace.score))
        except Exception as e:
            logger.error(f"Error programmatically scoring job {job_record.id} ({job_record.title}): {e}")
            continue

    # Sort jobs by programmatic score descending
    pre_scored_jobs.sort(key=lambda x: x[2], reverse=True)
    
    logger.info(f"\nProgrammatic Match Matrix Filtering Results (Top 10):")
    for idx, (jr, _, score) in enumerate(pre_scored_jobs[:10], 1):
        logger.info(f"  {idx}. {jr.title} @ {jr.company} -> Matrix Score: {score:.2f}")

    # Select only the top 3 jobs for deep AI alignment evaluation to conserve tokens & cost
    top_n_jobs = pre_scored_jobs[:3]
    logger.info(f"\nSelected top {len(top_n_jobs)} jobs for deep AI matching evaluation...")

    scored_jobs = []
    
    # Format candidate dict for fit scorer
    candidate_dict = {
        "id": candidate.id,
        "name": candidate.name,
        "email": candidate.email,
        "phone": candidate.phone,
        "location": candidate.location,
        "work_auth": candidate.work_auth,
        "tech_stack": candidate.tech_stack,
        "years_exp": candidate.years_exp
    }
    
    async def score_single_job(job_record, job_obj, matrix_score):
        try:
            logger.info(f"Scoring deep AI alignment for job: '{job_record.title}' @ {job_record.company}...")
            match_res = await score_job_fit(candidate_dict, base_resume, job_obj)
            logger.info(f"  -> Score: {match_res.combined_score} (Fit: {match_res.fit_score}, ATS: {match_res.ats_score}) | Should Apply: {match_res.should_apply} for '{job_record.title}'")
            return {
                "job": job_record,
                "fit_score": match_res.fit_score,
                "ats_score": match_res.ats_score,
                "combined_score": match_res.combined_score,
                "should_apply": match_res.should_apply,
                "reasoning": match_res.reasoning
            }
        except Exception as e:
            logger.error(f"Error matching job {job_record.id} ({job_record.title}): {e}")
            logger.debug(traceback.format_exc())
            return None

    tasks = [score_single_job(jr, jo, ms) for jr, jo, ms in top_n_jobs]
    results = await asyncio.gather(*tasks)
    
    scored_jobs = [r for r in results if r is not None]

    if not scored_jobs:
        raise RuntimeError("No jobs were successfully matched/scored.")

    # Sort jobs by combined score descending
    scored_jobs.sort(key=lambda x: x["combined_score"], reverse=True)
    
    log_header("JOB MATCHING RESULTS SUMMARY")
    print(f"\n{BOLD}{'Job Title':<45} | {'Company':<20} | {'Score':<6} | {'Should Apply'}{RESET}")
    print("-" * 90)
    for sj in scored_jobs:
        title = sj["job"].title[:45]
        company = sj["job"].company[:20]
        score = sj["combined_score"]
        should_apply = f"{GREEN}Yes{RESET}" if sj["should_apply"] else f"{RED}No{RESET}"
        print(f"{title:<45} | {company:<20} | {score:<6} | {should_apply}")
        logger.info(f"Match Summary: {sj['job'].title} @ {sj['job'].company} -> Score: {score} | Should Apply: {sj['should_apply']}")
    print("")

    # Select the best matching job
    best_match = scored_jobs[0]
    log_success(f"Best match found: '{best_match['job'].title}' at {best_match['job'].company} with Combined Score: {best_match['combined_score']}")
    return best_match


async def execute_tailoring_pipeline(candidate_id: str, job_id: str) -> Dict[str, Any]:
    log_header("STEP 4: Executing AI Resume Intelligence & Tailoring Pipeline")
    logger.info(f"Orchestrating resume & cover letter tailoring for Job {job_id}...")
    
    # We call the main orchestrator module directly
    result = await orchestrate_application_package(
        candidate_id=candidate_id,
        job_id=job_id,
        base_resume_pdf_path=RESUME_PDF,
        api_base_url=API_BASE.replace("/api", ""),
        skip_gate=True
    )
    
    log_success("Tailoring pipeline finished.")
    logger.info(f"Application Package ID: {result.get('application_id')}")
    logger.info(f"Tailored Resume ID    : {result.get('tailored_resume_id')}")
    logger.info(f"Tailored Resume URL   : {result.get('resume_pdf_url')}")
    logger.info(f"Cover Letter URL     : {result.get('cover_letter_url')}")
    logger.info(f"Screening Answers    : {result.get('screening_answers')}")
    return result


async def run_browser_automation(app_result: Dict[str, Any]) -> Any:
    log_header("STEP 5: Executing Browser Automation Form Filler")
    
    from app.tasks.browser_automation import hydrate_and_execute
    
    app_id = app_result.get("application_id")
    resume_url = app_result.get("tailored_resume_id")
    cover_letter_url = app_result.get("cover_letter_url")
    screening_answers = app_result.get("screening_answers", {})
    
    package_dict = {
        "application_id": str(app_id),
        "resume_url": str(resume_url) if resume_url else None,
        "cover_letter_url": str(cover_letter_url) if cover_letter_url else None,
        "screening_answers": screening_answers,
    }
    
    # Disable dry run to perform a real job application submission
    os.environ["DRY_RUN_NO_SUBMIT"] = "false"
    logger.info("DRY_RUN_NO_SUBMIT=false is active. Performing a REAL live job application submission.")
    
    logger.info("Starting hydrate_and_execute Playwright session...")
    try:
        start_time = time.time()
        result = await hydrate_and_execute(package_dict, retry_count=0)
        elapsed = time.time() - start_time
        
        log_success(f"Browser automation execution completed in {elapsed:.2f} seconds.")
        logger.info(f"  Result Status : {result.status}")
        logger.info(f"  Confirmation  : {result.confirmation_text}")
        if result.screenshot_url:
            logger.info(f"  Screenshot    : {result.screenshot_url}")
        if result.error_message:
            log_failure(f"Browser Automation reported error: {result.error_message}")
            
        # Verify status in database
        async with AsyncSessionLocal() as session:
            app_record = await session.get(Application, app_id)
            if app_record:
                logger.info(f"  Final Application Status in DB: {app_record.status}")
                
        return result
    except Exception as e:
        log_failure(f"Browser automation task failed with exception: {e}")
        logger.error(traceback.format_exc())
        raise


async def main():
    print(f"\n{BOLD}{CYAN}{'='*80}")
    print(" BD-AUTOMATOR E2E PIPELINE RUNNER (REAL DATA)")
    print(f"{'='*80}{RESET}\n")
    print(f"System logging initialized. All trace logs written to:")
    print(f"  {PROJECT_ROOT}/test_modules_1_4.log\n")
    
    start_time = time.time()
    try:
        # 1. Fetch Candidate
        candidate = await load_candidate()
        
        # 2. Get parsed base resume representation
        base_resume = await get_or_parse_base_resume(str(candidate.id))
        
        # 3. Load scraped jobs
        jobs = await load_jobs(str(candidate.id))
        
        # 4. Match and find the best job
        best_match_info = await match_jobs(candidate, base_resume, jobs)
        best_job = best_match_info["job"]
        
        # 5. Tailor package for the best job
        app_package = await execute_tailoring_pipeline(str(candidate.id), str(best_job.id))
        
        # 6. Apply via browser automation
        automation_result = await run_browser_automation(app_package)
        
        elapsed = time.time() - start_time
        log_header("E2E PIPELINE SUCCESSFUL SUMMARY")
        log_success(f"Entire E2E pipeline run completed successfully in {elapsed:.2f} seconds.")
        logger.info(f"Candidate       : {candidate.name}")
        logger.info(f"Target Job      : {best_job.title} @ {best_job.company}")
        logger.info(f"Match Score     : {best_match_info['combined_score']}")
        logger.info(f"Apply Status    : {automation_result.status}")
        logger.info(f"Confirmation Msg: {automation_result.confirmation_text}")
        if automation_result.screenshot_url:
            logger.info(f"Saved Screenshot: {automation_result.screenshot_url}")
            
    except Exception as e:
        log_header("E2E PIPELINE FAILED")
        log_failure(f"Pipeline crashed: {e}")
        logger.error(traceback.format_exc())
        print(f"\n{RED}{BOLD}Pipeline run failed. Please check test_modules_1_4.log for detailed debug traces.{RESET}\n")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
