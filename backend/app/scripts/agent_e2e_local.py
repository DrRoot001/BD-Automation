"""Standalone autonomous-agent E2E against the local mock form.

Drives ApplicationExecutor against a locally served Greenhouse-style form using
only PDF assets in this repo. No M1 API, no Postgres, no Redis required for
this run (rate limiter and state machine calls fail open with logged warnings).

This proves the LLM-driven pipeline end-to-end:
  * mock form served on 127.0.0.1
  * Claude (via OpenRouter) decides values for every field it sees
  * Playwright types them and uploads PDFs
  * screenshot is captured into ./screenshots/
  * field_memory.json is enriched with what was learned
  * learned_fixes/greenhouse.json is enriched if any selector recovery happens

Run::

    cd backend
    DRY_RUN_NO_SUBMIT=true python -m app.scripts.agent_e2e_local

Set DRY_RUN_NO_SUBMIT=false to actually click submit (still safe — the mock
form posts to itself).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
ROOT = BACKEND.parent
for p in (str(BACKEND), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

# Defaults — caller can override via env
os.environ.setdefault("DRY_RUN_NO_SUBMIT", "true")
os.environ.setdefault("USE_LLM_FILLER", "true")
os.environ.setdefault("USE_PAGE_AGENT", "true")
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true")

LOG = logging.getLogger("agent_e2e")
fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-30s %(message)s")
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(fmt)
logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(sh)


PORT = 9997
URL = f"http://127.0.0.1:{PORT}/mock_greenhouse_form.html"


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        return


def _serve():
    os.chdir(str(HERE))
    srv = HTTPServer(("127.0.0.1", PORT), _Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    LOG.info(f"Mock form served @ {URL}")
    return srv


def _resume_path() -> str:
    # Fall back to the bundled resume PDF at repo root if no tailored PDF exists.
    candidates = [
        ROOT / "harmain_ali_butt_resume.pdf",
        ROOT / "Sabih Haider — Software Engineer _ Full-Stack Web Developer.pdf",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    raise FileNotFoundError(f"No resume PDF found at: {candidates}")


async def run():
    from app.browser_automation.services.executor import ApplicationExecutor
    from app.browser_automation.services.models import ApplicationPackage

    resume = _resume_path()
    LOG.info(f"Resume: {resume}")

    candidate_id = "demo-cand-" + str(uuid4())[:8]
    application_id = "demo-app-" + str(uuid4())[:8]

    candidate_profile = {
        "name": "Harmain Ali Butt",
        "first_name": "Harmain",
        "last_name": "Butt",
        "email": "harmain.butt@example.com",
        "phone": "+1 (415) 555-0142",
        "location": "San Francisco, CA, USA",
        "linkedin_url": "https://linkedin.com/in/harmain-ali-butt",
        "website": "https://harmain.dev",
        "experience_years": "5",
        "tech_stack": "Python, FastAPI, React, TypeScript, Playwright, PostgreSQL",
        "current_company": "Stripe",
        "current_title": "Senior Software Engineer",
        "education": "BSc Computer Science, NUST",
        "salary_expectation": "$185,000",
        "work_authorization": "Yes",
        "sponsorship": "No",
        "agree_terms": "Yes",
        "willing_to_relocate": "Yes",
        "background_check": "Yes",
        "drug_test": "Yes",
        "referral_source": "Online",
        "start_date": "Immediately",
    }

    package = ApplicationPackage(
        application_id=application_id,
        candidate_id=candidate_id,
        job_id="demo-job-1",
        job_url=URL,
        platform="greenhouse",
        resume_url=resume,
        cover_letter_url=None,
        candidate_profile=candidate_profile,
        screening_answers={},
    )

    srv = _serve()
    try:
        executor = ApplicationExecutor()
        LOG.info(
            f"USE_LLM_FILLER={os.getenv('USE_LLM_FILLER')} "
            f"USE_PAGE_AGENT={os.getenv('USE_PAGE_AGENT')} "
            f"DRY_RUN_NO_SUBMIT={os.getenv('DRY_RUN_NO_SUBMIT')} "
            f"HEADLESS={os.getenv('PLAYWRIGHT_HEADLESS')}"
        )
        result = await executor.execute(package, retry_count=0)
    finally:
        srv.shutdown()

    LOG.info("=" * 60)
    LOG.info(f"status            = {result.status}")
    LOG.info(f"confirmation_text = {result.confirmation_text}")
    LOG.info(f"screenshot_url    = {result.screenshot_url}")
    LOG.info(f"error_message     = {result.error_message}")
    LOG.info(f"execution_time_s  = {result.execution_time_seconds:.1f}")
    LOG.info("=" * 60)
    return result


if __name__ == "__main__":
    r = asyncio.run(run())
    sys.exit(0 if r and r.status in ("SUBMITTED", "FORM_COMPLETED") else 1)
