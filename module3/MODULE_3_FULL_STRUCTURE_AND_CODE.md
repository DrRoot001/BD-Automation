# Module 3 Full Structure and Source Code
OWNER_NAME = "HARMAIN butt"
This document captures the current folder structure and source code for Module 3 of the BD Automator Agent project.

Date: 2026-06-25

## Folder Structure

```text
module3/
├── cover_letter/
│   └── generator.py
├── parser/
│   ├── resume_parser.py
│   └── test_resume.pdf
├── qa/
│   └── question_answerer.py
├── scoring/
│   ├── ats_scorer.py
│   └── fit_scorer.py
├── tailoring/
│   ├── pdf_generator.py
│   └── resume_tailor.py
├── templates/
│   ├── cover_letter_template.html
│   └── resume_template.html
├── utils/
│   ├── gemini.py
│   ├── storage.py
│   └── test_storage.py
├── orchestrator.py
```

> Note: The PDF fixture in the parser folder is a binary artifact and is not included as source code here.

---

## File: module3/orchestrator.py

```python
"""Orchestrator pipeline for Module 3 (AI & Resume Intelligence)."""
from __future__ import annotations

import os
import json
import asyncio
import httpx
from typing import List, Dict, Optional
from uuid import UUID

from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import parse_resume, ResumeData, ResumeSection
from module3.scoring.fit_scorer import score_job_fit, MatchResult
from module3.tailoring.resume_tailor import tailor_resume, TailoredResume
from module3.cover_letter.generator import generate_cover_letter, CoverLetter
from module3.qa.question_answerer import answer_screening_questions
from module3.utils.storage import upload_file_to_supabase, safe_filename

# Event publishing
try:
    from app.services.events import publish_event
    HAS_EVENTS = True
except ImportError:
    HAS_EVENTS = False

async def orchestrate_application_package(
    candidate_id: str,
    job_id: str,
    base_resume_pdf_path: Optional[str] = None,
    screening_questions: Optional[List[str]] = None,
    api_base_url: str = "http://127.0.0.1:8000"
) -> Dict[str, any]:
    """
    Orchestrate candidate application flow:
    Score -> Validate Gate -> Tailor Resume -> Generate Cover Letter -> QA -> Queue.
    """
    print(f"\n[ORCHESTRATOR] Starting application package preparation for Candidate: {candidate_id} | Job: {job_id}")
    
    async with httpx.AsyncClient(base_url=api_base_url, timeout=120.0) as client:
        # 1. Fetch Candidate details
        print("[ORCHESTRATOR] Fetching candidate details...")
        resp = await client.get(f"/api/candidates/{candidate_id}")
        if resp.status_code != 200:
            raise ValueError(f"Candidate not found in DB: {resp.text}")
        candidate = resp.json()
        
        # 2. Fetch Base Resume
        print("[ORCHESTRATOR] Fetching base resume...")
        resp = await client.get(f"/api/resumes/{candidate_id}?is_base=true")
        
        resume_data = None
        base_resume_id = None
        
        if resp.status_code == 200 and resp.json():
            resumes = resp.json()
            # Find the base resume
            base_resume = resumes[0]
            base_resume_id = base_resume["id"]
            parsed_json = base_resume.get("parsed_json")
            if parsed_json:
                sections = ResumeSection(**parsed_json)
                resume_data = ResumeData(
                    candidate_id=candidate_id,
                    resume_id=base_resume_id,
                    file_url=base_resume.get("file_url", ""),
                    sections=sections,
                    raw_text="[Loaded from DB]"
                )
                print(f"[ORCHESTRATOR] Loaded existing base resume from DB (ID: {base_resume_id})")
        
        # If no base resume exists, parse the local PDF and insert it as the base resume!
        if not resume_data:
            if not base_resume_pdf_path or not os.path.exists(base_resume_pdf_path):
                raise ValueError("No base resume found in DB, and local base_resume_pdf_path was missing or invalid.")
                
            print(f"[ORCHESTRATOR] Base resume not found. Parsing local PDF: {base_resume_pdf_path}...")
            parsed_resume = await parse_resume(base_resume_pdf_path, candidate_id=candidate_id)
            
            # Save base resume to central database
            print("[ORCHESTRATOR] Uploading parsed base resume to central database...")
            upload_payload = {
                "candidate_id": candidate_id,
                "version": 1,
                "file_url": base_resume_pdf_path,
                "parsed_json": parsed_resume.sections.model_dump(),
                "is_base": True
            }
            resp = await client.post("/api/resumes", json=upload_payload)
            if resp.status_code != 201:
                raise ValueError(f"Failed to create base resume record: {resp.text}")
                
            uploaded_resume = resp.json()
            base_resume_id = uploaded_resume["id"]
            parsed_resume.resume_id = base_resume_id
            resume_data = parsed_resume
            print(f"[ORCHESTRATOR] Created base resume record in database (ID: {base_resume_id})")

        # 3. Fetch Job details
        print("[ORCHESTRATOR] Fetching job details...")
        resp = await client.get(f"/api/jobs/{job_id}")
        if resp.status_code != 200:
            raise ValueError(f"Job not found in DB: {resp.text}")
        job_data = resp.json()
        
        skills = job_data.get("skills")
        if isinstance(skills, str):
            skills = json.loads(skills)
        elif not skills:
            skills = []
            
        job = NormalizedJob(
            title=job_data.get("title", ""),
            company=job_data.get("company", ""),
            location=job_data.get("location", "Remote"),
            url=job_data.get("source_url", ""),
            description=job_data.get("description", ""),
            source=job_data.get("source", "greenhouse"),
            skills=skills,
            salary_min=job_data.get("salary_min"),
            salary_max=job_data.get("salary_max"),
            pay_period=job_data.get("pay_period", "yearly"),
            job_type=job_data.get("job_type", "full-time"),
            canonical_url=job_data.get("canonical_url", ""),
            embedding=job_data.get("embedding"),
            source_url=job_data.get("source_url", "")
        )
        job.job_id = job_id

        # 4. Create Application record in FOUND status
        print("[ORCHESTRATOR] Creating application record in 'FOUND' status...")
        app_payload = {
            "candidate_id": candidate_id,
            "job_id": job_id,
            "status": "FOUND",
            "resume_id": base_resume_id
        }
        resp = await client.post("/api/applications", json=app_payload)
        if resp.status_code != 201:
            raise ValueError(f"Failed to create application record: {resp.text}")
        application = resp.json()
        app_id = application["id"]
        print(f"[ORCHESTRATOR] Created application ID: {app_id}")

        # 5. Run Fit Score & ATS match evaluation
        print("[ORCHESTRATOR] Evaluating candidate-job alignment & ATS compatibility...")
        match_result = await score_job_fit(candidate, resume_data, job)
        print(f"[ORCHESTRATOR] Scores calculated - Fit: {match_result.fit_score} | ATS: {match_result.ats_score} | Combined: {match_result.combined_score}")
        
        # Check Gate Threshold
        if not match_result.should_apply:
            print(f"[ORCHESTRATOR] combined_score ({match_result.combined_score}) is below gate threshold of 70. Transitioning status to ANALYZED and STOPPING.")
            
            # Transition to ANALYZED
            update_payload = {
                "status": "ANALYZED",
                "fit_score": match_result.fit_score,
                "ats_score": match_result.ats_score,
                "combined_score": match_result.combined_score,
                "metadata": {"reason": "Combined score below threshold gate", "explanation": match_result.reasoning}
            }
            await client.patch(f"/api/applications/{app_id}/status", json=update_payload)
            
            return {
                "status": "ANALYZED",
                "application_id": app_id,
                "match_result": match_result.model_dump(mode="json"),
                "tailored_resume_id": None,
                "cover_letter_url": None,
                "screening_answers": {}
            }

        # Transition to ANALYZED since combined_score >= 70
        print("[ORCHESTRATOR] Combined score matches threshold. Transitioning status to 'ANALYZED'...")
        update_payload = {
            "status": "ANALYZED",
            "fit_score": match_result.fit_score,
            "ats_score": match_result.ats_score,
            "combined_score": match_result.combined_score,
            "metadata": {"explanation": match_result.reasoning}
        }
        await client.patch(f"/api/applications/{app_id}/status", json=update_payload)

        # Transition to MATCHED
        print("[ORCHESTRATOR] Transitioning status to 'MATCHED'...")
        update_payload = {
            "status": "MATCHED",
            "fit_score": match_result.fit_score,
            "ats_score": match_result.ats_score,
            "combined_score": match_result.combined_score,
            "metadata": {"explanation": match_result.reasoning}
        }
        await client.patch(f"/api/applications/{app_id}/status", json=update_payload)

        # Query existing resumes for this candidate to calculate next version number
        all_resumes_resp = await client.get(f"/api/resumes/{candidate_id}")
        next_version = 2
        if all_resumes_resp.status_code == 200:
            existing_resumes = all_resumes_resp.json()
            if existing_resumes:
                versions = [r.get("version", 0) for r in existing_resumes if r.get("version") is not None]
                if versions:
                    next_version = max(versions) + 1
        print(f"[ORCHESTRATOR] Calculated next tailored resume version: {next_version}")

        # 6, 7, 8. Run Tailoring, Cover Letter, and Screening Answers concurrently
        print("[ORCHESTRATOR] Running Tailoring, Cover Letter, and Screening Questions in parallel...")
        
        tasks = [
            tailor_resume(resume_data, job, candidate, version=next_version),
            generate_cover_letter(resume_data, job, candidate)
        ]
        
        if screening_questions:
            tasks.append(answer_screening_questions(screening_questions, resume_data, job, candidate))
            
        results = await asyncio.gather(*tasks)
        
        tailored_resume = results[0]
        cover_letter = results[1]
        screening_answers = results[2] if screening_questions else {}
        
        print(f"[ORCHESTRATOR] Resume tailored. ATS Score improved from {tailored_resume.ats_score_before} to {tailored_resume.ats_score_after}")
        
        # Upload Tailored Resume to the CORRECT Bucket
        candidate_name = candidate.get("name") or candidate_id
        remote_resume_url = await upload_file_to_supabase(
            tailored_resume.pdf_url,
            "updated_resume",
            f"{safe_filename(candidate_name, default=candidate_id, extension='')}_resume_v{next_version}.pdf"
        )
        resume_pdf_url = remote_resume_url
        tailored_resume.pdf_url = remote_resume_url
        
        # Upload Tailored Resume version to central DB
        print("[ORCHESTRATOR] Uploading tailored resume version to database...")
        tailored_payload = {
            "candidate_id": candidate_id,
            "version": tailored_resume.version,
            "file_url": tailored_resume.pdf_url,
            "parsed_json": {
                "summary": tailored_resume.modified_summary,
                "skills": tailored_resume.modified_skills,
                "keywords": tailored_resume.modified_keywords,
                "experience": [exp.model_dump() for exp in tailored_resume.experience],
                "education": [edu.model_dump() for edu in tailored_resume.education],
                "certifications": resume_data.sections.certifications
            },
            "is_base": False,
            "tailored_for_job_id": job_id
        }
        resp = await client.post("/api/resumes", json=tailored_payload)
        if resp.status_code != 201:
            raise ValueError(f"Failed to save tailored resume: {resp.text}")
        tailored_db_resume = resp.json()
        tailored_resume_id = tailored_db_resume["id"]
        
        # Update status to RESUME_UPDATED
        print("[ORCHESTRATOR] Transitioning application status to 'RESUME_UPDATED'...")
        update_payload = {
            "status": "RESUME_UPDATED",
            "resume_id": tailored_resume_id,
            "metadata": {
                "ats_score_before": tailored_resume.ats_score_before,
                "ats_score_after": tailored_resume.ats_score_after
            }
        }
        await client.patch(f"/api/applications/{app_id}/status", json=update_payload)
        
        cover_letter_url = cover_letter.pdf_url
        print(f"[ORCHESTRATOR] Cover Letter compiled to: {cover_letter_url}")
        
        # Upload Cover letter to the CORRECT Bucket
        remote_cl_url = await upload_file_to_supabase(
            cover_letter_url, 
            "cover_letter", # CHANGED from "cover_letter"
            f"{candidate_id}_{job_id}_cl.pdf"
        )
        cover_letter_url = remote_cl_url
        
        # Update status to COVER_LETTER_CREATED
        print("[ORCHESTRATOR] Transitioning application status to 'COVER_LETTER_CREATED'...")
        update_payload = {
            "status": "COVER_LETTER_CREATED",
            "cover_letter_url": cover_letter_url
        }
        await client.patch(f"/api/applications/{app_id}/status", json=update_payload)

        if screening_questions:
            print("[ORCHESTRATOR] Screening answers drafted successfully.")

        # Update status to QUEUED (Ready for submission)
        print("[ORCHESTRATOR] Application package complete. Transitioning application status to 'QUEUED'...")
        update_payload = {
            "status": "QUEUED",
            "metadata": {
                "screening_answers": screening_answers
            }
        }
        await client.patch(f"/api/applications/{app_id}/status", json=update_payload)

        # 10. Publish event to Redis event bus
        if HAS_EVENTS:
            print("[ORCHESTRATOR] Publishing 'application.package_ready' event to Redis...")
            event_payload = {
                "application_id": str(app_id),
                "resume_url": tailored_resume.pdf_url,
                "cover_letter_url": cover_letter_url,
                "screening_answers": screening_answers
            }
            await publish_event("application.package_ready", event_payload)
            print("[ORCHESTRATOR] Redis event published successfully.")
            
        print(f"[ORCHESTRATOR] ✓ APPLICATION PACKAGE PREPARATION COMPLETE (App ID: {app_id})")
        
        return {
            "status": "QUEUED",
            "application_id": app_id,
            "match_result": match_result.model_dump(mode="json"),
            "tailored_resume_id": tailored_resume_id,
            "cover_letter_url": cover_letter_url,
            "screening_answers": screening_answers
        }


async def prepare_package_for_live_application(
    candidate_id: str,
    job_id: str,
    needs_cover_letter: bool,
    screening_questions: List[str],
    api_base_url: str = "http://127.0.0.1:8000"
) -> Dict[str, any]:
    """
    Prepare tailored resume, cover letter (if needed), and answer screening questions for live application.
    Runs synchronously and only executes required pipeline steps.
    """
    print(f"\n[ORCHESTRATOR] Synchronous package preparation for Candidate: {candidate_id} | Job: {job_id}")
    
    async with httpx.AsyncClient(base_url=api_base_url, timeout=120.0) as client:
        # 1. Fetch Candidate details
        print("[ORCHESTRATOR] Fetching candidate details...")
        resp = await client.get(f"/api/candidates/{candidate_id}")
        if resp.status_code != 200:
            raise ValueError(f"Candidate not found in DB: {resp.text}")
        candidate = resp.json()
        
        # 2. Fetch Base Resume
        print("[ORCHESTRATOR] Fetching base resume...")
        resp = await client.get(f"/api/resumes/{candidate_id}?is_base=true")
        
        resume_data = None
        base_resume_id = None
        
        if resp.status_code == 200 and resp.json():
            resumes = resp.json()
            base_resume = resumes[0]
            base_resume_id = base_resume["id"]
            parsed_json = base_resume.get("parsed_json")
            if parsed_json:
                sections = ResumeSection(**parsed_json)
                resume_data = ResumeData(
                    candidate_id=candidate_id,
                    resume_id=base_resume_id,
                    file_url=base_resume.get("file_url", ""),
                    sections=sections,
                    raw_text="[Loaded from DB]"
                )
                print(f"[ORCHESTRATOR] Loaded existing base resume from DB (ID: {base_resume_id})")
            else:
                # Base resume record exists but parsed_json is NULL — auto-parse from file_url
                file_url = base_resume.get("file_url", "")
                if file_url and os.path.exists(file_url):
                    print(f"[ORCHESTRATOR] Base resume has no parsed_json. Parsing from file: {file_url}")
                    parsed_resume = await parse_resume(file_url, candidate_id=candidate_id, resume_id=base_resume_id)
                    # Persist parsed_json back to DB via resume update
                    update_payload = {"parsed_json": parsed_resume.sections.model_dump()}
                    patch_resp = await client.patch(f"/api/resumes/{base_resume_id}", json=update_payload)
                    if patch_resp.status_code not in (200, 204):
                        print(f"[ORCHESTRATOR] Warning: could not persist parsed_json ({patch_resp.status_code}): {patch_resp.text}")
                    resume_data = parsed_resume
                    resume_data.resume_id = base_resume_id
                    print(f"[ORCHESTRATOR] Auto-parsed base resume and updated DB (ID: {base_resume_id})")

        if not resume_data:
            raise ValueError("No base resume found in DB for this candidate.")

        # 3. Fetch Job details
        print("[ORCHESTRATOR] Fetching job details...")
        resp = await client.get(f"/api/jobs/{job_id}")
        if resp.status_code != 200:
            raise ValueError(f"Job not found in DB: {resp.text}")
        job_data = resp.json()
        
        skills = job_data.get("skills")
        if isinstance(skills, str):
            skills = json.loads(skills)
        elif not skills:
            skills = []
            
        job = NormalizedJob(
            title=job_data.get("title", ""),
            company=job_data.get("company", ""),
            location=job_data.get("location", "Remote"),
            url=job_data.get("source_url", ""),
            description=job_data.get("description", ""),
            source=job_data.get("source", "greenhouse"),
            skills=skills,
            salary_min=job_data.get("salary_min"),
            salary_max=job_data.get("salary_max"),
            pay_period=job_data.get("pay_period", "yearly"),
            job_type=job_data.get("job_type", "full-time"),
            canonical_url=job_data.get("canonical_url", ""),
            embedding=job_data.get("embedding"),
            source_url=job_data.get("source_url", "")
        )
        job.job_id = job_id

        # 4. Locate or Create Application record
        app_id = None
        resp = await client.get("/api/applications")
        if resp.status_code == 200:
            apps = resp.json()
            for app in apps:
                if app["candidate_id"] == candidate_id and app["job_id"] == job_id:
                    app_id = app["id"]
                    break
        
        if not app_id:
            print("[ORCHESTRATOR] Application record not found. Creating in 'FOUND' status...")
            app_payload = {
                "candidate_id": candidate_id,
                "job_id": job_id,
                "status": "FOUND",
                "resume_id": base_resume_id
            }
            resp = await client.post("/api/applications", json=app_payload)
            if resp.status_code != 201:
                raise ValueError(f"Failed to create application record: {resp.text}")
            application = resp.json()
            app_id = application["id"]
            print(f"[ORCHESTRATOR] Created application ID: {app_id}")
        else:
            print(f"[ORCHESTRATOR] Found existing application ID: {app_id}")

        # 5. Run Fit Score & ATS match evaluation
        print("[ORCHESTRATOR] Evaluating candidate-job alignment & ATS compatibility...")
        match_result = await score_job_fit(candidate, resume_data, job)
        print(f"[ORCHESTRATOR] Scores calculated - Fit: {match_result.fit_score} | ATS: {match_result.ats_score} | Combined: {match_result.combined_score}")
        
        # Check Gate Threshold
        if not match_result.should_apply:
            print(f"[ORCHESTRATOR] combined_score ({match_result.combined_score}) is below gate threshold of 70. Transitioning status to ANALYZED and STOPPING.")
            
            # Transition to ANALYZED
            update_payload = {
                "status": "ANALYZED",
                "fit_score": match_result.fit_score,
                "ats_score": match_result.ats_score,
                "combined_score": match_result.combined_score,
                "metadata": {"reason": "Combined score below threshold gate", "explanation": match_result.reasoning}
            }
            await client.patch(f"/api/applications/{app_id}/status", json=update_payload)
            
            return {
                "should_apply": False,
                "reason": f"Combined score ({match_result.combined_score}) is below gate threshold of 70: {match_result.reasoning}"
            }

        # Transition to ANALYZED since combined_score >= 70
        print("[ORCHESTRATOR] Combined score matches threshold. Transitioning status to 'ANALYZED'...")
        update_payload = {
            "status": "ANALYZED",
            "fit_score": match_result.fit_score,
            "ats_score": match_result.ats_score,
            "combined_score": match_result.combined_score,
            "metadata": {"explanation": match_result.reasoning}
        }
        await client.patch(f"/api/applications/{app_id}/status", json=update_payload)

        # Transition to MATCHED
        print("[ORCHESTRATOR] Transitioning status to 'MATCHED'...")
        update_payload = {
            "status": "MATCHED",
            "fit_score": match_result.fit_score,
            "ats_score": match_result.ats_score,
            "combined_score": match_result.combined_score,
            "metadata": {"explanation": match_result.reasoning}
        }
        await client.patch(f"/api/applications/{app_id}/status", json=update_payload)

        # 6. Check if tailored resume already exists for this job
        tailored_resume_id = None
        resume_pdf_url = None
        
        all_resumes_resp = await client.get(f"/api/resumes/{candidate_id}")
        if all_resumes_resp.status_code == 200:
            existing_resumes = all_resumes_resp.json()
            for r in existing_resumes:
                if r.get("tailored_for_job_id") == job_id:
                    tailored_resume_id = r["id"]
                    resume_pdf_url = r["file_url"]
                    print(f"[ORCHESTRATOR] Found existing tailored resume for job {job_id} (ID: {tailored_resume_id})")
                    break

        if not resume_pdf_url:
            # Calculate next version
            next_version = 2
            if all_resumes_resp.status_code == 200:
                existing_resumes = all_resumes_resp.json()
                if existing_resumes:
                    versions = [r.get("version", 0) for r in existing_resumes if r.get("version") is not None]
                    if versions:
                        next_version = max(versions) + 1
            print(f"[ORCHESTRATOR] Tailoring new resume version: {next_version}")
            
            # Tailor the resume
            tailored_resume = await tailor_resume(resume_data, job, candidate, version=next_version)
            
            # Upload Tailored Resume to the CORRECT Bucket
            remote_resume_url = await upload_file_to_supabase(
                tailored_resume.pdf_url, 
                "updated_resume",  # CHANGED from "resume"
                f"{candidate_id}_{job_id}_v{next_version}.pdf"
            )
            resume_pdf_url = remote_resume_url
            tailored_resume.pdf_url = remote_resume_url
            
            # Upload Tailored Resume to DB
            tailored_payload = {
                "candidate_id": candidate_id,
                "version": tailored_resume.version,
                "file_url": tailored_resume.pdf_url,
                "parsed_json": {
                    "summary": tailored_resume.modified_summary,
                    "skills": tailored_resume.modified_skills,
                    "keywords": tailored_resume.modified_keywords,
                    "experience": [exp.model_dump() for exp in tailored_resume.experience],
                    "education": [edu.model_dump() for edu in tailored_resume.education],
                    "certifications": resume_data.sections.certifications
                },
                "is_base": False,
                "tailored_for_job_id": job_id
            }
            resp = await client.post("/api/resumes", json=tailored_payload)
            if resp.status_code != 201:
                raise ValueError(f"Failed to save tailored resume: {resp.text}")
            tailored_db_resume = resp.json()
            tailored_resume_id = tailored_db_resume["id"]
            
            # Update application status to RESUME_UPDATED
            update_payload = {
                "status": "RESUME_UPDATED",
                "resume_id": tailored_resume_id,
                "metadata": {
                    "ats_score_before": tailored_resume.ats_score_before,
                    "ats_score_after": tailored_resume.ats_score_after
                }
            }
            await client.patch(f"/api/applications/{app_id}/status", json=update_payload)

        # 7. Generate Cover Letter only if needs_cover_letter is True
        cover_letter_url = None
        if needs_cover_letter:
            print("[ORCHESTRATOR] Generating cover letter...")
            cover_letter = await generate_cover_letter(resume_data, job, candidate)
            cover_letter_url = cover_letter.pdf_url
            
            # Upload Cover letter to the CORRECT Bucket
            candidate_name = candidate.get("name") or candidate_id
            remote_cl_url = await upload_file_to_supabase(
                cover_letter_url,
                "cover_letter",
                f"{safe_filename(candidate_name, default=candidate_id, extension='')}_cover_letter.pdf"
            )
            cover_letter_url = remote_cl_url
            
            # Update application status to COVER_LETTER_CREATED
            update_payload = {
                "status": "COVER_LETTER_CREATED",
                "cover_letter_url": cover_letter_url
            }
            await client.patch(f"/api/applications/{app_id}/status", json=update_payload)
        else:
            print("[ORCHESTRATOR] Cover letter not required, skipping generation.")

        # 8. Answer screening questions if any
        screening_answers = {}
        if screening_questions:
            print(f"[ORCHESTRATOR] Answering {len(screening_questions)} screening questions...")
            screening_answers = await answer_screening_questions(screening_questions, resume_data, job, candidate)
            
        # Update application status to QUEUED
        update_payload = {
            "status": "QUEUED",
            "metadata": {
                "screening_answers": screening_answers
            }
        }
        await client.patch(f"/api/applications/{app_id}/status", json=update_payload)

        return {
            "should_apply": True,
            "resume_pdf_url": resume_pdf_url,
            "cover_letter_pdf_url": cover_letter_url,
            "screening_answers": screening_answers
        }
```

