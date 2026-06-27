#!/usr/bin/env python3
"""
Full End-to-End Pipeline Test — Module 1 → 2 → 3 → 4
======================================================

REAL SCENARIO — no dry run, no mocks.

Flow:
    M1  — Resolve candidate "Sabih Haider" from DB
    M1  — Fetch a random open job from DB (or use --job-id / --platform / URL)
    M3  — Fetch base resume from Supabase (via DB)
    M3  — Score job fit (LLM / Gemini)  — gate = 70
    M3  — Tailor resume  → upload to Supabase → save to DB
    M3  — Generate cover letter → upload to Supabase → save to DB (application record)
    M3  — Answer screening questions (if any)
    M4  — Build ApplicationPackage and run ApplicationExecutor (REAL submit)
    M1  — Update application status to SUBMITTED / FAILED in DB
    LOG — Write full evidence JSON to test_e2e_evidence.json

Usage:
    # Auto-pick a random matching job and REALLY submit:
    python test_full_pipeline_e2e.py

    # Pin a specific job from the DB:
    python test_full_pipeline_e2e.py --job-id <uuid>

    # Pin by platform (greenhouse / remoterocketship / lever / ashby / dice):
    python test_full_pipeline_e2e.py --platform greenhouse

    # Pin by explicit URL:
    python test_full_pipeline_e2e.py https://job-boards.greenhouse.io/vercel/jobs/5999792004

    # Override candidate (default = sabih):
    python test_full_pipeline_e2e.py --candidate "sabih"

    # Dry-run (stop before real browser submit — useful for M3 debugging):
    python test_full_pipeline_e2e.py --dry-run

    # Show browser window:
    python test_full_pipeline_e2e.py --no-headless

ENVIRONMENT:
    All secrets are loaded from backend/.env automatically.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

# ── path bootstrap ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / "backend" / ".env")

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(PROJECT_ROOT / "test_e2e_pipeline.log", mode="w"),
    ],
)
logger = logging.getLogger("e2e-pipeline")
for noisy in ("httpx", "httpcore", "anthropic", "urllib3", "playwright"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ── constants ─────────────────────────────────────────────────────────────────
API_BASE = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api")
API_BASE_URL = API_BASE.rstrip("/api").rstrip("/") if API_BASE.endswith("/api") else API_BASE

_WORK_AUTH_TYPE_MAP = {
    "citizen": "US Citizen",
    "us_authorized": "US Citizen",
    "green_card": "Green Card Holder",
    "visa": "H1B",
    "ead": "OPT",
}

SECTION_SEP = "=" * 72


def banner(title: str) -> None:
    logger.info(SECTION_SEP)
    logger.info(f"  {title}")
    logger.info(SECTION_SEP)


def step(msg: str) -> None:
    logger.info(f"  ▶  {msg}")


def ok(msg: str) -> None:
    logger.info(f"  ✓  {msg}")


def warn(msg: str) -> None:
    logger.warning(f"  ⚠  {msg}")


def fail(msg: str) -> None:
    logger.error(f"  ✗  {msg}")


# ── M1 helpers — DB queries via SQLAlchemy ────────────────────────────────────

async def resolve_candidate(query: str) -> Dict[str, Any]:
    """Resolve candidate by name/email/UUID from DB."""
    from sqlalchemy import select, or_
    from app.database import AsyncSessionLocal
    from app.models.candidate import Candidate

    q = (query or "").strip()
    if not q:
        raise RuntimeError("No candidate query provided. Pass --candidate or set TEST_CANDIDATE env var.")

    is_uuid = bool(re.match(r"^[0-9a-fA-F-]{32,36}$", q))
    async with AsyncSessionLocal() as s:
        conds = [Candidate.name.ilike(f"%{q}%"), Candidate.email.ilike(f"%{q}%")]
        if is_uuid:
            conds.append(Candidate.id == q)
        cand = (
            await s.execute(select(Candidate).where(or_(*conds)).limit(1))
        ).scalar_one_or_none()

    if not cand:
        raise RuntimeError(f"No candidate matching {q!r} found in the database.")

    work_auth = (getattr(cand, "work_auth", None) or "us_authorized").lower()
    is_auth = work_auth in ("us_authorized", "citizen", "green_card", "visa", "ead")
    full_name = (cand.name or "").strip()
    parts = full_name.split()

    profile = {
        "_candidate_id": str(cand.id),
        "name": full_name,
        "first_name": parts[0] if parts else "",
        "last_name": " ".join(parts[1:]) if len(parts) > 1 else "",
        "email": cand.email or "",
        "phone": cand.phone or "",
        "location": cand.location or "",
        "linkedin_url": getattr(cand, "linkedin_url", "") or "",
        "website": getattr(cand, "website", "") or "",
        "experience_years": str(getattr(cand, "years_exp", "") or ""),
        "tech_stack": ", ".join(cand.tech_stack or []),
        "current_title": getattr(cand, "current_title", "") or "",
        "current_company": getattr(cand, "current_company", "") or "",
        "education": getattr(cand, "education", "") or "",
        "salary_expectation": getattr(cand, "salary_expectation", "") or "",
        "work_authorization": "Yes" if is_auth else "No",
        "work_authorization_type": _WORK_AUTH_TYPE_MAP.get(work_auth, "Other"),
        "sponsorship": "No" if is_auth else "Yes",
        # Boilerplate answers for checkbox / consent questions
        "agree_terms": "Yes",
        "willing_to_relocate": "Yes",
        "background_check": "Yes",
        "drug_test": "Yes",
        "referral_source": "Online",
        "start_date": "Immediately",
    }

    ok(f"Candidate resolved: {full_name!r}  id={cand.id}  email={cand.email!r}  work_auth={work_auth}")
    return profile


async def resolve_job(args) -> Dict[str, Any]:
    """Resolve a job from the DB — by ID, platform, URL arg, or smart random pick.

    When no job is specified we pick a random job that matches relevant engineering
    keywords from the DB so we don't waste LLM calls on obviously mismatched roles
    (e.g. Partner Manager when the candidate is a Software Engineer).
    """
    from sqlalchemy import select, func, or_
    from app.database import AsyncSessionLocal
    from app.models.job import Job

    # Keywords that signal a software-engineering role relevant to Sabih's profile
    RELEVANT_TITLE_KEYWORDS = [
        "software engineer", "software developer", "full stack", "fullstack",
        "full-stack", "backend engineer", "frontend engineer", "web developer",
        "web engineer", "python developer", "node developer", "react developer",
        "typescript", "javascript developer", "api engineer", "platform engineer",
        "application engineer", "engineer ii", "engineer iii", "sr. engineer",
        "senior engineer", "staff engineer", "principal engineer",
    ]

    def _row_to_dict(j) -> Dict[str, Any]:
        return {
            "job_id": str(j.id),
            "job_url": j.source_url,
            "platform": (j.source or j.source_url or "").lower(),
            "title": j.title or "",
            "company": j.company or "",
            "description": j.description or "",
            "skills": j.skills or [],
        }

    if args.job_id:
        async with AsyncSessionLocal() as s:
            j = (
                await s.execute(select(Job).where(Job.id == args.job_id))
            ).scalar_one_or_none()
        if not j:
            raise RuntimeError(f"No job with id={args.job_id}")
        ok(f"Job resolved by ID: {j.title!r} @ {j.company!r}")
        return _row_to_dict(j)

    if args.platform:
        async with AsyncSessionLocal() as s:
            # Pick platform + relevant title keywords
            kw_conds = [Job.title.ilike(f"%{kw}%") for kw in RELEVANT_TITLE_KEYWORDS]
            j = (
                await s.execute(
                    select(Job)
                    .where(
                        Job.source.ilike(f"%{args.platform}%"),
                        Job.is_duplicate == False,
                        or_(*kw_conds),
                    )
                    .order_by(Job.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if not j:
                # Fallback: any job for that platform
                j = (
                    await s.execute(
                        select(Job)
                        .where(Job.source.ilike(f"%{args.platform}%"))
                        .order_by(Job.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
        if not j:
            raise RuntimeError(f"No job in DB with source matching {args.platform!r}")
        ok(f"Auto-picked newest {args.platform!r} job: {j.title!r} @ {j.company!r}")
        return _row_to_dict(j)

    if args.url:
        ok(f"Using explicit URL: {args.url}")
        return {
            "job_id": None,
            "job_url": args.url,
            "platform": args.url,
            "title": "",
            "company": "",
            "description": "",
            "skills": [],
        }

    # No args — smart random pick filtered by engineering keywords
    step("No job specified — picking a random relevant engineering job from DB...")
    async with AsyncSessionLocal() as s:
        kw_conds = [Job.title.ilike(f"%{kw}%") for kw in RELEVANT_TITLE_KEYWORDS]
        # Count matching jobs first
        count_res = await s.execute(
            select(func.count())
            .select_from(Job)
            .where(
                Job.is_duplicate == False,
                Job.source_url.isnot(None),
                or_(*kw_conds),
            )
        )
        total = count_res.scalar_one()

        if total > 0:
            offset = random.randint(0, max(0, total - 1))
            j = (
                await s.execute(
                    select(Job)
                    .where(
                        Job.is_duplicate == False,
                        Job.source_url.isnot(None),
                        or_(*kw_conds),
                    )
                    .offset(offset)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if j:
                ok(f"Smart-random job picked: {j.title!r} @ {j.company!r}  [{j.source}]")
                return _row_to_dict(j)

        # Fallback: truly random if no keyword match
        warn("No keyword-filtered jobs found — falling back to fully random pick.")
        count_res = await s.execute(
            select(func.count()).select_from(Job).where(Job.is_duplicate == False)
        )
        total = count_res.scalar_one()
        if total == 0:
            raise RuntimeError("No jobs found in DB.")
        offset = random.randint(0, max(0, total - 1))
        j = (
            await s.execute(
                select(Job)
                .where(Job.is_duplicate == False, Job.source_url.isnot(None))
                .offset(offset)
                .limit(1)
            )
        ).scalar_one_or_none()

    if not j:
        raise RuntimeError("Could not pick a job from DB.")

    ok(f"Random job picked: {j.title!r} @ {j.company!r}  [{j.source}]")
    return _row_to_dict(j)


async def resolve_base_resume(candidate_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the base resume record from the DB."""
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.resume import Resume

    async with AsyncSessionLocal() as s:
        r = (
            await s.execute(
                select(Resume)
                .where(Resume.candidate_id == candidate_id, Resume.is_base == True)
                .order_by(Resume.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if not r:
        return None

    return {
        "id": str(r.id),
        "file_url": r.file_url or "",
        "parsed_json": r.parsed_json,
        "version": r.version,
        "embedding": r.embedding,
    }


# ── M3 helpers — orchestrator pipeline ────────────────────────────────────────

async def run_m3_pipeline(
    candidate_id: str,
    job_dict: Dict[str, Any],
    base_resume: Dict[str, Any],
    api_base_url: str,
) -> Dict[str, Any]:
    """
    Run the full M3 pipeline:
      - Score job fit (LLM)
      - Tailor resume        ← ALWAYS runs (skip_gate=True)
      - Generate cover letter ← ALWAYS runs
      - Upload both to Supabase
      - Save to DB and update application status
      - Return package dict ready for M4

    Uses orchestrate_application_package (NOT prepare_package_for_live_application)
    because only the former supports skip_gate=True — ensuring tailored resume and
    cover letter are ALWAYS produced even when the fit score is below 70.
    """
    from module3.orchestrator import orchestrate_application_package

    job_id = job_dict.get("job_id")
    if not job_id:
        raise ValueError("job_dict must contain a real DB job_id for M3 pipeline.")

    step("Running M3 orchestrator (score → tailor → cover letter → upload)...")
    step("NOTE: skip_gate=True — tailored resume + cover letter generated regardless of fit score.")
    result = await orchestrate_application_package(
        candidate_id=candidate_id,
        job_id=job_id,
        base_resume_pdf_path=None,   # use DB record
        screening_questions=[],
        api_base_url=api_base_url,
        skip_gate=True,              # ← always tailor, never stop at gate
    )
    # orchestrate_application_package returns:
    # {
    #   "status": "QUEUED",
    #   "application_id": ...,
    #   "match_result": {...},
    #   "tailored_resume_id": ...,
    #   "resume_pdf_url": ...,        ← tailored resume Supabase URL
    #   "cover_letter_url": ...,      ← cover letter Supabase URL
    #   "screening_answers": {},
    # }
    # Normalise to the shape the rest of the test expects.
    return {
        "should_apply": result.get("status") == "QUEUED",
        "status": result.get("status", "QUEUED"),
        "application_id": result.get("application_id"),
        "resume_pdf_url": result.get("resume_pdf_url"),
        "cover_letter_pdf_url": result.get("cover_letter_url"),
        "screening_answers": result.get("screening_answers", {}),
        "match_result": result.get("match_result", {}),
    }


# ── M4 helpers — browser executor ─────────────────────────────────────────────

async def run_m4_executor(
    candidate_id: str,
    job_dict: Dict[str, Any],
    profile: Dict[str, Any],
    resume_url: str,
    cover_letter_url: Optional[str],
    application_id: str,
    dry_run: bool,
) -> Any:
    """Build an ApplicationPackage and run the real M4 ApplicationExecutor."""
    from app.browser_automation.services.executor import ApplicationExecutor
    from app.browser_automation.services.models import ApplicationPackage
    from app.browser_automation.llm import telemetry as tele

    tele.reset_session()

    work_auth_type = profile.get("work_authorization_type", "US Citizen")
    package = ApplicationPackage(
        application_id=application_id,
        candidate_id=candidate_id,
        job_id=job_dict.get("job_id") or str(uuid4()),
        job_title=job_dict.get("title", ""),
        job_description=job_dict.get("description", ""),
        job_url=job_dict["job_url"],
        platform=job_dict["platform"],
        ats_type=job_dict["platform"],
        company=job_dict.get("company", ""),
        resume_url=resume_url,
        cover_letter_url=cover_letter_url,
        candidate_profile=profile,
        screening_answers={
            "Work Authorization": work_auth_type,
            "Are you legally authorized to work in the United States?": profile.get("work_authorization", "Yes"),
            "Will you now or in the future require sponsorship for employment visa status?": profile.get("sponsorship", "No"),
        },
    )

    mode_label = "DRY RUN (no submit)" if dry_run else "*** REAL SUBMIT ***"
    logger.info(SECTION_SEP)
    logger.info(f"  M4 BROWSER EXECUTOR — {mode_label}")
    logger.info(f"  application_id : {application_id}")
    logger.info(f"  candidate      : {profile['name']} ({candidate_id})")
    logger.info(f"  platform       : {package.platform}")
    logger.info(f"  job_url        : {package.job_url}")
    logger.info(f"  resume_url     : {resume_url}")
    logger.info(f"  cover_letter   : {cover_letter_url or '(none)'}")
    logger.info(SECTION_SEP)

    executor = ApplicationExecutor()
    result = await executor.execute(package)

    session = tele.get_session()
    tele.log_summary("[Tokens] M4 FINAL")

    return result, session


# ── M1 DB update ──────────────────────────────────────────────────────────────

async def update_application_status_in_db(
    application_id: str,
    new_status: str,
    screenshot_url: Optional[str] = None,
    error_message: Optional[str] = None,
    failure_reason: Optional[str] = None,
) -> None:
    """Persist the final M4 result back to the application record in DB."""
    import httpx
    from datetime import datetime, timezone

    payload: Dict[str, Any] = {"status": new_status}
    if screenshot_url:
        payload["screenshot_url"] = screenshot_url
    if error_message:
        payload["error_message"] = error_message
    if failure_reason:
        payload["failure_reason"] = failure_reason
    if new_status == "SUBMITTED":
        payload["submitted_at"] = datetime.now(timezone.utc).isoformat()

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.patch(
                f"{API_BASE}/applications/{application_id}/status",
                json=payload,
            )
            if r.status_code in (200, 204):
                ok(f"DB application status updated → {new_status}")
            else:
                warn(f"DB status update returned {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        warn(f"Could not update DB application status: {exc}")


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Full M1→M2→M3→M4 end-to-end pipeline test (REAL submit)"
    )
    parser.add_argument("url", nargs="?", default=None, help="Explicit job URL (optional)")
    parser.add_argument("--job-id", default=os.getenv("TEST_JOB"), help="Job UUID from DB")
    parser.add_argument("--platform", default=None,
                        help="Auto-pick newest job of this platform (greenhouse / remoterocketship / lever / ashby / dice)")
    parser.add_argument("--candidate",
                        default=os.getenv("TEST_CANDIDATE") or os.getenv("TEST_CANDIDATE_ID") or "sabih",
                        help="Candidate name / email substring / UUID (default: sabih)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Stop before real browser submit (safe mode for M3 debugging)")
    parser.add_argument("--no-headless", action="store_true",
                        help="Show browser window during automation")
    parser.add_argument("--enforce-gate", action="store_true",
                        help="Enforce the M3 fit-score gate (≥70). By default the E2E test bypasses it to always exercise M4.")
    args = parser.parse_args()

    # Set env flags consumed by lower-level modules
    os.environ["DRY_RUN_NO_SUBMIT"] = "true" if args.dry_run else "false"
    os.environ.setdefault("PLAYWRIGHT_HEADLESS", "false" if args.no_headless else "true")

    evidence: Dict[str, Any] = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "candidate": None,
        "job": None,
        "m3_result": None,
        "m4_result": None,
        "final_status": None,
        "errors": [],
        "timings": {},
    }

    start_total = time.perf_counter()

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 0 — Resolve candidate
    # ─────────────────────────────────────────────────────────────────────────
    banner("STAGE 0 — M1: Resolve Candidate")
    t0 = time.perf_counter()
    try:
        profile = await resolve_candidate(args.candidate)
    except Exception as exc:
        fail(f"Could not resolve candidate: {exc}")
        evidence["errors"].append({"stage": "candidate_resolve", "error": str(exc)})
        _write_evidence(evidence)
        return 1
    candidate_id = profile.pop("_candidate_id")
    evidence["candidate"] = {"id": candidate_id, "name": profile["name"], "email": profile["email"]}
    evidence["timings"]["candidate_resolve_s"] = round(time.perf_counter() - t0, 2)

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 1 — Resolve job
    # ─────────────────────────────────────────────────────────────────────────
    banner("STAGE 1 — M1/M2: Resolve Job")
    t0 = time.perf_counter()
    try:
        job = await resolve_job(args)
    except Exception as exc:
        fail(f"Could not resolve job: {exc}")
        evidence["errors"].append({"stage": "job_resolve", "error": str(exc)})
        _write_evidence(evidence)
        return 1
    evidence["job"] = {
        "id": job.get("job_id"),
        "title": job.get("title"),
        "company": job.get("company"),
        "platform": job.get("platform"),
        "url": job.get("job_url"),
    }
    evidence["timings"]["job_resolve_s"] = round(time.perf_counter() - t0, 2)

    logger.info(f"  JOB  : {job.get('title')!r} @ {job.get('company')!r}")
    logger.info(f"  URL  : {job.get('job_url')}")
    logger.info(f"  SRC  : {job.get('platform')}")

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 2 — Fetch base resume from Supabase (via DB)
    # ─────────────────────────────────────────────────────────────────────────
    banner("STAGE 2 — M3: Fetch Base Resume from Supabase/DB")
    t0 = time.perf_counter()
    base_resume = await resolve_base_resume(candidate_id)
    evidence["timings"]["base_resume_fetch_s"] = round(time.perf_counter() - t0, 2)

    if not base_resume:
        fail("No base resume found in DB for this candidate.")
        fail("Please upload a base resume via the API or dashboard before running this test.")
        evidence["errors"].append({"stage": "base_resume", "error": "no_base_resume"})
        _write_evidence(evidence)
        return 1

    ok(f"Base resume found: id={base_resume['id']}  url={base_resume['file_url'][:60]}...")
    evidence["base_resume_id"] = base_resume["id"]
    evidence["base_resume_url"] = base_resume["file_url"]

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 3 — M3: Score → Tailor → Cover Letter → Upload to Supabase → Save DB
    # ─────────────────────────────────────────────────────────────────────────
    banner("STAGE 3 — M3: Score / Tailor Resume / Cover Letter / Save to Supabase")

    resume_url: Optional[str] = None
    cover_letter_url: Optional[str] = None
    application_id: Optional[str] = None
    m3_status: Optional[str] = None

    if job.get("job_id"):
        # We have a real DB job → run the full M3 orchestrator
        t0 = time.perf_counter()
        try:
            m3_result = await run_m3_pipeline(
                candidate_id=candidate_id,
                job_dict=job,
                base_resume=base_resume,
                api_base_url=API_BASE_URL,
            )
            m3_status = m3_result.get("status") or ("QUEUED" if m3_result.get("should_apply") else "ANALYZED")
            evidence["m3_result"] = {
                "status": m3_status,
                "should_apply": m3_result.get("should_apply"),
                "resume_pdf_url": m3_result.get("resume_pdf_url"),
                "cover_letter_url": m3_result.get("cover_letter_pdf_url"),
                "application_id": m3_result.get("application_id"),
            }
            evidence["timings"]["m3_pipeline_s"] = round(time.perf_counter() - t0, 2)

            if not m3_result.get("should_apply") and args.enforce_gate:
                fail(f"M3 gate: LLM fit score below threshold. Status={m3_status}")
                fail("Remove --enforce-gate to bypass the gate and continue to M4.")
                evidence["final_status"] = "M3_GATE_REJECTED"
                _write_evidence(evidence)
                return 2

            elif not m3_result.get("should_apply"):
                warn(f"M3 gate: fit score below threshold (Status={m3_status}) — bypassing gate to continue E2E test through M4.")

            # --- Extract URLs and app ID safely ---
            # When gate rejects, should_apply=False and the orchestrator returns:
            #   {"should_apply": False, "reason": "..."}
            # When gate passes, it returns:
            #   {"should_apply": True, "resume_pdf_url": ..., "cover_letter_pdf_url": ..., "application_id": ...}
            # Wait — prepare_package_for_live_application does NOT return application_id directly.
            # We need to fetch it from DB by candidate+job pair.
            _raw_resume = m3_result.get("resume_pdf_url")
            _raw_cl = m3_result.get("cover_letter_pdf_url")

            # Sanitize — never allow the string "None"
            resume_url = _raw_resume if (_raw_resume and str(_raw_resume).startswith("http")) else None
            cover_letter_url = _raw_cl if (_raw_cl and str(_raw_cl).startswith("http")) else None

            # Always fall back to the base resume if no tailored resume was produced
            if not resume_url:
                resume_url = base_resume.get("file_url") or ""
                warn(f"M3 did not produce tailored resume (gate rejected / error). Using base resume: {resume_url[:80]}")

            # Resolve the application_id created by the orchestrator via DB
            application_id = ""
            if not m3_result.get("application_id"):
                # orchestrator created the record but doesn't return it in the gate-reject path
                # fetch it from DB
                try:
                    from sqlalchemy import select
                    from app.database import AsyncSessionLocal
                    from app.models.application import Application
                    from uuid import UUID as _UUID
                    async with AsyncSessionLocal() as _s:
                        _app = (
                            await _s.execute(
                                select(Application)
                                .where(
                                    Application.candidate_id == _UUID(candidate_id),
                                    Application.job_id == _UUID(job["job_id"]),
                                )
                            )
                        ).scalar_one_or_none()
                        if _app:
                            application_id = str(_app.id)
                except Exception as _e:
                    warn(f"Could not look up application_id from DB: {_e}")
            else:
                application_id = str(m3_result.get("application_id"))

            if not application_id:
                application_id = str(uuid4())
                warn(f"No application_id found — using ephemeral ID: {application_id}")

            ok(f"M3 complete → status={m3_status}")
            ok(f"Resume URL     : {resume_url}")
            ok(f"Cover Letter   : {cover_letter_url or '(none)'}")
            ok(f"Application ID : {application_id}")

        except Exception as exc:
            fail(f"M3 pipeline error: {exc}")
            traceback.print_exc()
            evidence["errors"].append({"stage": "m3_pipeline", "error": str(exc), "traceback": traceback.format_exc()})
            evidence["timings"]["m3_pipeline_s"] = round(time.perf_counter() - t0, 2)
            _write_evidence(evidence)
            return 1
    else:
        # URL-only flow (no DB job) — skip M3, pull existing resume/CL from DB
        warn("No DB job_id available — skipping M3 pipeline. Fetching existing resume/CL from DB.")
        from sqlalchemy import select
        from app.database import AsyncSessionLocal
        from app.models.resume import Resume
        from app.models.application import Application

        async with AsyncSessionLocal() as s:
            r = (
                await s.execute(
                    select(Resume)
                    .where(Resume.candidate_id == candidate_id)
                    .order_by(Resume.is_base.asc(), Resume.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if r and r.file_url and r.file_url.startswith("http"):
                resume_url = r.file_url

            a = (
                await s.execute(
                    select(Application)
                    .where(
                        Application.candidate_id == candidate_id,
                        Application.cover_letter_url.isnot(None),
                    )
                    .order_by(Application.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if a and a.cover_letter_url and a.cover_letter_url.startswith("http"):
                cover_letter_url = a.cover_letter_url

        application_id = str(uuid4())
        ok(f"Resume URL   : {resume_url}")
        ok(f"Cover Letter : {cover_letter_url or '(none)'}")

    if not resume_url:
        fail("No resume URL available — cannot proceed to M4.")
        evidence["errors"].append({"stage": "resume_url", "error": "no_resume_url"})
        _write_evidence(evidence)
        return 1

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 4 — M4: Browser automation — REAL SUBMIT
    # ─────────────────────────────────────────────────────────────────────────
    banner(f"STAGE 4 — M4: Browser Automation ({'DRY RUN' if args.dry_run else 'REAL SUBMIT'})")

    if not application_id:
        application_id = str(uuid4())

    t0 = time.perf_counter()
    try:
        result, token_session = await run_m4_executor(
            candidate_id=candidate_id,
            job_dict=job,
            profile=profile,
            resume_url=resume_url,
            cover_letter_url=cover_letter_url,
            application_id=application_id,
            dry_run=args.dry_run,
        )
        evidence["timings"]["m4_executor_s"] = round(time.perf_counter() - t0, 2)
    except Exception as exc:
        fail(f"M4 executor raised an exception: {exc}")
        traceback.print_exc()
        evidence["errors"].append({"stage": "m4_executor", "error": str(exc), "traceback": traceback.format_exc()})
        evidence["timings"]["m4_executor_s"] = round(time.perf_counter() - t0, 2)
        evidence["final_status"] = "M4_EXCEPTION"
        _write_evidence(evidence)
        return 1

    m4_status = result.status
    evidence["m4_result"] = {
        "status": m4_status,
        "confirmation_text": result.confirmation_text,
        "error_message": result.error_message,
        "screenshot_url": result.screenshot_url,
        "elapsed_seconds": round(result.execution_time_seconds, 2),
        "tokens": {
            "calls": len(token_session.calls),
            "input": token_session.total_input,
            "output": token_session.total_output,
            "total": token_session.total,
        },
    }

    # ─────────────────────────────────────────────────────────────────────────
    # STAGE 5 — M1: Persist final status back to DB
    # ─────────────────────────────────────────────────────────────────────────
    banner("STAGE 5 — M1: Persist Final Status to DB")

    final_status = m4_status
    if job.get("job_id") and application_id:
        await update_application_status_in_db(
            application_id=application_id,
            new_status=final_status,
            screenshot_url=result.screenshot_url,
            error_message=result.error_message,
            failure_reason=_classify_failure(result.error_message or "", final_status),
        )

    evidence["final_status"] = final_status
    evidence["timings"]["total_s"] = round(time.perf_counter() - start_total, 2)

    _write_evidence(evidence)

    # ─────────────────────────────────────────────────────────────────────────
    # SUMMARY
    # ─────────────────────────────────────────────────────────────────────────
    banner("E2E PIPELINE SUMMARY")
    logger.info(f"  Candidate      : {profile['name']} ({candidate_id})")
    logger.info(f"  Job            : {job.get('title')!r} @ {job.get('company')!r}")
    logger.info(f"  Platform       : {job.get('platform')}")
    logger.info(f"  URL            : {job.get('job_url')}")
    logger.info(f"  Resume URL     : {resume_url}")
    logger.info(f"  Cover Letter   : {cover_letter_url or '(none)'}")
    logger.info(f"  Application ID : {application_id}")
    logger.info(f"  M3 Status      : {m3_status or 'skipped (URL-only)'}")
    logger.info(f"  M4 Status      : {m4_status}")
    logger.info(f"  Confirmation   : {result.confirmation_text or '—'}")
    logger.info(f"  Screenshot     : {result.screenshot_url or '—'}")
    logger.info(f"  Error          : {result.error_message or '—'}")
    logger.info(f"  Elapsed        : {evidence['timings']['total_s']}s")
    logger.info(f"  Evidence JSON  : {PROJECT_ROOT / 'test_e2e_evidence.json'}")
    logger.info(SECTION_SEP)

    if evidence["errors"]:
        logger.warning(f"  NON-FATAL ERRORS ({len(evidence['errors'])}):")
        for e in evidence["errors"]:
            logger.warning(f"    [{e['stage']}] {e['error']}")

    success = final_status in ("SUBMITTED", "FORM_COMPLETED")
    if success:
        ok("Pipeline completed SUCCESSFULLY — application submitted!")
    else:
        fail(f"Pipeline ended with status: {final_status}")

    return 0 if success else 1


# ── helpers ───────────────────────────────────────────────────────────────────

def _classify_failure(error_msg: str, status: str) -> Optional[str]:
    """Map an error message / status to a canonical failure_reason code."""
    if status == "BLOCKED" or "blocked" in error_msg.lower() or "bot" in error_msg.lower():
        return "BOT_DETECTED"
    if "captcha" in error_msg.lower():
        return "BOT_DETECTED"
    if "qualification" in error_msg.lower() or "mismatch" in error_msg.lower():
        return "QUALIFICATION_MISMATCH"
    if "form fill incomplete" in error_msg.lower() or "required field" in error_msg.lower():
        return "FORM_INCOMPLETE"
    if status in ("FAILED", "CAPTCHA_FAILED"):
        return "INFRA_ERROR"
    return None


def _write_evidence(evidence: Dict[str, Any]) -> None:
    out_path = PROJECT_ROOT / "test_e2e_evidence.json"
    out_path.write_text(json.dumps(evidence, indent=2, default=str))
    logger.info(f"  Evidence written → {out_path}")


# ── entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
