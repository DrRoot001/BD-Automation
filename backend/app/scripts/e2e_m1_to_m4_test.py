"""
End-to-End pipeline test: Module 1 → Module 2 (data in DB) → Module 3 (tailored assets) → Module 4 (browser).

Uses:
  Candidate : Harmain Ali Butt   (9f1d8c11-9a7c-48a0-ba09-5a8222a00cba)
  Job       : Lead Software Eng @ Vercel/Monks  (09c52692-5c97-46b8-94a3-e8e3ddaf61e1)
  URL       : https://boards.greenhouse.io/monks/jobs/5996484004  (Greenhouse hosted board)
  Resume    : backend/data/tailored_resumes/tailored_9f1d8c11..._v3.pdf  (M3 output, already on disk)
  Cover Ltr : backend/data/cover_letters/cover_letter_9f1d8c11....pdf     (M3 output, already on disk)

DRY_RUN_NO_SUBMIT=true  →  fills every field + uploads both PDFs, captures screenshot, skips submit click.
Set DRY_RUN_NO_SUBMIT=false to do a real live submit.
"""

import asyncio
import os
import sys
import json
import logging
from pathlib import Path

# ── Path setup ───────────────────────────────────────────────────────────────
HERE       = Path(__file__).resolve().parent
BACKEND    = HERE.parent.parent          # backend/
ROOT       = BACKEND.parent             # project root