---

## File: module3/cover_letter/generator.py

```python
"""Cover letter generation engine using Gemini API."""
from __future__ import annotations

import os
import json
import asyncio
import logging
from pydantic import BaseModel
from google import genai
from google.genai import types as genai_types

from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import ResumeData
from module3.tailoring.pdf_generator import generate_cover_letter_pdf
from module3.utils.storage import safe_filename

logger = logging.getLogger("cover_letter_generator")

class CoverLetter(BaseModel):
    candidate_id: str
    job_id: str
    content: str
    pdf_url: str

_COVER_LETTER_SYSTEM = """
You are an expert career coach who writes sharp, impactful cover letters.
Given a resume JSON and a job description, write a professional cover letter.

Output ONLY a valid JSON object — no markdown, no preamble — in this schema:
{
  "paragraphs": [
    "<One single, combined paragraph containing your entire message. DO NOT output multiple paragraphs.>"
  ]
}

STRICT RULES:
1. ONE PARAGRAPH ONLY: You MUST output exactly ONE string inside the "paragraphs" array. Combine your opening, achievements, and call to action into a single cohesive block of text.
2. WORD COUNT (CRITICAL): The total word count of this single paragraph MUST be strictly between 100 and 120 words. Count carefully before outputting.
3. Content: State your expertise, integrate 2-3 specific numeric achievements from the resume that map to the job, and end with a strong call to action.
4. Tone: Confident, direct, zero filler phrases. Mirror keywords from the job description naturally.
"""

async def generate_cover_letter(
    resume: ResumeData,
    job: NormalizedJob,
    candidate_profile: dict,
    output_pdf_dir: str = "backend/data/cover_letters"
) -> CoverLetter:
    """Generate a highly tailored, concise cover letter PDF using JSON structured output."""
    
    experience_text = "\n".join([
        f"- {exp.title} at {exp.company} ({exp.start_date} - {exp.end_date or 'Present'}): {exp.description} (Tech: {', '.join(exp.technologies)})"
        for exp in resume.sections.experience
    ])
    
    user_prompt = (
        f"--- JOB DETAILS ---\n"
        f"Role Title: {job.title}\n"
        f"Company Name: {job.company}\n"
        f"Description:\n{job.description}\n\n"
        f"--- CANDIDATE DETAILS ---\n"
        f"Candidate Name: {candidate_profile.get('name', 'Candidate')}\n"
        f"Work Experience History:\n{experience_text}\n"
    )

    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    loop = asyncio.get_event_loop()

    try:
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=user_prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=_COVER_LETTER_SYSTEM,
                    temperature=0.2, # Lowered temperature strictly enforces word count and schema limits
                ),
            )
        )
        
        raw_text = response.text.strip()
        
        # Strip markdown fences if the LLM accidentally includes them
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            start = 1
            end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
            raw_text = "\n".join(lines[start:end]).strip()
            
        cl_json = json.loads(raw_text)
        
    except Exception as e:
        logger.error(f"Failed to generate cover letter JSON: {e}")
        # Safe fallback in case of rate limit or JSON schema failure
        cl_json = {
            "paragraphs": [
                "I am writing to express my strong interest in the open position. My technical background aligns well with the core requirements outlined in the job description, and I would welcome the opportunity to discuss how I can leverage my experience to contribute to your engineering team. Thank you for your time and consideration."
            ]
        }

    # Extract the strict single paragraph for the PDF generator
    letter_paragraphs = cl_json.get("paragraphs", [])
    letter_content = "\n\n".join(letter_paragraphs)
    
    candidate_name = candidate_profile.get("name", "Candidate")
    pdf_filename = f"{safe_filename(candidate_name, default='candidate', extension='')}_cover_letter.pdf"
    output_pdf_path = os.path.join(output_pdf_dir, pdf_filename)
    
    candidate_location = candidate_profile.get("location", "US")
    candidate_email = candidate_profile.get("email", "email@example.com")
    candidate_phone = candidate_profile.get("phone", "")
    
    candidate_info = f"{candidate_location}  |  {candidate_email}"
    if candidate_phone:
        candidate_info += f"  |  {candidate_phone}"
        
    print(f"Compiling cover letter PDF to: {output_pdf_path}...")
    
    # Asynchronous non-blocking generation using xhtml2pdf
    await asyncio.to_thread(
        generate_cover_letter_pdf,
        output_path=output_pdf_path,
        candidate_name=candidate_name,
        candidate_info=candidate_info,
        company_name=job.company,
        job_title=job.title,
        letter_text=letter_content
    )
    
    return CoverLetter(
        candidate_id=resume.candidate_id or "candidate-unknown",
        job_id=job.job_id or "job-unknown",
        content=letter_content,
        pdf_url=output_pdf_path
    )
```

