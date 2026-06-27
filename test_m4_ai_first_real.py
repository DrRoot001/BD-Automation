#!/usr/bin/env python3
"""Module 4 — AI-First Real-World Test (platform-agnostic, production pipeline)
==============================================================================

Runs the SAME production path as Dice for ANY job in the database — Greenhouse,
Remote Rocketship, Lever, Ashby, iCIMS, Dice, etc. It is fully DB-driven:

    pick a candidate + a job from the DB
        │
    resolve resume + cover letter URLs from Supabase (resumes / applications)
        │
    build an ApplicationPackage (platform = jobs.source)
        │
    ApplicationExecutor().execute(package)   ← the real production executor
        │
    routes by platform → correct adapter + hints → AgentLoop fills + (dry) submits

This replaces the old Greenhouse-hardcoded flow so a real Greenhouse / Remote
Rocketship job from the pipeline is driven exactly like the Dice scenario.

Usage:
    # by candidate + job UUID (preferred — pulls the real job URL from DB)
    TEST_CANDIDATE=sabih python test_m4_ai_first_real.py --job-id <job_uuid>

    # by an explicit job URL (platform auto-detected from the host)
    TEST_CANDIDATE=sabih python test_m4_ai_first_real.py https://job-boards.greenhouse.io/<co>/jobs/<id>

    # auto-pick the newest job of a platform from the DB
    TEST_CANDIDATE=sabih python test_m4_ai_first_real.py --platform greenhouse
    TEST_CANDIDATE=sabih python test_m4_ai_first_real.py --platform remoterocketship

    # REAL submit (default is a safe dry run that stops before submit)
    ... --submit

Outputs (project root): test_m4_evidence_result.json (status, screenshot, tokens).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / "backend" / ".env")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s")
logger = logging.getLogger("m4-ai-first-real")
for n in ("httpx", "httpcore", "anthropic", "urllib3"):
    logging.getLogger(n).setLevel(logging.WARNING)

# Fallback job if neither --job-id, --platform, nor a URL arg is given.
DEFAULT_JOB_URL = "https://job-boards.greenhouse.io/vercel/jobs/5999792004"

# DB work_auth code → dropdown category (mirrors tasks/browser_automation.py)
_WORK_AUTH_TYPE_MAP = {
    "citizen": "US Citizen", "us_authorized": "US Citizen",
    "green_card": "Green Card Holder", "visa": "H1B", "ead": "OPT",
}


async def resolve_candidate(query: str) -> dict:
    """Look up a candidate by name/email substring or UUID; return a profile dict."""
    from sqlalchemy import select, or_
    from app.database import AsyncSessionLocal
    from app.models.candidate import Candidate

    q = (query or "").strip()
    if not q:
        raise RuntimeError("TEST_CANDIDATE not set — pass a name/email substring or UUID.")
    is_uuid = bool(re.match(r"^[0-9a-fA-F-]{32,36}$", q))
    async with AsyncSessionLocal() as s:
        conds = [Candidate.name.ilike(f"%{q}%"), Candidate.email.ilike(f"%{q}%")]
        if is_uuid:
            conds.append(Candidate.id == q)
        cand = (await s.execute(select(Candidate).where(or_(*conds)).limit(1))).scalar_one_or_none()
    if not cand:
        raise RuntimeError(f"No candidate matching {q!r}")

    work_auth = (getattr(cand, "work_auth", None) or "us_authorized").lower()
    is_auth = work_auth in ("us_authorized", "citizen", "green_card", "visa", "ead")
    full = (cand.name or "").strip()
    parts = full.split()
    profile = {
        "name": full,
        "first_name": parts[0] if parts else "",
        "last_name": " ".join(parts[1:]) if len(parts) > 1 else "",
        "email": cand.email or "",
        "phone": cand.phone or "",
        "location": cand.location or "",
        "linkedin_url": getattr(cand, "linkedin_url", "") or "",
        "website": getattr(cand, "website", "") or "",
        "experience_years": str(getattr(cand, "years_exp", "") or ""),
        "work_authorization": "Yes" if is_auth else "No",
        "work_authorization_type": _WORK_AUTH_TYPE_MAP.get(work_auth, "Other"),
        "sponsorship": "No" if is_auth else "Yes",
    }
    # Test-only phone override. Some candidates have invalid phone data (e.g.
    # an unassigned NANP area code) that real ATS phone widgets reject. This
    # lets us validate the pipeline end-to-end with a valid number without
    # mutating production data. Real candidates supply their own valid phone.
    _test_phone = os.getenv("TEST_PHONE", "").strip()
    if _test_phone:
        profile["phone"] = _test_phone
        logger.info(f"[test] phone overridden via TEST_PHONE → {_test_phone!r}")

    # Test-only full address. Some candidates store only a country code as their
    # location (e.g. "US"), which can't satisfy ATS steps that require a full
    # street/city/state/ZIP. TEST_LOCATION lets us validate those steps without
    # mutating production data. Real candidates supply their own address.
    _test_location = os.getenv("TEST_LOCATION", "").strip()
    if _test_location:
        profile["location"] = _test_location
        logger.info(f"[test] location overridden via TEST_LOCATION → {_test_location!r}")

    profile["_candidate_id"] = str(cand.id)
    logger.info(f"Candidate {full!r} id={cand.id} email={cand.email!r} work_auth={work_auth}")
    return profile


async def resolve_job(args) -> dict:
    """Return {job_url, platform, title, company, description} from --job-id /
    --platform / a URL arg / the default."""
    from sqlalchemy import select, text
    from app.database import AsyncSessionLocal
    from app.models.job import Job

    async def _from_row(j) -> dict:
        return {
            "job_url": j.source_url, "platform": j.source or j.source_url,
            "title": j.title or "", "company": j.company or "",
            "description": j.description or "",
        }

    if args.job_id:
        async with AsyncSessionLocal() as s:
            j = (await s.execute(select(Job).where(Job.id == args.job_id))).scalar_one_or_none()
        if not j:
            raise RuntimeError(f"No job with id={args.job_id}")
        return await _from_row(j)

    if args.platform:
        # Newest job whose source matches the platform substring.
        async with AsyncSessionLocal() as s:
            j = (await s.execute(
                select(Job).where(Job.source.ilike(f"%{args.platform}%"))
                .order_by(Job.created_at.desc()).limit(1)
            )).scalar_one_or_none()
        if not j:
            raise RuntimeError(f"No job in DB with source matching {args.platform!r}")
        logger.info(f"Auto-picked newest {args.platform!r} job: {j.source_url}")
        return await _from_row(j)

    if args.url:
        # Platform auto-detected from the host by the executor's get_adapter.
        return {"job_url": args.url, "platform": args.url, "title": "", "company": "", "description": ""}

    logger.warning("No job specified — using DEFAULT_JOB_URL")
    return {"job_url": DEFAULT_JOB_URL, "platform": DEFAULT_JOB_URL, "title": "", "company": "", "description": ""}


async def resolve_files(candidate_id: str) -> tuple[str | None, str | None]:
    """Latest resume URL (tailored→base) + latest cover-letter URL from DB."""
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.resume import Resume
    from app.models.application import Application

    resume_url = cover_letter_url = None
    async with AsyncSessionLocal() as s:
        r = (await s.execute(
            select(Resume).where(Resume.candidate_id == candidate_id)
            .order_by(Resume.is_base.asc(), Resume.version.desc()).limit(1)
        )).scalar_one_or_none()
        if r and r.file_url and r.file_url.startswith("http"):
            resume_url = r.file_url
        a = (await s.execute(
            select(Application).where(Application.candidate_id == candidate_id,
                                      Application.cover_letter_url.isnot(None))
            .order_by(Application.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if a and a.cover_letter_url and a.cover_letter_url.startswith("http"):
            cover_letter_url = a.cover_letter_url
    logger.info(f"Resume URL: {resume_url}")
    logger.info(f"Cover letter URL: {cover_letter_url}")
    return resume_url, cover_letter_url


def _address_screening_answers() -> dict:
    """Parse TEST_LOCATION ("street, city, state zip") into per-field answers so
    ATS address steps (address/city/state/postal) get exact values. Test-only."""
    loc = os.getenv("TEST_LOCATION", "").strip()
    if not loc:
        return {}
    out: dict = {"Country": "United States", "country": "United States"}
    parts = [p.strip() for p in loc.split(",")]
    if len(parts) >= 1:
        out["Address"] = out["address"] = out["Street address"] = parts[0]
    if len(parts) >= 2:
        out["City"] = out["city"] = parts[1]
    if len(parts) >= 3:
        m = re.match(r"([A-Za-z ]+)\s*(\d{5})?", parts[2])
        if m:
            if m.group(1):
                out["State"] = out["state"] = m.group(1).strip()
            if m.group(2):
                out["Postal code"] = out["postal"] = out["Zip"] = out["ZIP code"] = m.group(2)
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description="M4 AI-first real test (any platform)")
    parser.add_argument("url", nargs="?", default=None, help="Explicit job URL (optional)")
    parser.add_argument("--job-id", default=os.getenv("TEST_JOB"), help="Job UUID from the DB")
    parser.add_argument("--platform", default=None,
                        help="Auto-pick newest job of this platform (greenhouse / remoterocketship / lever ...)")
    parser.add_argument("--candidate", default=os.getenv("TEST_CANDIDATE") or os.getenv("TEST_CANDIDATE_ID") or "sabih")
    parser.add_argument("--submit", action="store_true", help="Actually submit (default: dry run)")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    os.environ["DRY_RUN_NO_SUBMIT"] = "false" if args.submit else "true"
    os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true" if args.headless else "false")

    from uuid import uuid4
    from app.browser_automation.services.executor import ApplicationExecutor
    from app.browser_automation.services.models import ApplicationPackage
    from app.browser_automation.llm import telemetry as tele
    tele.reset_session()

    profile = await resolve_candidate(args.candidate)
    candidate_id = profile.pop("_candidate_id")
    job = await resolve_job(args)
    resume_url, cover_letter_url = await resolve_files(candidate_id)
    if not resume_url:
        logger.error("No resume URL found in Supabase/DB for this candidate — aborting")
        return 2

    package = ApplicationPackage(
        application_id=str(uuid4()),
        candidate_id=candidate_id,
        job_id=str(uuid4()),
        job_title=job["title"], job_description=job["description"],
        job_url=job["job_url"], platform=job["platform"], ats_type=job["platform"],
        company=job["company"],
        resume_url=resume_url, cover_letter_url=cover_letter_url,
        candidate_profile=profile,
        # Candidate is US-authorized; pin work auth so the AI answers verbatim.
        screening_answers={
            "Work Authorization": profile.get("work_authorization_type", "US Citizen"),
            "Are you legally authorized to work in the United States?": "Yes",
            "Will you now or in the future require sponsorship for employment visa status?": "No",
            **_address_screening_answers(),
        },
    )

    mode = "REAL SUBMIT" if args.submit else "DRY RUN (no submit)"
    print("=" * 70)
    print(f"  M4 AI-FIRST TEST — {mode}")
    print(f"  candidate : {profile['name']} ({candidate_id})")
    print(f"  platform  : {package.platform}")
    print(f"  job_url   : {package.job_url}")
    print(f"  resume    : {resume_url}")
    print(f"  cover_ltr : {cover_letter_url or '(none)'}")
    print("=" * 70)

    result = await ApplicationExecutor().execute(package)

    session = tele.get_session()
    tele.log_summary("[Tokens] FINAL")
    evidence = {
        "platform": package.platform, "job_url": package.job_url,
        "candidate": profile["name"], "status": result.status,
        "confirmation": result.confirmation_text, "error": result.error_message,
        "screenshot": result.screenshot_url,
        "elapsed_seconds": round(result.execution_time_seconds, 2),
        "tokens": {"calls": len(session.calls), "input": session.total_input,
                   "output": session.total_output, "total": session.total},
    }
    (PROJECT_ROOT / "test_m4_evidence_result.json").write_text(json.dumps(evidence, indent=2))

    print("\n" + "=" * 70)
    print(f"  RESULT: status={result.status}  confirmation={result.confirmation_text}")
    print(f"  error={result.error_message}")
    print(f"  screenshot={result.screenshot_url}")
    print("=" * 70)
    return 0 if result.status in ("SUBMITTED", "FORM_COMPLETED") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
