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


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _candidate_with_resume_contact_fallbacks(candidate: dict, resume_data: ResumeData) -> dict:
    """
    Build the candidate profile used by generated documents.

    Non-empty UI/profile fields are authoritative. Blank profile contact fields
    fall back to the contact values parsed from the base resume PDF.
    """
    effective = dict(candidate or {})
    sections = resume_data.sections

    fallback_map = {
        "email": getattr(sections, "email", None),
        "phone": getattr(sections, "phone", None),
        "linkedin_url": getattr(sections, "linkedin_url", None) or getattr(sections, "website", None),
    }
    for field, fallback in fallback_map.items():
        if _is_blank(effective.get(field)) and not _is_blank(fallback):
            effective[field] = fallback

    return effective


def _resume_contact_is_incomplete(resume_data: ResumeData) -> bool:
    sections = resume_data.sections
    return _is_blank(getattr(sections, "email", None)) or _is_blank(getattr(sections, "phone", None))


async def _refresh_resume_contacts_if_needed(
    client: httpx.AsyncClient,
    resume_data: ResumeData,
    resume_id: Optional[str],
    file_url: Optional[str],
) -> ResumeData:
    """Re-parse the base PDF when older stored parsed_json lacks contact fields."""
    if not resume_data or not resume_id or not file_url or not _resume_contact_is_incomplete(resume_data):
        return resume_data

    try:
        print("[ORCHESTRATOR] Stored parsed_json is missing contact fields. Re-scanning base resume PDF...")
        reparsed = await parse_resume(file_url, candidate_id=resume_data.candidate_id, resume_id=resume_id)
        patch_resp = await client.patch(
            f"/api/resumes/{resume_id}",
            json={"parsed_json": reparsed.sections.model_dump()},
        )
        if patch_resp.status_code not in (200, 204):
            print(f"[ORCHESTRATOR] Warning: could not persist contact-enriched parsed_json ({patch_resp.status_code}): {patch_resp.text}")
        return reparsed
    except Exception as exc:
        print(f"[ORCHESTRATOR] Warning: could not re-scan base resume contacts: {exc}")
        return resume_data

async def orchestrate_application_package(
    candidate_id: str,
    job_id: str,
    base_resume_pdf_path: Optional[str] = None,
    screening_questions: Optional[List[str]] = None,
    api_base_url: str = "http://127.0.0.1:8000",
    skip_gate: bool = False
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
        base_resume_file_url = None
        
        if resp.status_code == 200 and resp.json():
            resumes = sorted(resp.json(), key=lambda r: r.get("version", 0))
            # Find the latest base resume
            base_resume = resumes[-1]
            base_resume_id = base_resume["id"]
            base_resume_file_url = base_resume.get("file_url", "")
            parsed_json = base_resume.get("parsed_json")
            if parsed_json:
                sections = ResumeSection(**parsed_json)
                resume_data = ResumeData(
                    candidate_id=candidate_id,
                    resume_id=base_resume_id,
                    file_url=base_resume_file_url,
                    sections=sections,
                    raw_text="[Loaded from DB]"
                )
                print(f"[ORCHESTRATOR] Loaded existing base resume from DB (ID: {base_resume_id})")
            else:
                # Base resume record exists but parsed_json is NULL — auto-parse from file_url
                file_url = base_resume.get("file_url", "")
                if file_url:
                    print(f"[ORCHESTRATOR] Base resume has no parsed_json. Parsing from file/URL: {file_url}")
                    parsed_resume = await parse_resume(file_url, candidate_id=candidate_id, resume_id=base_resume_id)
                    # Persist parsed_json back to DB via resume update
                    update_payload = {"parsed_json": parsed_resume.sections.model_dump()}
                    patch_resp = await client.patch(f"/api/resumes/{base_resume_id}", json=update_payload)
                    if patch_resp.status_code not in (200, 204):
                        print(f"[ORCHESTRATOR] Warning: could not persist parsed_json ({patch_resp.status_code}): {patch_resp.text}")
                    resume_data = parsed_resume
                    resume_data.resume_id = base_resume_id
                    print(f"[ORCHESTRATOR] Auto-parsed base resume and updated DB (ID: {base_resume_id})")
        
        if resume_data:
            resume_data = await _refresh_resume_contacts_if_needed(
                client,
                resume_data,
                base_resume_id,
                base_resume_file_url or resume_data.file_url,
            )
            candidate = _candidate_with_resume_contact_fallbacks(candidate, resume_data)
        
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
            candidate = _candidate_with_resume_contact_fallbacks(candidate, resume_data)

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
        if not match_result.should_apply and not skip_gate:
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
            "resume_pdf_url": resume_pdf_url,
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
        base_resume_file_url = None
        
        if resp.status_code == 200 and resp.json():
            resumes = sorted(resp.json(), key=lambda r: r.get("version", 0))
            # Find the latest base resume
            base_resume = resumes[-1]
            base_resume_id = base_resume["id"]
            base_resume_file_url = base_resume.get("file_url", "")
            parsed_json = base_resume.get("parsed_json")
            if parsed_json:
                sections = ResumeSection(**parsed_json)
                resume_data = ResumeData(
                    candidate_id=candidate_id,
                    resume_id=base_resume_id,
                    file_url=base_resume_file_url,
                    sections=sections,
                    raw_text="[Loaded from DB]"
                )
                print(f"[ORCHESTRATOR] Loaded existing base resume from DB (ID: {base_resume_id})")
            else:
                # Base resume record exists but parsed_json is NULL — auto-parse from file_url
                file_url = base_resume.get("file_url", "")
                if file_url:
                    print(f"[ORCHESTRATOR] Base resume has no parsed_json. Parsing from file/URL: {file_url}")
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

        resume_data = await _refresh_resume_contacts_if_needed(
            client,
            resume_data,
            base_resume_id,
            base_resume_file_url or resume_data.file_url,
        )
        candidate = _candidate_with_resume_contact_fallbacks(candidate, resume_data)

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

        # Always tailor a new resume version from the base resume
        tailored_resume_id = None
        resume_pdf_url = None
        
        # Calculate next version
        next_version = 2
        all_resumes_resp = await client.get(f"/api/resumes/{candidate_id}")
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