---

## File: module3/parser/resume_parser.py

```python
"""Resume parser engine using pdfplumber and Gemini API for structured JSON extraction."""
from __future__ import annotations

import os
import asyncio
import json
import pdfplumber
import pypdfium2 as pdfium
from PIL import Image
from typing import List, Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

class ExperienceEntry(BaseModel):
    company: str = Field(description="Name of the company or organization")
    title: str = Field(description="Job title")
    start_date: str = Field(description="Start date of employment (e.g., 'Jan 2020' or '2020')")
    end_date: Optional[str] = Field(None, description="End date of employment or None/Present if current")
    description: str = Field(description="Work description or responsibilities")
    technologies: List[str] = Field(default_factory=list, description="Technologies, programming languages, or tools used in this job")

class EducationEntry(BaseModel):
    institution: str = Field(description="Name of the school, university, or college")
    degree: str = Field(description="Degree name (e.g., 'Bachelor of Science' or 'BS')")
    field: str = Field(description="Field of study (e.g., 'Computer Science')")
    graduation_year: Optional[int] = Field(None, description="Graduation year (4-digit integer)")

class ResumeSection(BaseModel):
    summary: str = Field(description="Professional summary or profile description")
    current_company: Optional[str] = Field(None, description="Name of the candidate's current or most recent employer company. Return None if not explicitly clear.")
    current_title: Optional[str] = Field(None, description="Candidate's current or most recent job title. Return None if not explicitly clear.")
    salary_expectation: Optional[str] = Field(None, description="Any mention of salary expectations or current salary. Return None if absent.")
    website: Optional[str] = Field(None, description="Personal website, portfolio, GitHub, or LinkedIn URL extracted from contact info. Return None if absent.")
    skills: List[str] = Field(default_factory=list, description="Technical and soft skills")
    keywords: List[str] = Field(default_factory=list, description="Core keywords extracted from summary + skills + experience")
    experience: List[ExperienceEntry] = Field(default_factory=list, description="Employment and work experience details")
    education: List[EducationEntry] = Field(default_factory=list, description="Education details")
    certifications: List[str] = Field(default_factory=list, description="List of professional certifications")

class ResumeData(BaseModel):
    candidate_id: Optional[str] = None
    resume_id: Optional[str] = None
    file_url: str
    file_hash: Optional[str] = None
    sections: ResumeSection
    raw_text: str

def extract_pdf_text(file_path: str) -> str:
    """Extract raw text from a PDF file."""
    text_content = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_content.append(page_text)
    return "\n".join(text_content)

def render_pdf_to_images(file_path: str) -> List[Image.Image]:
    """Render PDF pages to PIL images."""
    doc = pdfium.PdfDocument(file_path)
    images = []
    for page in doc:
        bitmap = page.render(scale=2)
        images.append(bitmap.to_pil())
    return images

async def parse_resume(file_path: str, candidate_id: Optional[str] = None, resume_id: Optional[str] = None) -> ResumeData:
    """Parse a PDF resume into structured ResumeData using Gemini API. Handles both text and image-based PDFs."""
    temp_file_path = None
    original_url = file_path
    if file_path.startswith("http://") or file_path.startswith("https://"):
        print(f"Downloading remote resume: {file_path}")
        import httpx
        import tempfile
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(file_path)
                resp.raise_for_status()
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                    tmp.write(resp.content)
                    temp_file_path = tmp.name
            file_path = temp_file_path
        except Exception as e:
            raise FileNotFoundError(f"Failed to download remote resume from {file_path}: {e}")
            
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Resume file not found at: {file_path}")
        
    print(f"Extracting text from PDF resume: {file_path}...")
    raw_text = ""
    try:
        raw_text = extract_pdf_text(file_path)
    except Exception as e:
        print(f"pdfplumber failed to extract text: {e}. Falling back to image rendering.")
        raw_text = ""

    from module3.utils.gemini import generate_content_with_retry

    prompt = (
        "You are an expert resume parsing system. Analyze the following candidate's resume "
        "and structure it into the requested JSON schema. Make sure to ground all work experience, dates, "
        "institution names, and degrees strictly in the provided content. Do not make up any certifications, "
        "work experience, or education details."
    )

    # Decide if we need multimodal parsing
    if len(raw_text.strip()) < 100:
        print(f"Extracted text length is low ({len(raw_text)} chars). Rendering PDF pages as images for multimodal parsing...")
        try:
            images = render_pdf_to_images(file_path)
            contents = [prompt] + images
            
            response = await generate_content_with_retry(
                contents=contents,
                response_schema=ResumeSection,
                temperature=0.1
            )
            
            # Since text was missing/empty, let's set raw_text to a placeholder or the parsed summary
            raw_text = f"[Image-Based PDF parsed multimodally]"
        except Exception as img_err:
            print("Failed to run image rendering or generate content:", img_err)
            raise ValueError(f"Multimodal parsing failed: {img_err}")
    else:
        print("Using extracted text for parsing...")
        full_prompt = f"{prompt}\n\n--- RAW RESUME TEXT ---\n{raw_text}\n--- END RAW TEXT ---"
        response = await generate_content_with_retry(
            contents=full_prompt,
            response_schema=ResumeSection,
            temperature=0.1
        )
    
    # Parse the JSON response
    try:
        data = json.loads(response.text)
        sections = ResumeSection(**data)
    except Exception as e:
        print("Failed to parse Gemini output as ResumeSection:", e)
        print("Raw response:", response.text)
        raise ValueError(f"Failed to structure resume data: {e}")
        
    # Calculate file hash for idempotency if a valid file path was provided
    file_hash = None
    if os.path.exists(file_path):
        import hashlib
        with open(file_path, "rb") as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()
            
    if temp_file_path and os.path.exists(temp_file_path):
        try:
            os.remove(temp_file_path)
        except OSError:
            pass
            
    return ResumeData(
        candidate_id=candidate_id,
        resume_id=resume_id,
        file_url=original_url,
        file_hash=file_hash,
        sections=sections,
        raw_text=raw_text
    )
```

