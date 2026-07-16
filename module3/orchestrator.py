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
    # `location` is included deliberately: the parser only started extracting it
    # after the "US" incident, so EVERY parsed_json stored before then has
    # email+phone but no location. Without it here the re-parse never fires for
    # those rows, `sections.location` stays None, and the tailored resume falls
    # back to the (often country-only) DB value — reintroducing the exact bug
    # the parser change was meant to fix. Checking it makes the re-parse
    # self-healing: each stale resume is re-scanned once, then cached.
    return (
        _is_blank(getattr(sections, "email", None))
        or _is_blank(getattr(sections, "phone", None))
        or _is_blank(getattr(sections, "location", None))
    )


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


def _get_threshold() -> float:
    # Default MUST match the documented value (CLAUDE.md / .env.example = 45);
    # a hardcoded 75 silently gated out most jobs when APPLY_SCORE_THRESHOLD
    # was unset.
    try:
        from app.services.runtime_config import get as _rc_get
        return float(_rc_get("apply_score_threshold"))
    except Exception:
        pass
    try:
        return float(os.getenv("APPLY_SCORE_THRESHOLD", "45"))
    except ValueError:
        return 45.0


def _build_resume_data_from_tailored(
    candidate_id: str,
    resume_id: str,
    file_url: str,
    tailored_resume: TailoredResume,
    base_resume_data: ResumeData,
) -> ResumeData:
    """Build a ResumeData object representing the tailored resume, for use by
    downstream steps (cover letter generation, screening Q&A)."""
    return ResumeData(
        candidate_id=candidate_id,
        resume_id=resume_id,
        file_url=file_url,
        sections=ResumeSection(
            summary=tailored_resume.modified_summary,
            skills=tailored_resume.modified_skills,
            skills_categorized=tailored_resume.modified_skills_categorized,
            keywords=tailored_resume.modified_keywords,
            experience=tailored_resume.experience,
            education=tailored_resume.education,
            certifications=base_resume_data.sections.certifications,
            email=base_resume_data.sections.email,
            phone=base_resume_data.sections.phone,
            linkedin_url=base_resume_data.sections.linkedin_url,
            current_company=base_resume_data.sections.current_company,
            current_title=base_resume_data.sections.current_title,
            salary_expectation=base_resume_data.sections.salary_expectation,
            website=base_resume_data.sections.website
        ),
        raw_text="[Tailored Resume]"
    )


