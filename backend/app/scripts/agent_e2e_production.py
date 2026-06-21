"""Autonomous-agent E2E against a REAL production ATS website.

Reads a real candidate from the database, reads real jobs already scraped by
Module 2, scores them against the candidate's tech_stack, and dispatches the
M4 executor against the **top-matching real Greenhouse / Lever / Ashby URL**.

This is the closest thing to a production run we can do without a Celery
worker — same code paths, same browser, same LLM decisions, against actual
employer pages. Default mode is DRY_RUN_NO_SUBMIT=true: the agent fills the
form and screenshots the result, but does NOT click Submit on a real company's
page. Flip the env var to false when you're ready to actually apply.

Usage::

    cd backend
    # Default: dry-run against the top match for the most-recent real candidate
    python -m app.scripts.agent_e2e_production

    # Specify a candidate
    AGENT_CANDIDATE_ID=ca35fce1-1679-4f1f-afa7-1ff57184b54b \\
        python -m app.scripts.agent_e2e_production

    # Pick a specific job (skip auto-scoring)
    AGENT_JOB_ID=<uuid> python -m app.scripts.agent_e2e_production

    # Actually submit (be careful — real employer)
    DRY_RUN_NO_SUBMIT=false python -m app.scripts.agent_e2e_production
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
ROOT = BACKEND.parent
for p in (str(BACKEND), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

# Defaults for safety
os.environ.setdefault("DRY_RUN_NO_SUBMIT", "true")
os.environ.setdefault("USE_LLM_FILLER", "true")
os.environ.setdefault("USE_PAGE_AGENT", "true")
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "false")  # head visible so you can watch

LOG = logging.getLogger("agent_prod")
logging.getLogger().setLevel(logging.INFO)
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-32s %(message)s"))
logging.getLogger().addHandler(sh)

import asyncpg


_WORD_RE = re.compile(r"[A-Za-z0-9\+\-\#\.]{2,}")


def _tokens(s: str) -> set[str]:
    if not s:
        return set()
    return {t.lower() for t in _WORD_RE.findall(s)}


def _score_job(stack: set[str], job) -> float:
    if not stack:
        return 0.0
    haystack = set()
    haystack.update(_tokens(job["title"] or ""))
    haystack.update(_tokens(" ".join(job["skills"] or [])))
    haystack.update(_tokens((job["description"] or "")[:2500]))
    return len(stack & haystack) / max(len(stack), 1)


async def _pick_candidate(conn) -> dict:
    cid = os.getenv("AGENT_CANDIDATE_ID", "").strip()
    if cid:
        row = await conn.fetchrow(
            "SELECT id, name, email, phone, location, work_auth, years_exp, "
            "tech_stack, linkedin_url FROM candidates WHERE id=$1", cid)
        if not row:
            raise SystemExit(f"AGENT_CANDIDATE_ID={cid} not found in DB")
        return dict(row)
    # Auto-pick: most recent non-test candidate with a populated tech_stack
    row = await conn.fetchrow("""
        SELECT id, name, email, phone, location, work_auth, years_exp, tech_stack, linkedin_url
        FROM candidates
        WHERE tech_stack IS NOT NULL AND array_length(tech_stack, 1) > 0
          AND name NOT ILIKE '%test%'
        ORDER BY created_at DESC NULLS LAST
        LIMIT 1
    """)
    if not row:
        raise SystemExit("No non-test candidates with a tech_stack in DB")
    return dict(row)


async def _pick_job(conn, candidate: dict) -> dict:
    jid = os.getenv("AGENT_JOB_ID", "").strip()
    if jid:
        row = await conn.fetchrow(
            "SELECT id, title, company, source, source_url, skills, description, job_type "
            "FROM jobs WHERE id=$1", jid)
        if not row:
            raise SystemExit(f"AGENT_JOB_ID={jid} not found")
        return dict(row)

    stack = {s.lower() for s in (candidate["tech_stack"] or [])}
    jobs = await conn.fetch("""
        SELECT id, title, company, source, source_url, skills, description, job_type
        FROM jobs
        WHERE source_url IS NOT NULL
          AND source IN ('greenhouse', 'lever', 'ashby')
    """)
    scored = [(_score_job(stack, j), j) for j in jobs]
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [x for x in scored if x[0] > 0][:5]
    LOG.info("Top 5 matches for %s:", candidate["name"])
    for score, j in top:
        LOG.info("  score=%.2f | %-20s | %-50s | %s",
                 score, j["company"], (j["title"] or "")[:50], j["source_url"])
    if not top:
        raise SystemExit("No matching jobs scored > 0")
    return dict(top[0][1])


async def _ensure_application(conn, candidate_id: str, job_id: str) -> str:
    row = await conn.fetchrow(
        "SELECT id FROM applications WHERE candidate_id=$1 AND job_id=$2 LIMIT 1",
        candidate_id, job_id)
    if row:
        # Reset status so the executor runs cleanly
        await conn.execute(
            "UPDATE applications SET status='QUEUED', error_message=NULL WHERE id=$1",
            row["id"])
        return str(row["id"])
    new_id = uuid.uuid4()
    await conn.execute("""
        INSERT INTO applications (id, candidate_id, job_id, status, created_at)
        VALUES ($1, $2, $3, 'QUEUED', NOW())
    """, new_id, candidate_id, job_id)
    return str(new_id)


def _resolve_resume(candidate_id: Optional[str] = None) -> str:
    # Prefer a tailored resume for this candidate from M3 if one exists.
    tailored_dir = BACKEND / "data" / "tailored_resumes"
    if candidate_id and tailored_dir.is_dir():
        cands = sorted(
            tailored_dir.glob(f"tailored_{candidate_id}_*.pdf"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if cands:
            LOG.info(f"Resume: using tailored M3 PDF → {cands[0].name}")
            return str(cands[0])
    # No tailored resume for this candidate. Fall back to a bundled PDF, but
    # LOUDLY warn — this is an identity mismatch and only acceptable for
    # demo/dev runs. Production should never hit this path.
    for c in (
        ROOT / "harmain_ali_butt_resume.pdf",
        ROOT / "Sabih Haider — Software Engineer _ Full-Stack Web Developer.pdf",
    ):
        if c.is_file():
            LOG.warning(
                f"RESUME FALLBACK: no tailored resume found for candidate "
                f"{candidate_id} — uploading bundled {c.name}. "
                f"M3 must produce tailored_{candidate_id}_*.pdf before this is production-safe."
            )
            return str(c)
    raise FileNotFoundError("No resume PDF in repo root")


def _resolve_cover_letter(candidate_id: Optional[str] = None) -> Optional[str]:
    """Return a cover-letter PDF for this candidate, preferring tailored M3
    output. Falls back to any cover_letter_*.pdf in the cover_letters dir.
    Returns None if nothing exists — executor will skip the cover letter slot.
    """
    cl_dir = BACKEND / "data" / "cover_letters"
    if not cl_dir.is_dir():
        return None
    if candidate_id:
        cands = sorted(
            cl_dir.glob(f"cover_letter_{candidate_id}_*.pdf"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if cands:
            return str(cands[0])
    # No candidate-specific cover letter exists. Do NOT silently fall back to
    # someone else's cover letter — that's an identity leak. Return None and
    # let the form's optional Cover Letter slot stay empty.
    LOG.warning(
        f"COVER LETTER: no cover_letter_{candidate_id}_*.pdf found — "
        f"agent will skip the cover-letter upload slot. M3 must generate "
        f"a per-candidate cover letter for production runs."
    )
    return None


async def run():
    db_url = os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(db_url)
    try:
        candidate = await _pick_candidate(conn)
        LOG.info("=" * 70)
        LOG.info("CANDIDATE")
        LOG.info("=" * 70)
        LOG.info("  id     : %s", candidate["id"])
        LOG.info("  name   : %s", candidate["name"])
        LOG.info("  email  : %s", candidate["email"])
        LOG.info("  exp    : %s years", candidate["years_exp"])
        LOG.info("  stack  : %s", list(candidate["tech_stack"] or []))
        LOG.info("  loc    : %s", candidate["location"])

        job = await _pick_job(conn, candidate)
        LOG.info("")
        LOG.info("=" * 70)
        LOG.info("TARGET JOB (top match against candidate's CV)")
        LOG.info("=" * 70)
        LOG.info("  id      : %s", job["id"])
        LOG.info("  company : %s", job["company"])
        LOG.info("  title   : %s", job["title"])
        LOG.info("  ats     : %s", job["source"])
        LOG.info("  url     : %s", job["source_url"])

        application_id = await _ensure_application(conn, str(candidate["id"]), str(job["id"]))
        LOG.info("  app_id  : %s (reset to QUEUED)", application_id)
    finally:
        await conn.close()

    from app.browser_automation.services.executor import ApplicationExecutor
    from app.browser_automation.services.models import ApplicationPackage

    # Build candidate_profile in the exact shape the form filler expects
    full_name = candidate["name"] or ""
    parts = full_name.split()
    work_auth = (candidate["work_auth"] or "us_authorized").lower()
    is_auth = work_auth in ("us_authorized", "citizen", "green_card", "visa", "ead", "authorized")

    candidate_profile = {
        "name": full_name,
        "first_name": parts[0] if parts else "",
        "last_name": " ".join(parts[1:]) if len(parts) > 1 else "",
        "email": candidate["email"] or "",
        "phone": candidate["phone"] or "+1 (415) 555-0142",
        "location": candidate["location"] or "United States",
        "linkedin_url": candidate.get("linkedin_url") or "",
        "website": "",
        "experience_years": str(candidate["years_exp"] or "3"),
        "tech_stack": ", ".join(candidate["tech_stack"] or []),
        "current_company": "",
        "current_title": "",
        "education": "",
        "salary_expectation": "",
        "work_authorization": "Yes" if is_auth else "No",
        "sponsorship": "No" if is_auth else "Yes",
        "agree_terms": "Yes",
        "willing_to_relocate": "Yes",
        "background_check": "Yes",
        "drug_test": "Yes",
        "referral_source": "Online",
        "start_date": "Immediately",
    }

    package = ApplicationPackage(
        application_id=application_id,
        candidate_id=str(candidate["id"]),
        job_id=str(job["id"]),
        job_url=job["source_url"],
        platform=(job["source"] or "greenhouse").lower(),
        job_type=job.get("job_type") or "",
        resume_url=_resolve_resume(str(candidate["id"])),
        cover_letter_url=_resolve_cover_letter(str(candidate["id"])),
        candidate_profile=candidate_profile,
        screening_answers={},
    )

    LOG.info("")
    LOG.info("=" * 70)
    LOG.info("LAUNCHING AGENT  (DRY_RUN=%s  HEADLESS=%s  LLM=%s  PAGE_AGENT=%s)",
             os.getenv("DRY_RUN_NO_SUBMIT"), os.getenv("PLAYWRIGHT_HEADLESS"),
             os.getenv("USE_LLM_FILLER"), os.getenv("USE_PAGE_AGENT"))
    LOG.info("=" * 70)

    executor = ApplicationExecutor()
    result = await executor.execute(package, retry_count=0)

    LOG.info("")
    LOG.info("=" * 70)
    LOG.info("RESULT")
    LOG.info("=" * 70)
    LOG.info("  status            = %s", result.status)
    LOG.info("  confirmation_text = %s", result.confirmation_text)
    LOG.info("  screenshot_url    = %s", result.screenshot_url)
    LOG.info("  error_message     = %s", result.error_message)
    LOG.info("  execution_time_s  = %.1f", result.execution_time_seconds)
    return result


if __name__ == "__main__":
    r = asyncio.run(run())
    sys.exit(0 if r and r.status in ("SUBMITTED", "FORM_COMPLETED") else 1)