---

## File: module3/qa/question_answerer.py

```python
"""Screening question answerer engine using Gemini API."""
from __future__ import annotations

import os
import json
import asyncio
from typing import List, Dict
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import ResumeData

class QAEntry(BaseModel):
    question: str = Field(description="The exact screening question")
    answer: str = Field(description="The drafted answer")

class QuestionAnswers(BaseModel):
    answers: List[QAEntry] = Field(description="List of screening questions and drafted answers")

async def answer_screening_questions(
    questions: List[str],
    resume: ResumeData,
    job: NormalizedJob,
    candidate_profile: dict
) -> Dict[str, str]:
    """Draft answers to application screening questions based strictly on candidate profile and resume."""
    if not questions:
        return {}
        
    # Format experience for context
    experience_text = "\n".join([
        f"- {exp.title} at {exp.company} ({exp.start_date} - {exp.end_date or 'Present'}): {exp.description} (Tech: {', '.join(exp.technologies)})"
        for exp in resume.sections.experience
    ])
    
    questions_list_text = "\n".join([f"- {q}" for q in questions])

    prompt = (
        "You are an assistant helping a candidate fill out a job application. Draft professional, concise, "
        "and factual answers for the following screening questions. Follow these rules:\n\n"
        
        "1. Be concise (1-2 sentences maximum for text answers).\n"
        "2. Ground all answers strictly in the candidate's actual profile and work history.\n"
        "3. Do NOT fabricate experience, years of experience, or technical skills.\n"
        "4. For yes/no questions about sponsorship or work authorization, use candidate_work_auth "
        "to determine if they are authorized to work (e.g., if 'us_authorized', they do not need visa sponsorship in the US).\n"
        "5. For salary expectations, use the job salary range if available (e.g. state a number aligned with the job range), "
        "or list 'Negotiable based on package'.\n"
        
        f"--- CANDIDATE DETAILS ---\n"
        f"Name: {candidate_profile.get('name', 'Candidate')}\n"
        f"Work Authorization: {candidate_profile.get('work_auth', 'us_authorized')}\n"
        f"Years Experience: {candidate_profile.get('years_exp', 0)}\n"
        f"Work History Context:\n{experience_text}\n\n"
        
        f"--- JOB DETAILS ---\n"
        f"Role Title: {job.title}\n"
        f"Company Name: {job.company}\n"
        f"Salary Min: {job.salary_min or 'Not specified'}\n"
        f"Salary Max: {job.salary_max or 'Not specified'}\n\n"
        
        f"--- SCREENING QUESTIONS TO ANSWER ---\n"
        f"{questions_list_text}\n"
    )

    from module3.utils.gemini import generate_content_with_retry

    response = await generate_content_with_retry(
        contents=prompt,
        response_schema=QuestionAnswers,
        temperature=0.3
    )

    try:
        data = json.loads(response.text)
        results = QuestionAnswers(**data)
        return {entry.question: entry.answer for entry in results.answers}
    except Exception as e:
        print("Failed to parse Gemini screening question answers:", e)
        print("Raw response:", response.text)
        # Fallback to simple dictionary with empty answers
        return {q: "" for q in questions}
```

---

## File: module3/scoring/fit_scorer.py

```python
"""Candidate job fit scoring engine using Gemini API."""
from __future__ import annotations

import os
import json
import asyncio
from typing import List, Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import ResumeData
from module3.scoring.ats_scorer import calculate_ats_score

class MatchResult(BaseModel):
    job_id: str
    candidate_id: str
    fit_score: float              # 0-100
    ats_score: float              # 0-100
    combined_score: float         # (fit_score × 0.5) + (ats_score × 0.5)
    should_apply: bool            # combined_score >= 70
    matching_skills: List[str]    # skills found in both resume and JD
    missing_skills: List[str]     # skills in JD but not in resume
    experience_match: float       # 0-100, years + domain relevance
    reasoning: str                # LLM explanation of score

class LLMFitEvaluation(BaseModel):
    skills_overlap: float = Field(description="Score between 0 and 100 for technical skills overlap")
    experience_relevance: float = Field(description="Score between 0 and 100 for work history domain relevance")
    location_match: float = Field(description="Score: 100 if location matches Remote/US requirements, 0 otherwise")
    seniority_match: float = Field(description="Score between 0 and 100 matching the seniority level of JD and candidate")
    matching_skills: List[str]
    missing_skills: List[str]
    reasoning: str

async def score_job_fit(
    candidate: dict,
    resume: ResumeData,
    job: NormalizedJob
) -> MatchResult:
    """Evaluate job fit between candidate profile, resume, and normalized job description."""
    # Using central Gemini retry wrapper
    
    # Format candidate details
    candidate_tech_stack = ", ".join(candidate.get("tech_stack", []))
    candidate_years_exp = candidate.get("years_exp", 0)
    candidate_location = candidate.get("location", "US")
    candidate_work_auth = candidate.get("work_auth", "us_authorized")
    
    experience_text = "\n".join([
        f"- {exp.title} at {exp.company} ({exp.start_date} - {exp.end_date or 'Present'}): {exp.description} (Tech: {', '.join(exp.technologies)})"
        for exp in resume.sections.experience
    ])

    prompt = (
        "You are an expert recruitment matching system. Analyze the candidate profile and resume "
        "against the provided Job Description (JD) and evaluate the following dimensions:\n\n"
        
        "1. skills_overlap: Technical skills required in JD versus candidate profile tech stack and resume.\n"
        "2. experience_relevance: Candidate years of experience and work history relevance to JD requirements.\n"
        "3. location_match: Check candidate location and work authorization versus JD. Score as 100 if candidate is Remote or in the required region, otherwise 0.\n"
        "4. seniority_match: Check if candidate matches JD required seniority (Junior, Mid, Senior, Lead). Seniority matching score between 0 and 100.\n"
        "5. matching_skills: List technical skills in JD that candidate possesses.\n"
        "6. missing_skills: List technical skills in JD that candidate is missing.\n"
        "7. reasoning: Return a detailed 2-3 sentence professional hiring explanation of the fit score.\n"
        
        f"--- JOB DESCRIPTION ---\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location}\n"
        f"Description:\n{job.description}\n"
        f"Skills Required: {', '.join(job.skills)}\n\n"
        
        f"--- CANDIDATE PROFILE ---\n"
        f"Tech Stack: {candidate_tech_stack}\n"
        f"Years Experience: {candidate_years_exp}\n"
        f"Location: {candidate_location}\n"
        f"Work Authorization: {candidate_work_auth}\n\n"
        
        f"--- CANDIDATE RESUME ---\n"
        f"Summary: {resume.sections.summary}\n"
        f"Skills: {', '.join(resume.sections.skills)}\n"
        f"Work Experience:\n{experience_text}\n"
    )

    from module3.utils.gemini import generate_content_with_retry
    response = await generate_content_with_retry(
        contents=prompt,
        response_schema=LLMFitEvaluation,
        temperature=0.1
    )

    try:
        eval_data = json.loads(response.text)
        result = LLMFitEvaluation(**eval_data)
    except Exception as e:
        print("Failed to parse LLM Job Fit evaluation response:", e)
        print("Raw response:", response.text)
        raise ValueError(f"Failed to calculate job fit score: {e}")

    # Calculate fit_score using specified weights:
    # skills_overlap: 40%
    # experience_relevance: 35%
    # location_match: 15%
    # seniority_match: 10%
    fit_score = (
        (result.skills_overlap * 0.40) +
        (result.experience_relevance * 0.35) +
        (result.location_match * 0.15) +
        (result.seniority_match * 0.10)
    )
    fit_score = round(max(0.0, min(100.0, fit_score)), 2)

    # Fetch ATS score
    ats_score_obj = await calculate_ats_score(resume, job)
    ats_score = ats_score_obj.overall

    # Combined score is average of fit and ATS
    combined_score = round((fit_score * 0.5) + (ats_score * 0.5), 2)
    should_apply = combined_score >= 70.0

    return MatchResult(
        job_id=str(job.job_id) if job.job_id else "job-unknown",
        candidate_id=str(candidate.get("id")) if candidate.get("id") else "candidate-unknown",
        fit_score=fit_score,
        ats_score=ats_score,
        combined_score=combined_score,
        should_apply=should_apply,
        matching_skills=result.matching_skills,
        missing_skills=result.missing_skills,
        experience_match=round(result.experience_relevance, 2),
        reasoning=result.reasoning
    )
```