for p in [str(BACKEND), str(ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

# ── Env ──────────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

# Force dry-run so we never accidentally submit a live app during testing
os.environ.setdefault("DRY_RUN_NO_SUBMIT", "true")
os.environ.setdefault("M1_API_BASE_URL",   "http://127.0.0.1:8000/api")

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_PATH = str(HERE / "e2e_test_run.log")
fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s  %(message)s")
fh = logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(fmt)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(fmt)
root_log = logging.getLogger()
root_log.setLevel(logging.DEBUG)
root_log.addHandler(fh)
root_log.addHandler(ch)
logger = logging.getLogger("e2e_test")

# ── Test constants ────────────────────────────────────────────────────────────
CANDIDATE_ID = "9f1d8c11-9a7c-48a0-ba09-5a8222a00cba"
JOB_ID       = "09c52692-5c97-46b8-94a3-e8e3ddaf61e1"
# Use the direct Greenhouse-hosted boards URL.
# This hits boards.greenhouse.io (Greenhouse's own servers, no monks.com WAF).
# monks.com/careers embeds this same form in an iframe — the form data is identical.
JOB_URL      = "https://boards.greenhouse.io/monks/jobs/5996484004"
PLATFORM     = "greenhouse"

RESUME_PATH  = str(BACKEND / "data" / "tailored_resumes" /
               "tailored_9f1d8c11-9a7c-48a0-ba09-5a8222a00cba_09c52692-5c97-46b8-94a3-e8e3ddaf61e1_v3.pdf")
COVER_PATH   = str(BACKEND / "data" / "cover_letters" /
               "cover_letter_9f1d8c11-9a7c-48a0-ba09-5a8222a00cba_09c52692-5c97-46b8-94a3-e8e3ddaf61e1.pdf")

SECTION = lambda t: logger.info("\n" + "="*60 + f"\n  {t}\n" + "="*60)


async def run():
    import httpx
    api = os.environ["M1_API_BASE_URL"]

    # ── 1. Verify files exist ─────────────────────────────────────────────────
    SECTION("PRE-FLIGHT: checking assets")
    for label, path in [("Resume", RESUME_PATH), ("Cover Letter", COVER_PATH)]:
        exists = os.path.isfile(path)
        size   = os.path.getsize(path) if exists else 0
        logger.info(f"  {label}: {'✅' if exists else '❌'} {'EXISTS' if exists else 'MISSING'} "
                    f"({size:,} bytes) → {path}")
        if not exists:
            logger.error(f"ABORT: {label} PDF not found at {path}")
            return

    # ── 2. Module 1 checks via API ────────────────────────────────────────────
    SECTION("MODULE 1: API health + data fetch")
    async with httpx.AsyncClient(timeout=15) as client:
        h = await client.get(f"{api.replace('/api','')}/api/health")
        logger.info(f"  API health: {h.status_code} → {h.text}")
        assert h.status_code == 200, f"API health failed: {h.text}"

        cr = await client.get(f"{api}/candidates/{CANDIDATE_ID}")
        assert cr.status_code == 200, f"Candidate fetch failed: {cr.text}"
        cand = cr.json()
        logger.info(f"  Candidate: {cand['name']} | work_auth={cand['work_auth']} | "
                    f"years_exp={cand['years_exp']} | tech_stack={cand['tech_stack'][:3]}...")

        jr = await client.get(f"{api}/jobs/{JOB_ID}")
        assert jr.status_code == 200, f"Job fetch failed: {jr.text}"
        job = jr.json()
        logger.info(f"  Job: {job['title']} @ {job['company']} | source={job['source']} | "
                    f"url={job['source_url'][:60]}")

        rr = await client.get(f"{api}/resumes/{CANDIDATE_ID}")
        resumes = rr.json() if rr.status_code == 200 else []
        logger.info(f"  Resumes in DB: {len(resumes)} (base + tailored)")
        for r in resumes:
            logger.info(f"    v{r.get('version')} | is_base={r.get('is_base')} | "
                        f"tailored_for={r.get('tailored_for_job_id')} | url={r.get('file_url','')[:60]}")

    # ── 3. Create / reset application ─────────────────────────────────────────
    SECTION("MODULE 1 → STATE: create/reset application")
    import asyncpg
    from urllib.parse import urlparse

    raw_db_url = os.getenv("DATABASE_URL", "")
    # Convert asyncpg:// or postgresql+asyncpg:// to plain postgresql:// for asyncpg.connect
    pg_url = raw_db_url.replace("postgresql+asyncpg://", "postgresql://")

    # Use asyncpg directly to bypass the API state-machine for the reset.
    # The API only allows forward transitions; we need to force-reset to QUEUED for testing.
    pg_conn = await asyncpg.connect(pg_url)
    try:
        row = await pg_conn.fetchrow(
            "SELECT id, status FROM applications WHERE candidate_id=$1 AND job_id=$2",
            CANDIDATE_ID, JOB_ID
        )
        if row:
            app_id     = str(row["id"])
            old_status = row["status"]
            logger.info(f"  Found existing application {app_id} (status={old_status})")
            await pg_conn.execute(
                """UPDATE applications
                   SET status='QUEUED', error_message=NULL,
                       submitted_at=NULL, screenshot_url=NULL,
                       cover_letter_url=NULL, resume_id=NULL
                   WHERE id=$1""",
                row["id"]
            )
            await pg_conn.execute(
                "DELETE FROM application_history WHERE application_id=$1",
                row["id"]
            )
            logger.info(f"  Force-reset status to QUEUED via direct DB (bypassing state machine)")
        else:
            # Create via API
            async with httpx.AsyncClient(timeout=15) as client:
                create = await client.post(f"{api}/applications",
                                           json={"candidate_id": CANDIDATE_ID,
                                                 "job_id": JOB_ID, "status": "QUEUED"})
                assert create.status_code in (200, 201), f"Create application failed: {create.text}"
                app_data = create.json()
                app_id   = str(app_data["id"])
                logger.info(f"  Created new application {app_id}")
    finally:
        await pg_conn.close()

    logger.info(f"  Application ID: {app_id}")

    # ── 4. Build ApplicationPackage (same logic as hydrate_and_execute) ───────
    SECTION("MODULE 1→4 HANDOFF: build ApplicationPackage")
    from app.browser_automation.services.models import ApplicationPackage

    full_name  = cand["name"]
    parts      = full_name.split()
    first_name = parts[0] if parts else ""
    last_name  = " ".join(parts[1:]) if len(parts) > 1 else ""
    work_auth  = (cand.get("work_auth") or "us_authorized").lower()
    is_auth    = work_auth in ("us_authorized", "citizen", "green_card", "visa", "ead", "authorized")

    candidate_profile = {
        "name":               full_name,
        "first_name":         first_name,
        "last_name":          last_name,
        "email":              cand["email"],
        "phone":              cand.get("phone") or "",
        "location":           cand.get("location") or "Remote",
        "linkedin_url":       cand.get("linkedin_url") or "",
        "website":            cand.get("website") or "",
        "experience_years":   str(cand.get("years_exp") or ""),
        "tech_stack":         ", ".join(cand.get("tech_stack") or []),
        "current_company":    cand.get("current_company") or "",
        "current_title":      cand.get("current_title") or "",
        "education":          cand.get("education") or "",
        "salary_expectation": cand.get("salary_expectation") or "",
        "work_authorization": "Yes" if is_auth else "No",
        "sponsorship":        "No"  if is_auth else "Yes",
        "agree_terms":        "Yes",
        "willing_to_relocate":"Yes",
        "background_check":   "Yes",
        "drug_test":          "Yes",
        "referral_source":    "Online",
        "start_date":         "Immediately",
        # Demographic fields — EEO / diversity questions in Greenhouse forms
        "gender":             cand.get("gender") or "Decline To Self Identify",
        "race_ethnicity":     cand.get("race_ethnicity") or "Decline To Self Identify",
        "veteran_status":     cand.get("veteran_status") or "I am not a protected veteran",
        "disability_status":  cand.get("disability_status") or "I don't wish to answer",
        # Location-match screening (e.g., "Do you live in Santiago?")
        # Set to "Yes" only if the candidate is located in the job's target city.
        "location_match":     "No",
    }

    package = ApplicationPackage(
        application_id=str(app_id),
        candidate_id=CANDIDATE_ID,
        job_id=JOB_ID,
        job_url=JOB_URL,
        platform=PLATFORM,
        resume_url=RESUME_PATH,
        cover_letter_url=COVER_PATH,
        candidate_profile=candidate_profile,
        screening_answers={},
    )

    logger.info(f"  Package built for candidate {full_name}")
    logger.info(f"  resume_url      → {package.resume_url}")
    logger.info(f"  cover_letter_url → {package.cover_letter_url}")
    logger.info(f"  candidate_profile keys: {list(candidate_profile)}")

    # ── 5. Run Module 4 executor ──────────────────────────────────────────────
    SECTION(f"MODULE 4: Playwright browser automation (DRY_RUN={os.getenv('DRY_RUN_NO_SUBMIT')})")
    from app.browser_automation.services.executor import ApplicationExecutor

    executor = ApplicationExecutor()
    logger.info("  Launching Playwright → Chromium → boards.greenhouse.io/monks/jobs/5996484004")
    result = await executor.execute(package, retry_count=0)

    # ── 6. Results ────────────────────────────────────────────────────────────
    SECTION("RESULTS")
    logger.info(f"  Status          : {result.status}")
    logger.info(f"  Screenshot      : {result.screenshot_url}")
    logger.info(f"  Confirmation    : {result.confirmation_text}")
    logger.info(f"  Error           : {result.error_message}")
    logger.info(f"  Execution time  : {result.execution_time_seconds:.1f}s")

    # ── 7. Verify DB state ────────────────────────────────────────────────────
    SECTION("MODULE 1: verify DB state after run")
    async with httpx.AsyncClient(timeout=15) as client:
        ar = await client.get(f"{api}/applications/{app_id}")
        if ar.status_code == 200:
            final = ar.json()
            logger.info(f"  DB Application status : {final['status']}")
            logger.info(f"  DB screenshot_url     : {final.get('screenshot_url')}")
            logger.info(f"  DB cover_letter_url   : {final.get('cover_letter_url')}")
            logger.info(f"  DB resume_id          : {final.get('resume_id')}")

    # ── 8. Screenshot location ────────────────────────────────────────────────
    SECTION("SCREENSHOT PATH")
    if result.screenshot_url:
        logger.info(f"\n  ✅ Screenshot saved at:")
        logger.info(f"     {result.screenshot_url}")
    else:
        logger.warning("  ⚠️  No screenshot was saved (may be early failure before browser opened)")

    logger.info(f"\n  Full debug log → {LOG_PATH}")
    return result


if __name__ == "__main__":
    result = asyncio.run(run())
    code = 0 if result and result.status in ("SUBMITTED", "FORM_COMPLETED") else 1
    sys.exit(code)
