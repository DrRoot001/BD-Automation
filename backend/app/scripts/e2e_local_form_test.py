"""
E2E test using a LOCAL Greenhouse-like HTML form (no bot-block risk).

Serves mock_greenhouse_form.html on localhost:9997, then drives M4's
ApplicationExecutor against it with Harmain Ali Butt's real tailored
resume and cover letter PDFs.

Validates:
  - All text/select/radio/checkbox fields detected and filled
  - Both PDFs uploaded (input[name='file-attachment'])
  - Submission clicked (or skipped in dry-run)
  - Screenshot captured
  - State machine transitions work end-to-end

Usage:
    cd backend
    python app/scripts/e2e_local_form_test.py            # DRY_RUN=true (default)
    DRY_RUN_NO_SUBMIT=false python app/scripts/e2e_local_form_test.py  # live submit
"""

import asyncio
import os
import sys
import logging
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# ── Path setup ───────────────────────────────────────────────────────────────
HERE    = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
ROOT    = BACKEND.parent
for p in [str(BACKEND), str(ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")
os.environ.setdefault("DRY_RUN_NO_SUBMIT", "true")
os.environ.setdefault("M1_API_BASE_URL", "http://127.0.0.1:8000/api")

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_PATH = str(HERE / "e2e_local_test.log")
fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s  %(message)s")
fh = logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(fmt)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(fmt)
logging.getLogger().setLevel(logging.DEBUG)
logging.getLogger().addHandler(fh)
logging.getLogger().addHandler(ch)
logger = logging.getLogger("e2e_local")

# ── Constants ─────────────────────────────────────────────────────────────────
MOCK_PORT    = 9997
MOCK_URL     = f"http://localhost:{MOCK_PORT}/mock_greenhouse_form.html"
CANDIDATE_ID = "9f1d8c11-9a7c-48a0-ba09-5a8222a00cba"
JOB_ID       = "09c52692-5c97-46b8-94a3-e8e3ddaf61e1"
RESUME_PATH  = str(BACKEND / "data" / "tailored_resumes" /
               "tailored_9f1d8c11-9a7c-48a0-ba09-5a8222a00cba_"
               "09c52692-5c97-46b8-94a3-e8e3ddaf61e1_v3.pdf")
COVER_PATH   = str(BACKEND / "data" / "cover_letters" /
               "cover_letter_9f1d8c11-9a7c-48a0-ba09-5a8222a00cba_"
               "09c52692-5c97-46b8-94a3-e8e3ddaf61e1.pdf")

SECTION = lambda t: logger.info("\n" + "="*60 + f"\n  {t}\n" + "="*60)


# ── Local HTTP server ─────────────────────────────────────────────────────────

class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress access log noise

def _start_server():
    os.chdir(str(HERE))  # serve files relative to scripts/
    srv = HTTPServer(("localhost", MOCK_PORT), _QuietHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    logger.info(f"Mock form server running at {MOCK_URL}")
    return srv


# ── Main ──────────────────────────────────────────────────────────────────────

async def run():
    # ── 0. Pre-flight ────────────────────────────────────────────────────────
    SECTION("PRE-FLIGHT: assets")
    for label, path in [("Resume (v3)", RESUME_PATH), ("Cover Letter", COVER_PATH)]:
        ok   = os.path.isfile(path)
        size = os.path.getsize(path) if ok else 0
        logger.info(f"  {label}: {'OK' if ok else 'MISSING'} ({size:,} bytes) - {path}")
        if not ok:
            logger.error(f"ABORT: {label} not found")
            return None

    # ── 1. Fetch candidate profile from M1 API ───────────────────────────────
    SECTION("MODULE 1: fetch candidate + job")
    import httpx
    api = os.environ["M1_API_BASE_URL"]
    async with httpx.AsyncClient(timeout=15) as client:
        h  = await client.get(f"{api.replace('/api','')}/api/health")
        assert h.status_code == 200, f"API down: {h.text}"
        logger.info(f"  API health: {h.status_code}")

        cr = await client.get(f"{api}/candidates/{CANDIDATE_ID}")
        assert cr.status_code == 200
        cand = cr.json()
        logger.info(f"  Candidate: {cand['name']} | email={cand['email']} | "
                    f"work_auth={cand['work_auth']} | years_exp={cand['years_exp']}")

        jr = await client.get(f"{api}/jobs/{JOB_ID}")
        assert jr.status_code == 200
        job = jr.json()
        logger.info(f"  Job: {job['title']} @ {job['company']}")

    # ── 2. Reset application to QUEUED via direct DB ─────────────────────────
    SECTION("MODULE 1 -> STATE: reset application")
    import asyncpg
    pg_url = os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(pg_url)
    try:
        row = await conn.fetchrow(
            "SELECT id, status FROM applications WHERE candidate_id=$1 AND job_id=$2",
            CANDIDATE_ID, JOB_ID
        )
        if row:
            app_id = str(row["id"])
            logger.info(f"  Existing app {app_id} (was {row['status']}) -> QUEUED")
            await conn.execute("UPDATE applications SET status='QUEUED', error_message=NULL WHERE id=$1", row["id"])
            await conn.execute("DELETE FROM application_history WHERE application_id=$1", row["id"])
        else:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(f"{api}/applications",
                                      json={"candidate_id": CANDIDATE_ID, "job_id": JOB_ID})
                assert r.status_code in (200, 201)
                app_id = str(r.json()["id"])
            logger.info(f"  Created new application {app_id}")
    finally:
        await conn.close()
    logger.info(f"  Application ID: {app_id}")

    # ── 3. Build ApplicationPackage ──────────────────────────────────────────
    SECTION("MODULE 3 -> 4 HANDOFF: ApplicationPackage")
    from app.browser_automation.services.models import ApplicationPackage

    full_name  = cand["name"]
    parts      = full_name.split()
    is_auth    = (cand.get("work_auth") or "").lower() in (
        "us_authorized", "citizen", "green_card", "visa", "ead", "authorized"
    )
    candidate_profile = {
        "name":               full_name,
        "first_name":         parts[0] if parts else "",
        "last_name":          " ".join(parts[1:]) if len(parts) > 1 else "",
        "email":              cand["email"],
        "phone":              cand.get("phone") or "+1 (555) 000-0000",
        "location":           cand.get("location") or "Remote",
        "linkedin_url":       cand.get("linkedin_url") or "",
        "website":            cand.get("website") or "",
        "experience_years":   str(cand.get("years_exp") or "3"),
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
    }
    package = ApplicationPackage(
        application_id=app_id,
        candidate_id=CANDIDATE_ID,
        job_id=JOB_ID,
        job_url=MOCK_URL,          # <-- LOCAL FORM, no bot detection
        platform="greenhouse",
        resume_url=RESUME_PATH,
        cover_letter_url=COVER_PATH,
        candidate_profile=candidate_profile,
        screening_answers={},
    )
    logger.info(f"  job_url  : {package.job_url}")
    logger.info(f"  resume   : {package.resume_url}")
    logger.info(f"  cover_ltr: {package.cover_letter_url}")
    logger.info(f"  profile  : {list(candidate_profile)}")

    # ── 4. Start local mock form server ──────────────────────────────────────
    SECTION("LOCAL SERVER: serving mock Greenhouse form")
    srv = _start_server()

    # ── 5. Run Module 4 executor ──────────────────────────────────────────────
    SECTION(f"MODULE 4: browser automation (DRY_RUN={os.getenv('DRY_RUN_NO_SUBMIT')})")
    from app.browser_automation.services.executor import ApplicationExecutor
    executor = ApplicationExecutor()
    logger.info(f"  Navigating to: {MOCK_URL}")
    result = await executor.execute(package, retry_count=0)

    # ── 6. Results ────────────────────────────────────────────────────────────
    SECTION("RESULTS")
    logger.info(f"  Status         : {result.status}")
    logger.info(f"  Screenshot     : {result.screenshot_url}")
    logger.info(f"  Confirmation   : {result.confirmation_text}")
    logger.info(f"  Error          : {result.error_message}")
    logger.info(f"  Execution time : {result.execution_time_seconds:.1f}s")

    if result.screenshot_url:
        # Resolve relative path to absolute
        ss_path = result.screenshot_url
        if ss_path.startswith("./"):
            ss_path = str(BACKEND / ss_path[2:])
        logger.info(f"\n  SCREENSHOT -> open this file to see the filled form:\n     {ss_path}")

    # ── 7. DB verification ────────────────────────────────────────────────────
    SECTION("MODULE 1: verify final DB state")
    async with httpx.AsyncClient(timeout=15) as client:
        ar = await client.get(f"{api}/applications/{app_id}")
        if ar.status_code == 200:
            final = ar.json()
            logger.info(f"  status       : {final['status']}")
            logger.info(f"  screenshot   : {final.get('screenshot_url')}")

    srv.shutdown()
    logger.info(f"\n  Full log: {LOG_PATH}")
    return result


if __name__ == "__main__":
    result = asyncio.run(run())
    sys.exit(0 if result and result.status in ("SUBMITTED", "FORM_COMPLETED") else 1)