---

## File: module3/scoring/ats_scorer.py

```python
"""ATS compatibility scoring engine using Gemini API."""
from __future__ import annotations

import os
import json
import asyncio
from typing import List, Optional
from pydantic import BaseModel, Field

from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import ResumeData

class ATSScore(BaseModel):
    overall: float = Field(description="Overall ATS score between 0 and 100")
    keyword_match: float = Field(description="Score between 0 and 100 for JD keywords found in the resume")
    skills_overlap: float = Field(description="Score between 0 and 100 for core technical skills overlap")
    experience_relevance: float = Field(description="Score between 0 and 100 for years and domain relevance")
    education_match: float = Field(description="Score of 0 or 100 indicating if degree requirements are met")
    formatting_score: float = Field(description="Score between 0 and 100 checking the layout/formatting friendliness")
    missing_keywords: List[str] = Field(default_factory=list, description="Keywords present in the JD but absent from the resume")

_SYSTEM_PROMPT = """
You are an expert ATS (Applicant Tracking System) evaluator. Your job is to score a candidate's resume
against a provided job description. You must output ONLY a valid JSON object with no markdown, no preamble.

Scoring rubric:
- keyword_score_out_of_50: How many required keywords/skills from the JD are present in the resume (0-50)
- experience_score_out_of_30: Relevance and depth of experience to the role (0-30)
- education_score_out_of_20: Education match to requirements (0-20)
- formatting_penalty: Deduct points for poor formatting, missing sections, etc. (0 or negative)

Output schema:
{
  "company_name": "<string>",
  "job_title": "<string>",
  "final_ats_score": <integer 0-100>,
  "scoring_breakdown": {
    "keyword_score_out_of_50": <integer>,
    "experience_score_out_of_30": <integer>,
    "education_score_out_of_20": <integer>,
    "formatting_penalty": <integer>
  },
  "extracted_job_keywords": ["<keyword>", ...],
  "missing_keywords": ["<keyword>", ...],
  "brief_justification": "<string>"
}
"""

async def calculate_ats_score(resume: ResumeData, job: NormalizedJob) -> ATSScore:
    """Calculate the ATS score of a resume against a job description using Gemini."""
    
    experience_text = "\n".join([
        f"- {exp.title} at {exp.company} ({exp.start_date} - {exp.end_date or 'Present'}): {exp.description} (Tech: {', '.join(exp.technologies)})"
        for exp in resume.sections.experience
    ])
    
    education_text = "\n".join([
        f"- {edu.degree} in {edu.field} from {edu.institution} ({edu.graduation_year or 'N/A'})"
        for edu in resume.sections.education
    ])

    job_data = {
        "company_name": job.company,
        "job_title": job.title,
        "description": job.description,
        "requirements": job.skills
    }

    resume_data_str = (
        f"Summary: {resume.sections.summary}\n"
        f"Skills: {', '.join(resume.sections.skills)}\n"
        f"Work Experience:\n{experience_text}\n"
        f"Education:\n{education_text}\n"
    )

    combined_prompt = (
        f"**Candidate Resume:**\n{resume_data_str}\n\n"
        f"**Job Details:**\n```json\n{json.dumps(job_data, indent=2)}\n```\n"
        "Please evaluate the attached resume."
    )

    from module3.utils.gemini import generate_content_with_retry
    
    full_prompt = f"{_SYSTEM_PROMPT}\n\n{combined_prompt}"
    response = await generate_content_with_retry(
        contents=full_prompt,
        temperature=0.0,
        response_mime_type="application/json"
    )

    try:
        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            start = 1
            end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
            raw_text = "\n".join(lines[start:end]).strip()
        
        result = json.loads(raw_text)
    except Exception as e:
        print("Failed to parse LLM ATS evaluation response:", e)
        print("Raw response:", response.text)
        raise ValueError(f"Failed to calculate ATS score: {e}")

    breakdown = result.get("scoring_breakdown", {})
    
    keyword_match = (breakdown.get("keyword_score_out_of_50", 0) / 50.0) * 100
    exp_rel = (breakdown.get("experience_score_out_of_30", 0) / 30.0) * 100
    edu_match = (breakdown.get("education_score_out_of_20", 0) / 20.0) * 100
    formatting = 100 + breakdown.get("formatting_penalty", 0)

    overall = result.get("final_ats_score", 0)
    
    return ATSScore(
        overall=float(overall),
        keyword_match=float(keyword_match),
        skills_overlap=float(keyword_match),
        experience_relevance=float(exp_rel),
        education_match=float(edu_match),
        formatting_score=float(formatting),
        missing_keywords=result.get("missing_keywords", [])
    )
```

---

## File: module3/tailoring/resume_tailor.py

```python
"""Resume tailoring engine using the Fabricator Agent loop and Gemini."""
from __future__ import annotations

import os
import json
import asyncio
import re
import logging
from typing import List
from pydantic import BaseModel, Field
from google import genai
from google.genai import types as genai_types

from module2.normalization.schemas import NormalizedJob
from module3.parser.resume_parser import ResumeData, ExperienceEntry, EducationEntry
from module3.scoring.ats_scorer import calculate_ats_score
from module3.tailoring.pdf_generator import generate_resume_pdf
from module3.utils.storage import safe_filename

logger = logging.getLogger("resume_tailor")

class TailoredResume(BaseModel):
    candidate_id: str
    job_id: str
    version: int
    original_resume_id: str
    modified_summary: str
    modified_skills: List[str]
    modified_keywords: List[str]
    experience: List[ExperienceEntry]
    education: List[EducationEntry]
    pdf_url: str
    ats_score_before: float
    ats_score_after: float

_FABRICATOR_SYSTEM = """
You are a master Resume ATS Optimizer and Senior Technical Recruiter operating in the year 2026.
Your task is to take a candidate's Resume JSON, analyze the ATS Feedback, and heavily enhance the JSON to score ABOVE 90 against the Target Job Description.

You MUST follow these STRICT GUARDRAILS. Violating them is FORBIDDEN:

1. ZERO MARKDOWN (CRITICAL):
   - ABSOLUTELY NO MARKDOWN. Do not use asterisks (**), italics, or bolding anywhere inside the JSON text strings. The text must be clean and plain.

2. HEADLINE & CONTACT INFO:
   - HEADLINE: You MUST generate a professional headline (e.g., "Senior Machine Learning Engineer | Generative AI") under `basics.headline` that matches the Target Job Description.
   - LOCATION: You MUST ensure the location reflects a USA residence. If it is outside the USA, change it to a suitable US tech hub.
   - LINKS: You MUST keep all original links (LinkedIn, GitHub, Portfolio) EXACTLY as they are.

3. SUMMARY & SKILLS:
   - SUMMARY: Edit and rewrite the summary block to naturally weave in missing ATS keywords to guarantee a high score.
   - SKILLS: You may ADD missing ATS keywords to the skills section, but do not erase the candidate's core baseline skills.

4. EXPERIENCE (STRICT NO-FABRICATION RULE):
   - COMPANIES & DATES: You MUST keep the exact company names and dates as listed in the original resume. DO NOT invent new companies.
   - PRESENT ROLE: You are permitted to change the TITLE of the most recent/present role to better align with the target job.
   - BULLET POINTS: You MUST enhance the descriptions of both present and past roles using the XYZ/STAR method. You must weave missing ATS keywords NATURALLY into these sentences.

5. PROJECTS:
   - If projects are provided, enhance their descriptions and listed technologies so they heavily match the job description and requirements.

6. EDUCATION & CERTIFICATIONS (STRICT NO-FABRICATION RULE):
   - EDUCATION: You MUST keep the candidate's exact degree, major, university, descriptions, and dates as listed in the original resume. DO NOT alter, add, or fabricate any educational details.
   - CERTIFICATIONS: DO NOT invent or add new certifications. If certifications exist in the original resume, you MUST retain them EXACTLY as they are with their original dates and descriptions.

7. OUTPUT SCHEMA (CRITICAL):
   - Return ONLY raw, valid JSON. DO NOT wrap in ```json blocks.
   - Schema must include "basics", "summary", "skills", "experience", "education", "projects", and "certifications".
   - "basics" must contain: "name", "headline", "email", "phone", "location", "linkedin", "github_portfolio".