async def orchestrate_application_package(
    candidate_id: str,
    job_id: str,
    base_resume_pdf_path: Optional[str] = None,
    screening_questions: Optional[List[str]] = None,
    api_base_url: Optional[str] = None,
    skip_gate: bool = False,
    existing_app_id: Optional[str] = None,
    prefetched_match_result: Optional[MatchResult] = None,
) -> Dict[str, any]:
    """
    Orchestrate candidate application flow:
    Score -> (tailor resume IF ATS score < APPLY_SCORE_THRESHOLD, else use base resume as-is) ->
    Cover Letter -> QA -> Queue for browser automation.

    APPLY_SCORE_THRESHOLD (default 75) decides whether the resume needs tailoring:
      - score >= threshold: the base resume already clears the bar, so tailoring
        is skipped entirely (saves an LLM round-trip) and the base resume is used
        as-is for the cover letter, screening answers, and the queued application.
      - score < threshold: the resume is tailored via the Fabricator loop. If the
        tailored resume STILL doesn't clear the threshold afterward (and
        skip_gate is False), the application is marked ANALYZED and the
        pipeline stops there rather than queuing a poor-fit application.
      - If tailoring itself raises an exception (e.g. the LLM providers are all
        down), the pipeline does not crash: it logs a warning and falls back to
        the base resume so the application can still proceed.

    If existing_app_id is provided the orchestrator reuses that record instead
    of creating a new one (prevents duplicates when matching.py pre-creates it).

    If prefetched_match_result is provided the LLM scoring step is skipped and
    the precomputed scores are used, avoiding a redundant second Gemini call.
    """
    if not api_base_url:
        api_base_url = os.getenv("API_URL") or os.getenv("M1_API_BASE_URL") or "http://127.0.0.1:8002"
    print(f"\n[ORCHESTRATOR] Starting application package preparation for Candidate: {candidate_id} | Job: {job_id}")

    transport = httpx.AsyncHTTPTransport(retries=3)
    async with httpx.AsyncClient(base_url=api_base_url, timeout=120.0, transport=transport) as client:
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

        # 4. Create or reuse Application record in QUEUED status
        if existing_app_id:
            # Reuse the record already created by matching.py (avoids duplicate + redundant patches)
            app_id = existing_app_id
            print(f"[ORCHESTRATOR] Reusing existing application ID: {app_id}")
        else:
            print("[ORCHESTRATOR] Creating application record in 'QUEUED' status...")
            app_payload = {
                "candidate_id": candidate_id,
                "job_id": job_id,
                "status": "QUEUED",
                "resume_id": base_resume_id
            }
            resp = await client.post("/api/applications", json=app_payload)
            if resp.status_code not in (200, 201):
                raise ValueError(f"Failed to create application record: {resp.text}")
            application = resp.json()
            app_id = application["id"]
            print(f"[ORCHESTRATOR] Created application ID: {app_id}")

        # 5. Fit Score & ATS evaluation — skip if caller already computed the scores
        if prefetched_match_result is not None:
            match_result = prefetched_match_result
            print(
                f"[ORCHESTRATOR] Using prefetched scores — "
                f"Fit: {match_result.fit_score} | ATS: {match_result.ats_score} | "
                f"Combined: {match_result.combined_score}"
            )
        else:
            print("[ORCHESTRATOR] Evaluating candidate-job alignment & ATS compatibility...")
            match_result = await score_job_fit(candidate, resume_data, job)
            print(
                f"[ORCHESTRATOR] Scores calculated — "
                f"Fit: {match_result.fit_score} | ATS: {match_result.ats_score} | "
                f"Combined: {match_result.combined_score}"
            )

        threshold_val = _get_threshold()
        should_tailor = match_result.ats_score < threshold_val

        # Defaults assume the base resume is used as-is; overwritten below if tailoring runs.
        tailored_resume: Optional[TailoredResume] = None
        resume_id_for_app = base_resume_id
        resume_pdf_url = base_resume_file_url or (resume_data.file_url if resume_data else "")
        final_resume_data = resume_data

        if should_tailor:
            print(f"[ORCHESTRATOR] ATS score ({match_result.ats_score}) is below threshold ({threshold_val}). Tailoring resume...")

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

            try:
                # Step 1: Resume Tailoring
                print("[ORCHESTRATOR] Running Step 1: Resume Tailoring...")
                tailored_resume = await tailor_resume(
                    resume_data, job, candidate,
                    version=next_version,
                    prefetched_ats_score=match_result.ats_score,
                    prefetched_missing_keywords=match_result.missing_skills,
                )
                print(f"[ORCHESTRATOR] Resume tailored. ATS Score: {tailored_resume.ats_score_before} -> {tailored_resume.ats_score_after}")

                # Check Gate Threshold on TAILORED score
                if tailored_resume.ats_score_after < threshold_val and not skip_gate:
                    print(f"[ORCHESTRATOR] ats_score_after ({tailored_resume.ats_score_after}) is still below gate threshold of {threshold_val}. Transitioning status to ANALYZED and STOPPING.")

                    update_payload = {
                        "status": "ANALYZED",
                        "fit_score": tailored_resume.ats_score_after,
                        "ats_score": tailored_resume.ats_score_after,
                        "combined_score": tailored_resume.ats_score_after,
                        "metadata": {"reason": f"Tailored ATS score ({tailored_resume.ats_score_after}) is below gate threshold of {threshold_val}", "explanation": "Failed even after tailoring"}
                    }
                    await client.patch(f"/api/applications/{app_id}/status", json=update_payload)

                    return {
                        "status": "ANALYZED",
                        "application_id": app_id,
                        "match_result": match_result.model_dump(mode="json"),
                        "tailored_resume_id": base_resume_id,
                        "resume_pdf_url": base_resume_file_url or (resume_data.file_url if resume_data else ""),
                        "cover_letter_url": None,
                        "screening_answers": {}
                    }

                # Upload Tailored Resume to Supabase
                candidate_name = candidate.get("name") or candidate_id
                clean_name = safe_filename(candidate_name, default=candidate_id, extension='')

                remote_resume_url = await upload_file_to_supabase(
                    tailored_resume.pdf_url,
                    "updated_resume",
                    f"{candidate_id}/{job_id}/v{next_version}/{clean_name}_resume_v{next_version}.pdf"
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
                        "skills_categorized": [sg.model_dump() for sg in tailored_resume.modified_skills_categorized] if tailored_resume.modified_skills_categorized else [],
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
                resume_id_for_app = tailored_db_resume["id"]

                # Build the resume data object used by cover letter / QA
                final_resume_data = _build_resume_data_from_tailored(
                    candidate_id, resume_id_for_app, remote_resume_url, tailored_resume, resume_data
                )

            except Exception as tailor_err:
                # Tailoring is best-effort: if it fails for any reason (LLM
                # providers down, parsing failure, etc.), fall back to the base
                # resume rather than crashing the whole pipeline.
                print(f"[ORCHESTRATOR] Warning: resume tailoring failed ({tailor_err}). Falling back to base resume.")
                tailored_resume = None
                resume_id_for_app = base_resume_id
                resume_pdf_url = base_resume_file_url or (resume_data.file_url if resume_data else "")
                final_resume_data = resume_data
        else:
            print(f"[ORCHESTRATOR] ATS score ({match_result.ats_score}) meets/exceeds threshold ({threshold_val}). Using base resume as-is, skipping tailoring.")

        # Step 2: Cover Letter (utilizing tailored resume details if tailored, else base).
        # Non-fatal: an ATS apply without a cover letter is still a real apply.
        cover_letter_url = None
        try:
            print("[ORCHESTRATOR] Running Step 2: Cover Letter Generation...")
            cover_letter = await generate_cover_letter(final_resume_data, job, candidate)
            cover_letter_url = cover_letter.pdf_url
            print(f"[ORCHESTRATOR] Cover Letter compiled to: {cover_letter_url}")

            # Upload Cover letter to Supabase
            candidate_name = candidate.get("name") or candidate_id
            clean_name = safe_filename(candidate_name, default=candidate_id, extension='')
            cover_letter_version = tailored_resume.version if tailored_resume else 1
            remote_cl_url = await upload_file_to_supabase(
                cover_letter_url,
                "cover_letter",
                f"{candidate_id}/{job_id}/v{cover_letter_version}/{clean_name}_cover_letter.pdf"
            )
            cover_letter_url = remote_cl_url
        except Exception as cl_err:
            cover_letter_url = None
            print(f"[ORCHESTRATOR] [DEGRADED] Cover letter generation unavailable ({cl_err}). Proceeding without one.")

        # Step 3: Screening Questions (utilizing tailored resume details if tailored, else base).
        # Non-fatal: the browser agent has its own field-memory fallbacks.
        screening_answers = {}
        if screening_questions:
            try:
                print("[ORCHESTRATOR] Running Step 3: Answering Screening Questions...")
                screening_answers = await answer_screening_questions(screening_questions, final_resume_data, job, candidate)
                print("[ORCHESTRATOR] Screening answers drafted successfully.")
            except Exception as qa_err:
                screening_answers = {}
                print(f"[ORCHESTRATOR] [DEGRADED] Screening answers unavailable ({qa_err}). Browser agent will fall back to field memory.")

        # Transition application to QUEUED status
        print("[ORCHESTRATOR] Queuing application for browser automation...")
        final_update_payload = {
            "status": "QUEUED",
            "resume_id": resume_id_for_app,
            "cover_letter_url": cover_letter_url,
            "fit_score": match_result.fit_score,
            "ats_score": match_result.ats_score,
            "combined_score": match_result.combined_score,
            "metadata": {
                "tailored": tailored_resume is not None,
                "ats_score_before": tailored_resume.ats_score_before if tailored_resume else match_result.ats_score,
                "ats_score_after": tailored_resume.ats_score_after if tailored_resume else match_result.ats_score,
                "screening_answers": screening_answers,
                "explanation": match_result.reasoning
            }
        }
        _final_resp = await client.patch(f"/api/applications/{app_id}/status", json=final_update_payload)
        if _final_resp.status_code not in (200, 201, 204):
            raise ValueError(
                f"Failed to transition app {app_id} to QUEUED: "
                f"{_final_resp.status_code} {_final_resp.text}"
            )

        # Publish event to Redis event bus
        if HAS_EVENTS:
            print("[ORCHESTRATOR] Publishing 'application.package_ready' event to Redis...")
            event_payload = {
                "application_id": str(app_id),
                "resume_url": resume_pdf_url,
                "cover_letter_url": cover_letter_url,
                "screening_answers": screening_answers
            }
            await publish_event("application.package_ready", event_payload)
            print("[ORCHESTRATOR] Redis event published successfully.")

        print(f"[ORCHESTRATOR] [SUCCESS] APPLICATION PACKAGE PREPARATION COMPLETE (App ID: {app_id})")

        return {
            "status": "QUEUED",
            "application_id": app_id,
            "match_result": match_result.model_dump(mode="json"),
            "tailored_resume_id": resume_id_for_app,
            "resume_pdf_url": resume_pdf_url,
            "cover_letter_url": cover_letter_url,
            "screening_answers": screening_answers
        }


async def prepare_package_for_live_application(
    candidate_id: str,
    job_id: str,
    needs_cover_letter: bool,
    screening_questions: List[str],
    api_base_url: Optional[str] = None,
    skip_gate: bool = False
) -> Dict[str, any]:
    """
    Prepare tailored resume, cover letter (if needed), and answer screening questions for live application.
    Runs synchronously and only executes required pipeline steps.

    Same APPLY_SCORE_THRESHOLD (default 75) tailor-vs-base logic as
    orchestrate_application_package: score >= threshold skips tailoring and
    uses the base resume as-is; score < threshold tailors, then gates on the
    resulting score (since this function feeds a LIVE application submission,
    the post-tailoring gate is kept so a still-poor-fit resume doesn't get
    submitted). A tailoring failure falls back to the base resume and is
    treated conservatively (should_apply=False) if the base score doesn't
    already clear the bar.
    """
    if not api_base_url:
        api_base_url = os.getenv("API_URL") or os.getenv("M1_API_BASE_URL") or "http://127.0.0.1:8002"
    print(f"\n[ORCHESTRATOR] Synchronous package preparation for Candidate: {candidate_id} | Job: {job_id}")

    transport = httpx.AsyncHTTPTransport(retries=3)
    async with httpx.AsyncClient(base_url=api_base_url, timeout=120.0, transport=transport) as client:
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
        resp = await client.get("/api/applications", params={"candidate_id": candidate_id, "job_id": job_id})
        if resp.status_code == 200:
            apps = resp.json()
            if apps:
                app_id = apps[0]["id"]

        if not app_id:
            print("[ORCHESTRATOR] Application record not found. Creating in 'QUEUED' status...")
            app_payload = {
                "candidate_id": candidate_id,
                "job_id": job_id,
                "status": "QUEUED",
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

        threshold_val = _get_threshold()
        should_tailor = match_result.ats_score < threshold_val

        tailored_resume: Optional[TailoredResume] = None
        resume_pdf_url = base_resume_file_url or (resume_data.file_url if resume_data else "")
        final_resume_data = resume_data
        tailored_resume_id = base_resume_id

        if should_tailor:
            print(f"[ORCHESTRATOR] ATS score ({match_result.ats_score}) is below threshold ({threshold_val}). Tailoring resume...")

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

            try:
                # Step 1: Resume Tailoring
                print("[ORCHESTRATOR] Running Step 1: Resume Tailoring...")
                tailored_resume = await tailor_resume(
                    resume_data, job, candidate,
                    version=next_version,
                    prefetched_ats_score=match_result.ats_score,
                    prefetched_missing_keywords=match_result.missing_skills,
                )
                print(f"[ORCHESTRATOR] Resume tailored. ATS Score: {tailored_resume.ats_score_before} -> {tailored_resume.ats_score_after}")

                # Check Gate Threshold on TAILORED score — this gate stays even
                # though orchestrate_application_package has no apply-gate,
                # because this function submits a LIVE application: a resume
                # that still doesn't clear the bar after tailoring should not
                # be auto-submitted.
                if tailored_resume.ats_score_after < threshold_val and not skip_gate:
                    print(f"[ORCHESTRATOR] ats_score_after ({tailored_resume.ats_score_after}) is still below gate threshold of {threshold_val}. Transitioning status to ANALYZED and STOPPING.")

                    update_payload = {
                        "status": "ANALYZED",
                        "fit_score": tailored_resume.ats_score_after,
                        "ats_score": tailored_resume.ats_score_after,
                        "combined_score": tailored_resume.ats_score_after,
                        "metadata": {"reason": f"Tailored ATS score ({tailored_resume.ats_score_after}) is below gate threshold of {threshold_val}", "explanation": "Failed even after tailoring"}
                    }
                    await client.patch(f"/api/applications/{app_id}/status", json=update_payload)

                    return {
                        "should_apply": False,
                        "reason": f"Tailored ATS score ({tailored_resume.ats_score_after}) is below gate threshold of {threshold_val}."
                    }

                # Transition application to QUEUED
                print("[ORCHESTRATOR] Tailored score clears threshold. Transitioning status to 'QUEUED'...")
                update_payload = {
                    "status": "QUEUED",
                    "fit_score": match_result.fit_score,
                    "ats_score": match_result.ats_score,
                    "combined_score": match_result.combined_score,
                    "metadata": {"explanation": match_result.reasoning}
                }
                await client.patch(f"/api/applications/{app_id}/status", json=update_payload)

                # Upload Tailored Resume to Supabase
                candidate_name = candidate.get("name") or candidate_id
                clean_name = safe_filename(candidate_name, default=candidate_id, extension='')

                remote_resume_url = await upload_file_to_supabase(
                    tailored_resume.pdf_url,
                    "updated_resume",
                    f"{candidate_id}/{job_id}/v{next_version}/{clean_name}_resume_v{next_version}.pdf"
                )
                resume_pdf_url = remote_resume_url

                # Upload Tailored Resume version to central DB
                print("[ORCHESTRATOR] Uploading tailored resume version to database...")
                tailored_payload = {
                    "candidate_id": candidate_id,
                    "version": tailored_resume.version,
                    "file_url": remote_resume_url,
                    "parsed_json": {
                        "summary": tailored_resume.modified_summary,
                        "skills": tailored_resume.modified_skills,
                        "skills_categorized": [sg.model_dump() for sg in tailored_resume.modified_skills_categorized] if tailored_resume.modified_skills_categorized else [],
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

                # Update application status with tailored resume ID
                update_payload = {
                    "status": "QUEUED",
                    "resume_id": tailored_resume_id,
                    "metadata": {
                        "ats_score_before": tailored_resume.ats_score_before,
                        "ats_score_after": tailored_resume.ats_score_after
                    }
                }
                await client.patch(f"/api/applications/{app_id}/status", json=update_payload)

                # Build the resume data object used by cover letter / QA
                final_resume_data = _build_resume_data_from_tailored(
                    candidate_id, tailored_resume_id, remote_resume_url, tailored_resume, resume_data
                )

            except Exception as tailor_err:
                # Tailoring is best-effort. If it fails outright, fall back to
                # the base resume. Since this base resume was already below
                # threshold (that's why tailoring was attempted), we cannot
                # confirm it's good enough for a LIVE submission — be
                # conservative and block, unless skip_gate is set.
                print(f"[ORCHESTRATOR] Warning: resume tailoring failed ({tailor_err}). Falling back to base resume.")
                tailored_resume = None
                resume_pdf_url = base_resume_file_url or (resume_data.file_url if resume_data else "")
                final_resume_data = resume_data
                tailored_resume_id = base_resume_id

                if not skip_gate:
                    update_payload = {
                        "status": "ANALYZED",
                        "fit_score": match_result.fit_score,
                        "ats_score": match_result.ats_score,
                        "combined_score": match_result.combined_score,
                        "metadata": {"reason": f"Resume tailoring failed ({tailor_err}); base ATS score ({match_result.ats_score}) is below gate threshold of {threshold_val}", "explanation": "Tailoring error"}
                    }
                    await client.patch(f"/api/applications/{app_id}/status", json=update_payload)
                    return {
                        "should_apply": False,
                        "reason": f"Resume tailoring failed and base ATS score ({match_result.ats_score}) is below gate threshold of {threshold_val}."
                    }
        else:
            print(f"[ORCHESTRATOR] ATS score ({match_result.ats_score}) meets/exceeds threshold ({threshold_val}). Using base resume as-is, skipping tailoring.")

            # Transition application to QUEUED directly — base resume already clears the bar
            print("[ORCHESTRATOR] Base score meets threshold. Transitioning status to 'QUEUED'...")
            update_payload = {
                "status": "QUEUED",
                "fit_score": match_result.fit_score,
                "ats_score": match_result.ats_score,
                "combined_score": match_result.combined_score,
                "metadata": {"explanation": match_result.reasoning}
            }
            await client.patch(f"/api/applications/{app_id}/status", json=update_payload)

        # Step 2: Generate Cover Letter only if needs_cover_letter is True
        cover_letter_url = None
        if needs_cover_letter:
            print("[ORCHESTRATOR] Running Step 2: Cover Letter Generation (utilizing resume details)...")
            cover_letter = await generate_cover_letter(final_resume_data, job, candidate)
            cover_letter_url = cover_letter.pdf_url

            # Upload Cover letter to Supabase
            candidate_name = candidate.get("name") or candidate_id
            clean_name = safe_filename(candidate_name, default=candidate_id, extension='')
            cover_letter_version = tailored_resume.version if tailored_resume else 1
            remote_cl_url = await upload_file_to_supabase(
                cover_letter_url,
                "cover_letter",
                f"{candidate_id}/{job_id}/v{cover_letter_version}/{clean_name}_cover_letter.pdf"
            )
            cover_letter_url = remote_cl_url

            # Update application status
            update_payload = {
                "status": "QUEUED",
                "cover_letter_url": cover_letter_url
            }
            await client.patch(f"/api/applications/{app_id}/status", json=update_payload)
        else:
            print("[ORCHESTRATOR] Cover letter not required, skipping generation.")

        # Step 3: Answer screening questions if any
        screening_answers = {}
        if screening_questions:
            print(f"[ORCHESTRATOR] Running Step 3: Answering {len(screening_questions)} screening questions (utilizing resume details)...")
            screening_answers = await answer_screening_questions(screening_questions, final_resume_data, job, candidate)

            # Update application status
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