"""

async def tailor_resume(
    resume: ResumeData,
    job: NormalizedJob,
    candidate_profile: dict,
    output_pdf_dir: str = "backend/data/tailored_resumes",
    version: int = 1
) -> TailoredResume:
    """Tailor a candidate's resume by looping through the Fabricator Agent."""
    
    ats_score_before_obj = await calculate_ats_score(resume, job)
    ats_score_before = ats_score_before_obj.overall
    
    current_ats_score = ats_score_before
    current_missing_keywords = ats_score_before_obj.missing_keywords
    
    resume_json = {
        "basics": {
            "name": candidate_profile.get("name", "Candidate"),
            "headline": "",
            "email": candidate_profile.get("email", ""),
            "phone": candidate_profile.get("phone", ""),
            "location": candidate_profile.get("location", ""),
            "linkedin": candidate_profile.get("linkedin_url", ""),
            "github_portfolio": ""
        },
        "summary": resume.sections.summary,
        "skills": [{"category": "General", "keywords": resume.sections.skills}],
        "experience": [
            {
                "company": exp.company,
                "title": exp.title,
                "location": None,
                "date": f"{exp.start_date} - {exp.end_date or 'Present'}",
                "technologies_used": exp.technologies,
                "bullets": [exp.description]
            } for exp in resume.sections.experience
        ],
        "education": [
            {
                "institution": edu.institution,
                "degree": edu.degree,
                "date": str(edu.graduation_year) if edu.graduation_year else None
            } for edu in resume.sections.education
        ],
        "projects": [],
        "certifications": resume.sections.certifications
    }

    job_data = {
        "company_name": job.company,
        "job_title": job.title,
        "description": job.description,
        "requirements": job.skills
    }

    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    loop = asyncio.get_event_loop()

    max_loops = 5
    loop_count = 0
    final_resume_json = resume_json

    while current_ats_score < 90.0 and loop_count < max_loops:
        logger.info(f"Fabrication Loop {loop_count + 1}/{max_loops} - Current ATS: {current_ats_score}")
        
        user_prompt = (
            f"**ATS Feedback:**\nCurrent Score: {current_ats_score}/100\nMissing Keywords: {', '.join(current_missing_keywords)}\n\n"
            f"**Candidate Resume:**\n```json\n{json.dumps(final_resume_json, indent=2)}\n```\n\n"
            f"**Job Details:**\n```json\n{json.dumps(job_data, indent=2)}\n```\n"
        )
        
        try:
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=user_prompt,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=_FABRICATOR_SYSTEM,
                        temperature=0.3,
                    ),
                )
            )
            
            raw_text = response.text.strip()
            if raw_text.startswith("```"):
                lines = raw_text.splitlines()
                start = 1
                end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
                raw_text = "\n".join(lines[start:end]).strip()
                
            final_resume_json = json.loads(raw_text)
            
        except Exception as e:
            logger.error(f"Fabrication failed on loop {loop_count}: {e}")
            break

        # Convert back to internal ResumeData to score again
        temp_skills = []
        for sg in final_resume_json.get("skills", []):
            temp_skills.extend(sg.get("keywords", []))
            
        temp_exp = []
        for exp in final_resume_json.get("experience", []):
            date_str = exp.get("date", "")
            parts = date_str.split("-")
            temp_exp.append(ExperienceEntry(
                company=exp.get("company", ""),
                title=exp.get("title", ""),
                start_date=parts[0].strip() if len(parts) > 0 else "",
                end_date=parts[1].strip() if len(parts) > 1 else "Present",
                description=" ".join(exp.get("bullets", [])),
                technologies=exp.get("technologies_used", [])
            ))
            
        temp_edu = []
        for edu in final_resume_json.get("education", []):
            date_str = edu.get("date", "")
            digits = re.findall(r'\d{4}', str(date_str))
            
            degree_str = edu.get("degree", "")
            field_str = edu.get("field", "") or edu.get("major", "")
            if not field_str and " in " in degree_str:
                parts = degree_str.split(" in ", 1)
                degree_str = parts[0].strip()
                field_str = parts[1].strip()
                
            temp_edu.append(EducationEntry(
                institution=edu.get("institution", ""),
                degree=degree_str,
                field=field_str, 
                graduation_year=int(digits[0]) if digits else None
            ))
            
        temp_resume_data = resume.model_copy(deep=True)
        temp_resume_data.sections.summary = final_resume_json.get("summary", "")
        temp_resume_data.sections.skills = temp_skills
        temp_resume_data.sections.experience = temp_exp
        temp_resume_data.sections.education = temp_edu
        temp_resume_data.sections.certifications = final_resume_json.get("certifications", [])
        
        new_ats_obj = await calculate_ats_score(temp_resume_data, job)
        current_ats_score = new_ats_obj.overall
        current_missing_keywords = new_ats_obj.missing_keywords
        loop_count += 1

    flat_skills = []
    for sg in final_resume_json.get("skills", []):
        flat_skills.extend(sg.get("keywords", []))
        
    final_experience = []
    pdf_experience = []
    for exp in final_resume_json.get("experience", []):
        date_str = exp.get("date", "")
        parts = date_str.split("-")
        final_experience.append(ExperienceEntry(
            company=exp.get("company", "Company"),
            title=exp.get("title", "Position"),
            start_date=parts[0].strip() if len(parts) > 0 else "",
            end_date=parts[1].strip() if len(parts) > 1 else "Present",
            description=" ".join(exp.get("bullets", [])),
            technologies=exp.get("technologies_used", [])
        ))
        pdf_experience.append({
            "company": exp.get("company", "Company"),
            "title": exp.get("title", "Position"),
            "location": exp.get("location", ""),
            "date": exp.get("date", ""),
            "technologies_used": exp.get("technologies_used", []),
            "bullets": exp.get("bullets", [])
        })

    final_education = []
    pdf_education = []
    for edu in final_resume_json.get("education", []):
        date_str = edu.get("date", "")
        digits = re.findall(r'\d{4}', str(date_str))
        
        degree_str = edu.get("degree", "")
        field_str = edu.get("field", "") or edu.get("major", "")
        if not field_str and " in " in degree_str:
            parts = degree_str.split(" in ", 1)
            degree_str = parts[0].strip()
            field_str = parts[1].strip()
            
        final_education.append(EducationEntry(
            institution=edu.get("institution", "Institution"),
            degree=degree_str, 
            field=field_str,
            graduation_year=int(digits[0]) if digits else None
        ))
        pdf_education.append({
            "institution": edu.get("institution", "Institution"),
            "degree": f"{degree_str} in {field_str}" if field_str else degree_str,
            "date": edu.get("date", "")
        })

    basics = final_resume_json.get("basics", {})
    candidate_name = basics.get("name", "Candidate")
    pdf_filename = f"{safe_filename(candidate_name, default='candidate', extension='')}_resume_v{version}.pdf"
    output_pdf_path = os.path.join(output_pdf_dir, pdf_filename)
    pdf_projects = final_resume_json.get("projects", [])

    # Wrap the synchronous rendering in asyncio.to_thread to prevent event loop blocking
    await asyncio.to_thread(
        generate_resume_pdf,
        output_path=output_pdf_path,
        name=basics.get("name", "Candidate"),
        headline=basics.get("headline", ""),
        email=basics.get("email", ""),
        phone=basics.get("phone", ""),
        location=basics.get("location", ""),
        linkedin_url=basics.get("linkedin", ""),
        summary=final_resume_json.get("summary", ""),
        skills=flat_skills,
        experience=pdf_experience,
        education=pdf_education,
        certifications=final_resume_json.get("certifications", []),
        projects=pdf_projects  # Used for rendering, discarded from return payload
    )

    return TailoredResume(
        candidate_id=resume.candidate_id or "candidate-unknown",
        job_id=job.job_id or "job-unknown",
        version=version,
        original_resume_id=resume.resume_id or "resume-unknown",
        modified_summary=final_resume_json.get("summary", ""),
        modified_skills=flat_skills,
        modified_keywords=flat_skills,
        experience=final_experience,
        education=final_education,
        pdf_url=output_pdf_path,
        ats_score_before=ats_score_before,
        ats_score_after=current_ats_score
    )
```

---

## File: module3/tailoring/pdf_generator.py

```python
"""ATS-friendly PDF generator for tailored resumes and cover letters using xhtml2pdf and Jinja2."""
from __future__ import annotations

import os
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from xhtml2pdf import pisa

# Define the path to the templates directory
TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

def _render_pdf(html_content: str, output_path: str) -> None:
    """Helper to render HTML to PDF via xhtml2pdf."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w+b") as result_file:
        pisa_status = pisa.CreatePDF(
            src=html_content,
            dest=result_file
        )
    if pisa_status.err:
        raise RuntimeError(f"Failed to generate PDF at {output_path}")

def generate_resume_pdf(
    output_path: str,
    name: str,
    email: str,
    phone: str,
    location: str,
    linkedin_url: str,
    summary: str,
    skills: list[str],
    experience: list[dict],
    education: list[dict],
    certifications: list[str],
    projects: list[dict] = None  # NEW: Added projects for rendering only
) -> None:
    """Generate a clean, ATS-compliant PDF resume using the HTML template."""
    if projects is None:
        projects = []
        
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template("resume_template.html")
    
    resume_data = {
        "basics": {
            "name": name,
            "email": email,
            "phone": phone,
            "location": location,
            "linkedin": linkedin_url,
            "github_portfolio": ""
        },
        "summary": summary,
        "skills": [{"category": "Core Skills", "keywords": skills}] if skills else [],
        "experience": experience,
        "education": education,
        "certifications": certifications,
        "projects": projects  # Passed to template, but not returned to DB
    }
    
    html_content = template.render(resume=resume_data)
    _render_pdf(html_content, output_path)

def generate_cover_letter_pdf(
    output_path: str,
    candidate_name: str,
    candidate_info: str,
    company_name: str,
    job_title: str,
    letter_text: str
) -> None:
    """Generate a clean cover letter PDF using the HTML template."""
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template("cover_letter_template.html")
    
    paragraphs = [p.strip() for p in letter_text.split('\n') if p.strip()]
    
    letter_data = {
        "hiring_manager": f"Hiring Manager at {company_name}",
        "paragraphs": paragraphs
    }
    
    resume_data = {
        "basics": {"name": candidate_name}
    }
    
    html_content = template.render(letter=letter_data, resume=resume_data)
    _render_pdf(html_content, output_path)
```

---

## File: module3/templates/resume_template.html

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <style>
    @page {
      size: A4;
      margin: 15mm 15mm 15mm 15mm;
    }

    body {
      font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
      font-size: 10pt;
      color: #000000;
      line-height: 1.4;
    }

    /* ── Header ── */
    .header {
      text-align: center;
      padding-bottom: 8pt;
      margin-bottom: 12pt;
    }

    .header h1 {
      font-size: 22pt;
      font-weight: bold;
      color: #000;
      margin: 0 0 4pt 0;
    }
    
    .headline {
      font-size: 12pt;
      font-weight: bold;
      color: #333;
      margin: 4pt 0;
      text-transform: capitalize;
    }

    .contact {
      font-size: 9pt;
      color: #333;
    }

    .contact a {
      color: #333;
      text-decoration: none;
    }

    /* ── Section Headings ── */
    .section { margin-bottom: 14pt; }

    .section-title {
      font-size: 11pt;
      font-weight: bold;
      text-transform: uppercase;
      color: #000;
      border-bottom: 1pt solid #000;
      padding-bottom: 2pt;
      margin-bottom: 8pt;
    }

    /* ── Summary ── */
    .summary { 
      font-size: 9.5pt; 
      text-align: justify;
      margin-top: 0;
    }

    /* ── Skills (Categorized) ── */
    .skills-table {
      width: 100%;
      font-size: 9.5pt;
      border-collapse: collapse;
    }
    .skills-table td {
      padding: 2pt 0;
      vertical-align: top;
    }
    .skill-category {
      font-weight: bold;
      width: 25%;
      padding-right: 10pt;
    }

    /* ── Shared Experience & Projects ── */
    .item-block { margin-bottom: 10pt; }
    
    .item-header {
      display: table;
      width: 100%;
      font-size: 10pt;
      margin-bottom: 2pt;
    }
    
    .item-header-left {
      display: table-cell;
      text-align: left;
      font-weight: bold;
    }
    
    .item-header-right {
      display: table-cell;
      text-align: right;
      white-space: nowrap;
    }

    .item-sub {
      font-style: italic;
      font-size: 9.5pt;
      margin-bottom: 3pt;
      color: #333;
    }

    .tech-used {
      font-size: 9pt;
      color: #444;
      margin-bottom: 4pt;
    }

    .item-block ul {
      margin: 0 0 0 16pt;
      padding: 0;
    }

    .item-block ul li {
      font-size: 9.5pt;
      margin-bottom: 3pt;
      text-align: justify;
    }

    /* ── Education & Certifications ── */
    .edu-block {
      display: table;
      width: 100%;
      margin-bottom: 6pt;
    }
    .edu-left {
      display: table-cell;
      text-align: left;
    }
    .edu-right {
      display: table-cell;
      text-align: right;
      white-space: nowrap;
    }
  </style>
</head>
<body>

  <div class="header">
    <h1>{{ resume.basics.name }}</h1>
    
    {% if resume.basics.headline %}
      <div class="headline">{{ resume.basics.headline }}</div>
    {% endif %}

    <div class="contact">
      {% if resume.basics.location %}{{ resume.basics.location }} | {% endif %}
      {{ resume.basics.email }}
      {% if resume.basics.phone %} | {{ resume.basics.phone }}{% endif %}
      {% if resume.basics.linkedin %} | {{ resume.basics.linkedin }}{% endif %}
      {% if resume.basics.github_portfolio %} | {{ resume.basics.github_portfolio }}{% endif %}
    </div>
  </div>

  {% if resume.summary %}
  <div class="section">
    <div class="section-title">Professional Summary</div>
    <p class="summary">{{ resume.summary }}</p>
  </div>
  {% endif %}

  {% if resume.skills and resume.skills|length > 0 %}
  <div class="section">
    <div class="section-title">Skills</div>
    <table class="skills-table">
      {% for skill_group in resume.skills %}
      <tr>
        <td class="skill-category">{{ skill_group.category }}:</td>
        <td>{{ skill_group.keywords | join(', ') }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
  {% endif %}

  {% if resume.experience and resume.experience|length > 0 %}
  <div class="section">
    <div class="section-title">Professional Experience</div>
    {% for job in resume.experience %}
    <div class="item-block">
      <table style="width: 100%; margin-bottom: 2pt; border-collapse: collapse;">
        <tr>
          <td style="text-align: left; font-weight: bold; font-size: 10pt; padding: 0;">
            {{ job.company }} {% if job.location %} | {{ job.location }}{% endif %}
          </td>
          <td style="text-align: right; font-size: 10pt; white-space: nowrap; padding: 0;">
            {{ job.date }}
          </td>
        </tr>
      </table>
      <div class="item-sub">{{ job.title }}</div>
      {% if job.technologies_used and job.technologies_used|length > 0 %}
      <div class="tech-used"><b>Technologies:</b> {{ job.technologies_used | join(', ') }}</div>
      {% endif %}
      {% if job.bullets and job.bullets|length > 0 %}
      <ul>
        {% for bullet in job.bullets %}
          <li>{{ bullet }}</li>
        {% endfor %}
      </ul>
      {% endif %}
    </div>
    {% endfor %}
  </div>
  {% endif %}

  {% if resume.projects and resume.projects|length > 0 %}
  <div class="section">
    <div class="section-title">Key Projects</div>
    {% for project in resume.projects %}
    <div class="item-block">
      <div class="item-header">
        <div class="item-header-left">{{ project.name }}</div>
      </div>
      {% if project.technologies_used and project.technologies_used|length > 0 %}
      <div class="tech-used"><b>Technologies:</b> {{ project.technologies_used | join(', ') }}</div>
      {% endif %}
      {% if project.bullets and project.bullets|length > 0 %}
      <ul>
        {% for bullet in job.bullets %}
          <li>{{ bullet }}</li>
        {% endfor %}
      </ul>
      {% endif %}
    </div>
    {% endfor %}
  </div>
  {% endif %}

  {% if resume.certifications and resume.certifications|length > 0 %}
  <div class="section">
    <div class="section-title">Certifications</div>
    <ul style="margin: 0 0 0 16pt; padding: 0;">
      {% for cert in resume.certifications %}
        <li style="font-size: 9.5pt; margin-bottom: 2pt;">{{ cert }}</li>
      {% endfor %}
    </ul>
  </div>
  {% endif %}

  {% if resume.education and resume.education|length > 0 %}
  <div class="section">
    <div class="section-title">Education</div>
    {% for edu in resume.education %}
    <table style="width: 100%; margin-bottom: 6pt; border-collapse: collapse;">
      <tr>
        <td style="text-align: left; font-size: 10pt; padding: 0;">
          <b>{{ edu.institution }}</b><br>
          {{ edu.degree }}
        </td>
        <td style="text-align: right; font-size: 10pt; vertical-align: top; white-space: nowrap; padding: 0;">
          {{ edu.date }}
        </td>
      </tr>
    </table>
    {% endfor %}
  </div>
  {% endif %}

</body>
</html>
```

---

## File: module3/templates/cover_letter_template.html

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <style>
    @page {
      size: A4;
      margin: 22mm 20mm 22mm 20mm;
    }

    body {
      font-family: Georgia, "Times New Roman", serif;
      font-size: 11pt;
      color: #1a1a1a;
      line-height: 1.65;
    }

    /* ── Salutation ── */
    .salutation {
      font-size: 11pt;
      font-weight: bold; /* Added bold to the first line */
      margin-bottom: 12pt;
    }

    /* ── Body ── */
    .body p {
      font-size: 11pt;
      text-align: justify;
      margin-bottom: 12pt;
    }

    /* ── Closing ── */
    .sign-off {
      margin-top: 12pt;  
      font-size: 11pt;
      margin-bottom: 2pt; 
    }

    .signature {
      font-weight: bold;
      font-size: 11pt; /* Decreased font size to match document */
      color: #1a3a5c;
      text-transform: uppercase;
    }
  </style>
</head>
<body>

  <div class="salutation">
    Dear Hiring Team,
  </div>

  <div class="body">
    {% for paragraph in letter.paragraphs %}
      <p>{{ paragraph }}</p>
    {% endfor %}
  </div>

  <div class="sign-off">Best regards,</div>
  <div class="signature">{{ resume.basics.name }}</div>

</body>
</html>
```

---

## File: module3/utils/storage.py

```python
import os
import re
import httpx
from typing import Optional


def safe_filename(filename: str, default: str = "file", extension: str = ".pdf") -> str:
    if not filename:
        filename = default
    filename = str(filename).strip()
    filename = filename.replace(" ", "_")
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
    filename = re.sub(r"_+", "_", filename)
    filename = filename.strip("._-")
    if not filename:
        filename = default
    if extension and not filename.lower().endswith(extension.lower()):
        filename += extension
    return filename


def get_env_var_from_file(filepath: str, var_name: str) -> Optional[str]:
    try:
        with open(filepath, "r") as f:
            for line in f:
                if line.startswith(f"{var_name}="):
                    return line.strip().split("=", 1)[1]
    except Exception:
        pass
    return None

SUPABASE_URL = get_env_var_from_file("frontend/.env.local", "NEXT_PUBLIC_SUPABASE_URL") or get_env_var_from_file("frontend/.env", "NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = get_env_var_from_file("frontend/.env.local", "NEXT_PUBLIC_SUPABASE_ANON_KEY") or get_env_var_from_file("frontend/.env", "NEXT_PUBLIC_SUPABASE_ANON_KEY")

async def upload_file_to_supabase(file_path: str, bucket_name: str, file_name: str) -> str:
    """
    Uploads a file to Supabase storage and returns the public URL.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(f"[STORAGE] Warning: Supabase credentials not found. Falling back to local file path: {file_path}")
        return file_path
        
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket_name}/{file_name}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/pdf" # Assuming all these are PDFs
    }
    
    print(f"[STORAGE] Uploading {file_path} to Supabase bucket '{bucket_name}'...")
    try:
        with open(file_path, "rb") as f:
            file_data = f.read()
            
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, content=file_data)
            
            if resp.status_code in (200, 201):
                public_url = f"{SUPABASE_URL}/storage/v1/object/public/{bucket_name}/{file_name}"
                print(f"[STORAGE] Successfully uploaded to {public_url}")
                return public_url
            elif resp.status_code == 400 and "Duplicate" in resp.text:
                # If it already exists, just return the public URL
                public_url = f"{SUPABASE_URL}/storage/v1/object/public/{bucket_name}/{file_name}"
                return public_url
            else:
                print(f"[STORAGE] Failed to upload to Supabase ({resp.status_code}): {resp.text}")
                return file_path
    except Exception as e:
        print(f"[STORAGE] Exception during Supabase upload: {e}")
        return file_path
```

---

## File: module3/utils/gemini.py

```python
import os
import json
import asyncio
import time
import httpx
from typing import Any, List, Union
from google import genai
from google.genai import types

class OpenRouterResponse:
    def __init__(self, text: str, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.text = text
        
        class UsageMetadata:
            def __init__(self, in_tokens: int, out_tokens: int):
                self.prompt_token_count = in_tokens
                self.candidates_token_count = out_tokens
                
        self.usage_metadata = UsageMetadata(prompt_tokens, completion_tokens)

class GeminiResponse:
    def __init__(self, text: str, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.text = text
        
        class UsageMetadata:
            def __init__(self, in_tokens: int, out_tokens: int):
                self.prompt_token_count = in_tokens
                self.candidates_token_count = out_tokens
                
        self.usage_metadata = UsageMetadata(prompt_tokens, completion_tokens)

def _clean_response_text(text: str, is_json: bool) -> str:
    text = text.strip()
    if is_json:
        if "```" in text:
            start_idx = text.find("```")
            eol = text.find("\n", start_idx)
            if eol != -1:
                start_idx = eol
            else:
                start_idx += 3
            end_idx = text.rfind("```")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                text = text[start_idx:end_idx]
        
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            text = text[first_brace:last_brace + 1]
        else:
            first_bracket = text.find("[")
            last_bracket = text.rfind("]")
            if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
                text = text[first_bracket:last_bracket + 1]
    return text.strip()

async def _generate_with_openrouter(api_key, contents, response_schema, temperature, max_retries, initial_delay, response_mime_type):
    openrouter_model = "anthropic/claude-3-haiku"

    if isinstance(contents, list):
        user_content = ""
        for item in contents:
            if isinstance(item, str):
                user_content += item
            elif hasattr(item, "text"):
                user_content += item.text
            else:
                user_content += str(item)
    else:
        user_content = str(contents)

    payload = {
        "model": openrouter_model,
        "messages": [{"role": "user", "content": user_content}],
        "temperature": temperature,
        "max_tokens": 1500,
    }

    if response_schema or response_mime_type == "application/json":
        payload["response_format"] = {"type": "json_object"}
        if response_schema:
            if hasattr(response_schema, "model_json_schema"):
                schema_desc = json.dumps(response_schema.model_json_schema(), indent=2)
            else:
                schema_desc = str(response_schema)
            payload["messages"][0]["content"] += f"\n\nCRITICAL: You must return valid JSON that conforms strictly to this JSON Schema:\n{schema_desc}"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/sabihhaider/BD-Automator-Agent",
        "X-Title": "BD Automator Agent",
    }

    delay = initial_delay
    for attempt in range(max_retries):
        try:
            start_time = time.time()
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload
                )
            
            if resp.status_code == 429 or resp.status_code == 402:
                raise RuntimeError(f"Rate limit or quota hit ({resp.status_code}): {resp.text}")

            if resp.status_code != 200:
                raise ValueError(f"OpenRouter API error ({resp.status_code}): {resp.text}")

            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                raise ValueError(f"OpenRouter returned empty choices: {data}")

            text_content = choices[0]["message"]["content"]
            is_json = bool(response_schema or response_mime_type == "application/json")
            text_content = _clean_response_text(text_content, is_json)

            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)

            latency_ms = (time.time() - start_time) * 1000
            cost_usd = (prompt_tokens / 1_000_000 * 0.075) + (completion_tokens / 1_000_000 * 0.3)

            print(f"[OPENROUTER SUCCESS] Model: {openrouter_model} | Latency: {latency_ms:.0f}ms | Tokens (In/Out): {prompt_tokens}/{completion_tokens} | Est. Cost: ${cost_usd:.6f}")
            return OpenRouterResponse(text_content, prompt_tokens, completion_tokens)

        except Exception as e:
            err_str = str(e)
            is_rate_limit = "429" in err_str or "Rate limit" in err_str or "RESOURCE_EXHAUSTED" in err_str or "402" in err_str

            if is_rate_limit and attempt < max_retries - 1:
                print(f"[OPENROUTER RETRY] Rate limit hit. Retrying in {delay:.2f}s... (Attempt {attempt+1}/{max_retries})")
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2.0)
            else:
                print(f"[OPENROUTER ERROR] Attempt {attempt+1} failed: {e}")
                if attempt == max_retries - 1:
                    raise e
                await asyncio.sleep(1.0)
    return None

async def _generate_with_gemini(api_key, contents, response_schema, temperature, max_retries, initial_delay, model, response_mime_type):
    client = genai.Client(api_key=api_key)

    config_args = {}
    if response_schema:
        config_args["response_schema"] = response_schema
        config_args["response_mime_type"] = "application/json"
    elif response_mime_type:
        config_args["response_mime_type"] = response_mime_type

    if temperature is not None:
        config_args["temperature"] = temperature

    config = types.GenerateContentConfig(**config_args)

    delay = initial_delay
    for attempt in range(max_retries):
        try:
            loop = asyncio.get_event_loop()
            start_time = time.time()
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config
                )
            )
            latency_ms = (time.time() - start_time) * 1000
            
            tokens_in = 0
            tokens_out = 0
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                tokens_in = getattr(response.usage_metadata, 'prompt_token_count', 0)
                tokens_out = getattr(response.usage_metadata, 'candidates_token_count', 0)
                
            cost_usd = (tokens_in / 1_000_000 * 0.10) + (tokens_out / 1_000_000 * 0.40)
            print(f"[GEMINI SUCCESS] Model: {model} | Latency: {latency_ms:.0f}ms | Tokens (In/Out): {tokens_in}/{tokens_out} | Est. Cost: ${cost_usd:.6f}")
            is_json = bool(response_schema or response_mime_type == "application/json")
            cleaned_text = _clean_response_text(response.text, is_json)
            return GeminiResponse(cleaned_text, tokens_in, tokens_out)
        except Exception as e:
            err_str = str(e)
            is_rate_limit = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str

            if is_rate_limit and attempt < max_retries - 1:
                print(f"[GEMINI RETRY] Rate limit hit (429). Retrying in {delay:.2f}s... (Attempt {attempt+1}/{max_retries})")
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2.0)
            else:
                print(f"[GEMINI ERROR] Attempt {attempt+1} failed: {e}")
                if attempt == max_retries - 1:
                    raise e
                await asyncio.sleep(1.0)
    return None

class AnthropicResponse:
    def __init__(self, text: str, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.text = text
        
        class UsageMetadata:
            def __init__(self, in_tokens: int, out_tokens: int):
                self.prompt_token_count = in_tokens
                self.candidates_token_count = out_tokens
                
        self.usage_metadata = UsageMetadata(prompt_tokens, completion_tokens)

async def _generate_with_anthropic(api_key, contents, response_schema, temperature, max_retries, initial_delay, response_mime_type):
    model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    if isinstance(contents, list):
        user_content = ""
        for item in contents:
            if isinstance(item, str):
                user_content += item
            elif hasattr(item, "text"):
                user_content += item.text
            else:
                user_content += str(item)
    else:
        user_content = str(contents)

    payload = {
        "model": model,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": user_content}],
    }
    if temperature is not None:
        payload["temperature"] = temperature

    if response_schema or response_mime_type == "application/json":
        system_prompt = "You are a strict JSON assistant. You must respond with valid JSON and nothing else."
        if response_schema:
            if hasattr(response_schema, "model_json_schema"):
                schema_desc = json.dumps(response_schema.model_json_schema(), indent=2)
            else:
                schema_desc = str(response_schema)
            system_prompt += f"\nReturn a valid JSON object matching this JSON Schema:\n{schema_desc}"
        payload["system"] = system_prompt

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    delay = initial_delay
    for attempt in range(max_retries):
        try:
            start_time = time.time()
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=payload
                )
            
            if resp.status_code == 429 or resp.status_code == 402:
                raise RuntimeError(f"Rate limit or quota hit ({resp.status_code}): {resp.text}")

            if resp.status_code != 200:
                raise ValueError(f"Anthropic API error ({resp.status_code}): {resp.text}")

            data = resp.json()
            content_list = data.get("content", [])
            if not content_list:
                raise ValueError(f"Anthropic returned empty content: {data}")

            text_content = "".join([c.get("text", "") for c in content_list if c.get("type") == "text"]).strip()
            
            usage = data.get("usage", {})
            prompt_tokens = usage.get("input_tokens", 0)
            completion_tokens = usage.get("output_tokens", 0)

            latency_ms = (time.time() - start_time) * 1000
            cost_usd = (prompt_tokens / 1_000_000 * 3.0) + (completion_tokens / 1_000_000 * 15.0)

            print(f"[ANTHROPIC SUCCESS] Model: {model} | Latency: {latency_ms:.0f}ms | Tokens (In/Out): {prompt_tokens}/{completion_tokens} | Est. Cost: ${cost_usd:.6f}")
            is_json = bool(response_schema or response_mime_type == "application/json")
            text_content = _clean_response_text(text_content, is_json)
            return AnthropicResponse(text_content, prompt_tokens, completion_tokens)

        except Exception as e:
            err_str = str(e)
            is_rate_limit = "429" in err_str or "Rate limit" in err_str or "RESOURCE_EXHAUSTED" in err_str or "402" in err_str

            if is_rate_limit and attempt < max_retries - 1:
                print(f"[ANTHROPIC RETRY] Rate limit hit. Retrying in {delay:.2f}s... (Attempt {attempt+1}/{max_retries})")
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2.0)
            else:
                print(f"[ANTHROPIC ERROR] Attempt {attempt+1} failed: {e}")
                if attempt == max_retries - 1:
                    raise e
                await asyncio.sleep(1.0)
    return None

# Keep track of the working provider key across calls to avoid repeatedly switching/retrying failed providers
_working_provider_key = None

async def generate_content_with_retry(
    contents: Union[str, List[Any]],
    response_schema: Any = None,
    temperature: float = 0.3,
    max_retries: int = 10,
    initial_delay: float = 5.0,
    model: str = "gemini-2.5-flash",
    response_mime_type: str = None
) -> Any:
    """
    Wrap model generation with retry, trying available providers in fallback sequence.
    """
    global _working_provider_key
    providers = []
    seen_keys = set()
    
    candidates = [
        ("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY")),
        ("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_API_KEY")),
        ("ANTHROPIC_API_KEY_2", os.getenv("ANTHROPIC_API_KEY_2")),
        ("CLAUDE_API_KEY", os.getenv("CLAUDE_API_KEY")),
        ("OPENROUTER_API_KEY", os.getenv("OPENROUTER_API_KEY")),
    ]
    
    for name, key in candidates:
        if not key:
            continue
        key = key.strip()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        
        if key.startswith("sk-or-"):
            providers.append(("openrouter", key))
        elif key.startswith("sk-ant-") or key.startswith("sk-"):
            providers.append(("anthropic", key))
        else:
            providers.append(("gemini", key))

    if not providers:
        raise ValueError("No AI API keys (GEMINI_API_KEY, ANTHROPIC_API_KEY, etc.) configured in environment variables.")

    # Reorder providers to place the last known working API key at the front of the list
    if _working_provider_key:
        working_idx = -1
        for idx, (p_type, api_key) in enumerate(providers):
            if api_key == _working_provider_key:
                working_idx = idx
                break
        if working_idx > 0:
            working_provider = providers.pop(working_idx)
            providers.insert(0, working_provider)
            print(f"[API ROUTER] Starting with last known working provider: {working_provider[0]}")

    last_error = None
    
    for idx, (provider_type, api_key) in enumerate(providers):
        # If there are subsequent providers available, retry at most once before falling back
        has_fallback = idx < len(providers) - 1
        current_max_retries = 2 if has_fallback else max_retries
        
        try:
            if provider_type == "openrouter":
                res = await _generate_with_openrouter(
                    api_key, contents, response_schema, temperature, 
                    current_max_retries, initial_delay, response_mime_type
                )
                _working_provider_key = api_key
                return res
            elif provider_type == "gemini":
                res = await _generate_with_gemini(
                    api_key, contents, response_schema, temperature, 
                    current_max_retries, initial_delay, model, response_mime_type
                )
                _working_provider_key = api_key
                return res
            elif provider_type == "anthropic":
                res = await _generate_with_anthropic(
                    api_key, contents, response_schema, temperature,
                    current_max_retries, initial_delay, response_mime_type
                )
                _working_provider_key = api_key
                return res
        except Exception as e:
            last_error = e
            print(f"[FALLBACK] Provider '{provider_type}' failed with error: {e}")
            # If the current working key failed, clear it so we don't assume it works next time
            if api_key == _working_provider_key:
                _working_provider_key = None
            if has_fallback:
                print(f"-> Trying next available API key...")

    print("[ERROR] All available API providers failed.")
    raise last_error
```

---

## File: module3/utils/test_storage.py

```python
from module3.utils.storage import safe_filename


def test_safe_filename_basic():
    assert safe_filename("John Doe", default="candidate", extension=".pdf") == "John_Doe.pdf"
    assert safe_filename("John Doe", default="candidate", extension="") == "John_Doe"


def test_safe_filename_handles_special_chars():
    assert safe_filename("Mary-Jane O'Connor", default="candidate", extension=".pdf") == "Mary-Jane_O_Connor.pdf"
    assert safe_filename(" /\\:*?\"<>| ", default="candidate", extension=".pdf") == "candidate.pdf"


def test_safe_filename_collapses_underscores_and_trims():
    assert safe_filename("  Alice   Smith  ", default="candidate", extension=".pdf") == "Alice_Smith.pdf"
    assert safe_filename("__A__B__", default="candidate", extension=".pdf") == "A_B.pdf"
```
