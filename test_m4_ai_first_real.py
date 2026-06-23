#!/usr/bin/env python3
"""Module 4 — AI-First Real-World Test
======================================

Drives the AgentLoop against a REAL public job posting (Greenhouse-hosted by
default). Captures token usage, action-by-action AI decisions, and a final
screenshot of the filled form. We stop *before* clicking the final Submit so
we don't actually file a fake application on a real company — the screenshot
is the evidence that the AI filled the form correctly.

Usage:
    python test_m4_ai_first_real.py [job_url]

Outputs (in the project root):
    - test_m4_evidence_actions.json    (every action the AI took)
    - test_m4_evidence_screenshot.png  (filled-form screenshot)
    - test_m4_evidence_tokens.json     (token totals + per-step breakdown)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / "backend" / ".env")

# DRY_RUN_NO_SUBMIT default: respect whatever the caller set in the shell.
# Set to "true" to stop before final submit (safe for repeated testing on the
# same company), "false" to actually file a real application. Defaults to
# "true" if nothing is set — safer default for development.
os.environ.setdefault("DRY_RUN_NO_SUBMIT", "true")
_DRY_RUN = os.environ["DRY_RUN_NO_SUBMIT"].lower() == "true"
logging.info(
    "[m4-ai-first-real] DRY_RUN_NO_SUBMIT=%s — %s",
    os.environ["DRY_RUN_NO_SUBMIT"],
    "AI will stop before final submit" if _DRY_RUN else "AI WILL CLICK SUBMIT — REAL APPLICATION",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
)
logger = logging.getLogger("m4-ai-first-real")

# Lower the noise from third parties
for n in ("httpx", "httpcore", "anthropic", "urllib3"):
    logging.getLogger(n).setLevel(logging.WARNING)

# A well-known stable public Greenhouse job board for testing (Vercel).
# We override via argv if the caller wants a different job.
DEFAULT_JOB_URL = (
    "https://job-boards.greenhouse.io/vercel/jobs/5999792004"  # Vercel public Greenhouse job
)


# Candidate selection is now DATABASE-DRIVEN. Set TEST_CANDIDATE to any
# substring of the candidate's name or email (e.g. "sabih", "harmain",
# "haider@gmail") and we look them up in the `candidates` table, then pull
# their resume + cover letter URLs from `resumes` / `applications`. The
# resume PDF is parsed for the source-of-truth identity values (name, email,
# phone, LinkedIn) so the form fills match what the candidate uploaded.
#
# Universal policy answers (LinkedIn=N/A, demographics=fixed values) are
# enforced inside the AgentLoop regardless of which candidate is loaded.

# Default profile shape — DB-driven values overwrite these on resolve.
_DEFAULT_CANDIDATE = {
    "name": "",
    "first_name": "",
    "last_name": "",
    "email": "",
    "phone": "",
    "location": "",
    "linkedin_url": "",
    "website": "",
    "experience_years": "",
    "tech_stack": "",
    "current_company": "",
    "current_title": "",
    "education": "",
    "salary_expectation": "Negotiable",
    "work_authorization": "Yes",
    "sponsorship": "No",
    "agree_terms": "Yes",
    "willing_to_relocate": "Yes",
    "background_check": "Yes",
    "drug_test": "Yes",
    "referral_source": "",
    "start_date": "Immediately",
}
CANDIDATE: dict = dict(_DEFAULT_CANDIDATE)
RESOLVED_CANDIDATE_ID: str = ""  # set by resolve_candidate_from_db()


async def resolve_candidate_from_db(query: str) -> str:
    """Look up a candidate in the DB by name or email substring and populate
    the global CANDIDATE dict + RESOLVED_CANDIDATE_ID. Returns the resolved
    candidate UUID.

    Match strategy (first hit wins):
      1. name ILIKE %query%
      2. email ILIKE %query%
      3. id == query (when query is a full UUID)
    """
    global RESOLVED_CANDIDATE_ID, CANDIDATE
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))
    from sqlalchemy import select, or_
    from app.database import AsyncSessionLocal
    from app.models.candidate import Candidate

    q = (query or "").strip()
    if not q:
        raise RuntimeError("TEST_CANDIDATE not set — pass a name/email substring or UUID.")

    # Only enable the UUID predicate when the input actually looks like one;
    # postgres errors on `WHERE id = 'sabih'` because asyncpg validates the
    # parameter type up-front. Hyphenated 36-char check is enough.
    _is_uuid = bool(re.match(r"^[0-9a-fA-F-]{32,36}$", q))
    async with AsyncSessionLocal() as session:
        conds = [
            Candidate.name.ilike(f"%{q}%"),
            Candidate.email.ilike(f"%{q}%"),
        ]
        if _is_uuid:
            conds.append(Candidate.id == q)
        stmt = select(Candidate).where(or_(*conds)).limit(1)
        cand = (await session.execute(stmt)).scalar_one_or_none()

    if not cand:
        raise RuntimeError(
            f"No candidate in DB matching {q!r}. Run a candidates SELECT to "
            f"see what's available."
        )

    RESOLVED_CANDIDATE_ID = str(cand.id)
    # Build the CANDIDATE dict from whatever the DB row carries. Empty cells
    # stay empty — the resume parser will fill them.
    full_name = (cand.name or "").strip()
    parts = full_name.split()
    CANDIDATE.update({
        "name": full_name,
        "first_name": parts[0] if parts else "",
        "last_name": " ".join(parts[1:]) if len(parts) > 1 else "",
        "email": cand.email or "",
        "phone": cand.phone or "",
        "location": cand.location or "",
        "linkedin_url": cand.linkedin_url or "",
        "tech_stack": (cand.tech_stack or "") if hasattr(cand, "tech_stack") else "",
        "work_authorization": (cand.work_auth or "Yes") if hasattr(cand, "work_auth") else "Yes",
        "experience_years": str(cand.years_exp or "") if hasattr(cand, "years_exp") else "",
    })
    os.environ["TEST_CANDIDATE_ID"] = RESOLVED_CANDIDATE_ID
    print(
        f"[m4-ai-first-real] DB resolved candidate {full_name!r} "
        f"id={RESOLVED_CANDIDATE_ID} email={cand.email!r}"
    )
    return RESOLVED_CANDIDATE_ID


def _extract_pdf_text(path: str) -> str:
    """Read raw text from a resume PDF. Returns "" if extraction fails.

    Tries pypdf first (lightweight, pure-Python) then pdfminer for layouts
    pypdf chokes on. Resume PDFs are usually <5 pages so we don't bother
    with caching.
    """
    if not path:
        return ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        out = []
        for page in reader.pages:
            try:
                out.append(page.extract_text() or "")
            except Exception:
                pass
        text = "\n".join(out).strip()
        if text:
            return text
    except Exception as exc:
        print(f"[resume-parse] pypdf failed: {exc}")
    try:
        from pdfminer.high_level import extract_text as _pdfminer_extract
        return (_pdfminer_extract(path) or "").strip()
    except Exception as exc:
        print(f"[resume-parse] pdfminer failed: {exc}")
        return ""


def _derive_profile_from_resume(text: str) -> dict:
    """Best-effort regex extraction of name/email/phone/linkedin/github from
    a resume's raw text. Lets the test pick up the candidate's real values
    instead of using whatever was hardcoded in CANDIDATE.
    """
    if not text:
        return {}
    out: dict = {}
    # Email — first match wins
    m = re.search(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b", text)
    if m:
        out["email"] = m.group(1)
    # Phone — accept +CC or local formats, 10+ digits when stripped
    for cand in re.findall(r"(?:\+?\d[\d\-\s().]{8,}\d)", text):
        digits = re.sub(r"\D", "", cand)
        if 10 <= len(digits) <= 15:
            out["phone"] = cand.strip()
            break
    # LinkedIn — full URL preferred
    m = re.search(r"(https?://[^\s)]*linkedin\.com/[^\s)]+)", text, re.I)
    if m:
        out["linkedin_url"] = m.group(1).rstrip(".,;")
    else:
        m = re.search(r"\blinkedin\.com/in/[A-Za-z0-9\-_/]+", text, re.I)
        if m:
            out["linkedin_url"] = "https://" + m.group(0).rstrip(".,;")
    # GitHub / website
    m = re.search(r"(https?://[^\s)]*github\.com/[^\s)]+)", text, re.I)
    if m:
        out["website"] = m.group(1).rstrip(".,;")
    # Name — first non-empty line that looks like a person's name
    # (two-to-four capitalized words, no digits, no @, < 60 chars)
    for line in text.splitlines()[:8]:
        s = line.strip()
        if not s or "@" in s or any(c.isdigit() for c in s):
            continue
        if len(s) > 60:
            continue
        words = s.split()
        if 2 <= len(words) <= 4 and all(w[:1].isupper() for w in words if w):
            out["name"] = s
            parts = s.split()
            out["first_name"] = parts[0]
            out["last_name"] = " ".join(parts[1:])
            break
    return out


async def pick_first_greenhouse_job(page, board_url: str) -> str:
    """If we were given a job-board URL (lists multiple jobs), click into the
    first listing and return its URL. If it's already a single-job URL just
    return board_url.
    """
    await page.goto(board_url, wait_until="domcontentloaded", timeout=60_000)
    await asyncio.sleep(2.0)
    # Greenhouse public boards expose <div class="opening"><a href="...">Title</a></div>
    job_anchor = page.locator(".opening a, a[href*='/jobs/']").first
    cnt = await job_anchor.count()
    if cnt == 0:
        logger.info(f"No job-list anchors found at {board_url} — assuming single-job URL.")
        return board_url
    href = await job_anchor.get_attribute("href")
    if not href:
        return board_url
    if href.startswith("/"):
        from urllib.parse import urljoin
        href = urljoin(board_url, href)
    logger.info(f"Resolved first job listing → {href}")
    return href


async def main():
    job_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_JOB_URL

    # Lazy imports so env is loaded first
    from app.browser_automation.agent.loop import AgentLoop
    from app.browser_automation.adapters import get_adapter
    from app.browser_automation.llm import telemetry as tele
    from playwright.async_api import async_playwright

    tele.reset_session()

    # ── Fetch resume + cover letter FROM SUPABASE (same as production M1→M4) ──
    # Production never uses local files: M3 uploads resume + cover letter to
    # Supabase buckets and passes the public URLs to M4. M4's executor
    # downloads those URLs to temp files before launching the browser
    # (Playwright's set_input_files needs a local path). This standalone
    # test now mirrors that flow exactly so the demo is honest:
    #   1. Query the DB to find the latest tailored resume URL (Supabase)
    #      AND the latest cover-letter URL for the test candidate.
    #   2. Download both via the executor's _resolve_file_to_local_path helper.
    #   3. Hand the temp paths to AgentLoop.
    # No fallback to repo PDFs — if Supabase is unreachable the test fails
    # with a clear error so we never silently demo from local cache.
    from app.browser_automation.services.executor import _resolve_file_to_local_path
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.resume import Resume
    from app.models.application import Application

    # DB-driven candidate resolution. TEST_CANDIDATE can be a name/email
    # substring (e.g. "sabih") or a full UUID. We look the row up in the
    # `candidates` table and populate the CANDIDATE dict from it. Falls back
    # to TEST_CANDIDATE_ID for backward compatibility if TEST_CANDIDATE
    # isn't set.
    _query = os.getenv("TEST_CANDIDATE") or os.getenv("TEST_CANDIDATE_ID") or "harmain"
    try:
        TEST_CANDIDATE_ID = await resolve_candidate_from_db(_query)
    except Exception as exc:
        logger.error(f"[m4-ai-first-real] candidate lookup failed: {exc}")
        raise
    logger.info(f"Fetching files from Supabase for candidate_id={TEST_CANDIDATE_ID}")

    resume_supabase_url = None
    cover_letter_supabase_url = None
    db_ok = False

    # URL cache — remembers the last-known-good Supabase URLs from any prior
    # successful DB query. If DB goes down (Supabase pooler hiccups happen)
    # the cache still serves real Supabase URLs, so the test never silently
    # falls back to local files unless this is the very first run AND DB
    # is unreachable.
    CACHE_FILE = PROJECT_ROOT / ".m4_test_supabase_urls.json"
    cached_urls: dict = {}
    if CACHE_FILE.exists():
        try:
            cached_urls = json.loads(CACHE_FILE.read_text())
        except Exception:
            cached_urls = {}

    try:
        async with AsyncSessionLocal() as session:
            # Latest tailored resume first; fall back to latest base resume
            rq = (
                select(Resume)
                .where(Resume.candidate_id == TEST_CANDIDATE_ID)
                .order_by(Resume.is_base.asc(), Resume.version.desc())
                .limit(1)
            )
            rs = (await session.execute(rq)).scalar_one_or_none()
            if rs and rs.file_url and rs.file_url.startswith("http"):
                resume_supabase_url = rs.file_url
                logger.info(f"Resume Supabase URL: {resume_supabase_url}")
            # Latest application's cover_letter_url
            aq = (
                select(Application)
                .where(Application.candidate_id == TEST_CANDIDATE_ID,
                       Application.cover_letter_url.isnot(None))
                .order_by(Application.created_at.desc())
                .limit(1)
            )
            ap = (await session.execute(aq)).scalar_one_or_none()
            if ap and ap.cover_letter_url and ap.cover_letter_url.startswith("http"):
                cover_letter_supabase_url = ap.cover_letter_url
                logger.info(f"Cover letter Supabase URL: {cover_letter_supabase_url}")
        db_ok = True
        # Persist what we just got so the next run survives a DB outage
        if resume_supabase_url or cover_letter_supabase_url:
            try:
                CACHE_FILE.write_text(json.dumps({
                    "candidate_id": TEST_CANDIDATE_ID,
                    "resume_url": resume_supabase_url,
                    "cover_letter_url": cover_letter_supabase_url,
                }, indent=2))
                logger.info(f"Cached Supabase URLs to {CACHE_FILE.name}")
            except Exception:
                pass
    except Exception as exc:
        logger.warning(
            f"DB query failed ({exc.__class__.__name__}: {str(exc)[:120]}). "
            f"Falling back to URL cache from {CACHE_FILE.name} if available..."
        )
        # Use cached URLs from prior successful DB query
        if cached_urls.get("candidate_id") == TEST_CANDIDATE_ID:
            if not resume_supabase_url and cached_urls.get("resume_url"):
                resume_supabase_url = cached_urls["resume_url"]
                logger.info(f"Resume URL (from cache): {resume_supabase_url}")
            if not cover_letter_supabase_url and cached_urls.get("cover_letter_url"):
                cover_letter_supabase_url = cached_urls["cover_letter_url"]
                logger.info(f"Cover letter URL (from cache): {cover_letter_supabase_url}")

    # ── Fallback path 1: Supabase storage REST API (no DB required) ────────
    # If the DB is unreachable but Supabase storage is, we can still list the
    # candidate's resume + cover-letter buckets directly using just the anon
    # key. This is the "primary file source" the user explicitly asked for.
    if not resume_supabase_url or not cover_letter_supabase_url:
        try:
            import httpx
            from app.browser_automation.services.screenshot import (
                _supabase_project_url, _supabase_anon_key,
            )
            base = _supabase_project_url()
            key = _supabase_anon_key()
            if base and key:
                headers = {"Authorization": f"Bearer {key}", "apikey": key,
                           "Content-Type": "application/json"}
                async with httpx.AsyncClient(timeout=15) as client:
                    if not resume_supabase_url:
                        r = await client.post(
                            f"{base}/storage/v1/object/list/resume",
                            headers=headers,
                            json={"prefix": "", "limit": 100,
                                  "sortBy": {"column": "created_at", "order": "desc"}},
                        )
                        if r.status_code == 200:
                            items = [it for it in r.json()
                                     if TEST_CANDIDATE_ID in (it.get("name") or "")]
                            if items:
                                items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
                                fn = items[0]["name"]
                                resume_supabase_url = f"{base}/storage/v1/object/public/resume/{fn}"
                                logger.info(f"Resume URL (Supabase listing): {resume_supabase_url}")
                    if not cover_letter_supabase_url:
                        r = await client.post(
                            f"{base}/storage/v1/object/list/cover_letter",
                            headers=headers,
                            json={"prefix": "", "limit": 100,
                                  "sortBy": {"column": "created_at", "order": "desc"}},
                        )
                        if r.status_code == 200:
                            items = [it for it in r.json()
                                     if TEST_CANDIDATE_ID in (it.get("name") or "")]
                            if items:
                                items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
                                fn = items[0]["name"]
                                cover_letter_supabase_url = f"{base}/storage/v1/object/public/cover_letter/{fn}"
                                logger.info(f"Cover letter URL (Supabase listing): {cover_letter_supabase_url}")
        except Exception as exc:
            logger.warning(f"Supabase storage listing failed: {exc}")

    # ── Fallback path 2: local repo PDF (last resort, with warning) ────────
    if not resume_supabase_url:
        local_resume = PROJECT_ROOT / "harmain_ali_butt_resume.pdf"
        if not local_resume.exists():
            local_resume = PROJECT_ROOT / "dummy.pdf"
        if local_resume.exists():
            logger.warning(
                f"⚠ Supabase unreachable AND no DB resume found. Falling back to "
                f"local file {local_resume}. This is NOT production behavior."
            )
            resume_path = str(local_resume)
        else:
            raise RuntimeError(
                "Resume could not be sourced — DB query failed, Supabase listing "
                "failed, and no local fallback PDF exists. Cannot proceed."
            )
    else:
        resume_path = await _resolve_file_to_local_path(resume_supabase_url, ".pdf")
        if not resume_path:
            raise RuntimeError(f"Failed to download resume from {resume_supabase_url}")
        logger.info(f"Resume downloaded to temp: {resume_path}")

    # ── Extract resume content + override profile from it ──────────────────
    # "AI is the master" — instead of trusting the hardcoded CANDIDATE dict,
    # parse the actual resume PDF and let the agent see its real contents.
    # We also auto-populate name / email / phone / linkedin from the parsed
    # text so the form fills match the resume the candidate actually uploaded
    # (mismatch between dict and resume = ATS-side rejection).
    resume_text = _extract_pdf_text(resume_path)
    if resume_text:
        derived = _derive_profile_from_resume(resume_text)
        if derived:
            logger.info(
                f"[resume-parse] derived from resume → "
                + ", ".join(f"{k}={v!r}" for k, v in derived.items() if v)
            )
            # Overwrite CANDIDATE fields with resume-derived values.
            # Resume wins over the hardcoded dict — that's the whole point.
            for k, v in derived.items():
                if v:
                    CANDIDATE[k] = v
            # ── Sync the per-candidate memory file ──────────────────────────
            # The form-fill memory layer recalls these values via per-candidate
            # JSON. If a previous session memorized a different name/email/
            # phone for this candidate (e.g. an older resume), those stale
            # values would pre-fill BEFORE the AI ever sees the resume —
            # which is the "form still shows Harmain" bug we hit. Refresh
            # the file from the resume so memory and resume agree.
            try:
                from app.browser_automation.forms import memory as _fm
                _cand_id = RESOLVED_CANDIDATE_ID
                # NOTE: LinkedIn is intentionally NOT in this sync map —
                # operator policy is to always answer LinkedIn fields with
                # "N/A", not the real URL from the resume. Persisting the
                # real URL would defeat the runtime override on next run.
                _label_map = {
                    "name": ("Name", derived.get("name")),
                    "first_name": ("First Name", derived.get("first_name")),
                    "last_name": ("Last Name", derived.get("last_name")),
                    "email": ("Email", derived.get("email")),
                    "phone": ("Phone", derived.get("phone")),
                    "website": ("Website", derived.get("website")),
                }
                # Pin LinkedIn → "N/A" in memory so even if a previous
                # session cached a real URL it gets overwritten this run.
                try:
                    _fm.remember(
                        label="LinkedIn Profile",
                        field_type="text",
                        value="N/A",
                        source="policy_override",
                        candidate_id=_cand_id,
                    )
                except Exception as exc:
                    logger.debug(f"[resume-parse] LinkedIn N/A pin failed: {exc}")
                for _, (lbl, val) in _label_map.items():
                    if val:
                        try:
                            _fm.remember(
                                label=lbl,
                                field_type="text",
                                value=val,
                                source="resume_parse",
                                candidate_id=_cand_id,
                            )
                        except Exception as exc:
                            logger.debug(f"[resume-parse] memory sync {lbl} failed: {exc}")
                logger.info(
                    f"[resume-parse] synced per-candidate memory file for {_cand_id[:8]}"
                )
            except Exception as exc:
                logger.warning(f"[resume-parse] memory sync skipped: {exc}")
        CANDIDATE["_resume_text"] = resume_text[:6000]  # cap for prompt size
    else:
        logger.warning(
            "[resume-parse] could NOT extract text from resume PDF — "
            "falling back to hardcoded CANDIDATE dict values."
        )

    cover_letter_path = None
    if cover_letter_supabase_url:
        cover_letter_path = await _resolve_file_to_local_path(cover_letter_supabase_url, ".pdf")
        if cover_letter_path:
            logger.info(f"Cover letter downloaded to temp: {cover_letter_path}")
        else:
            logger.warning(f"Cover letter download failed from {cover_letter_supabase_url}")
    if not cover_letter_path:
        # Last resort: use the resume as cover letter (better than skipping
        # the field entirely if the form requires it)
        cover_letter_path = resume_path
        logger.warning(f"⚠ No cover letter available — using resume as stand-in for the cover-letter file input.")

    actions_log = []
    screenshot_path = PROJECT_ROOT / "test_m4_evidence_screenshot.png"
    started_at = time.monotonic()
    final_status = "INIT"
    final_error = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=[
            "--disable-blink-features=AutomationControlled",
        ])
        context = await browser.new_context(
            viewport={"width": 1366, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = await context.new_page()
        try:
            # Resolve a real job URL (may be a single job already)
            real_job_url = await pick_first_greenhouse_job(page, job_url)

            # Route through the GreenhouseAdapter so we get the URL rewrite
            # (mirrored careers pages like cockroachlabs.com/careers/job/?gh_jid=X
            # get auto-redirected to job-boards.greenhouse.io/<slug>/jobs/<id>),
            # the iframe detection (#grnhse_iframe embeds on company sites),
            # the bot-block detection, and the auto-click of the Apply link
            # on listing pages. This mirrors what the production executor does.
            adapter = get_adapter("greenhouse")
            await adapter.navigate_to_application(page, real_job_url)

            # If the adapter detected iframe mode, hand the frame to AgentLoop
            # so its DOM snapshot reads inside the iframe instead of the host page.
            frame_loc = getattr(adapter, "_frame_locator", None) or getattr(adapter, "_frame", None)

            loop = AgentLoop(
                candidate_profile=CANDIDATE,
                job_context={
                    "platform": "greenhouse",
                    "ats_type": "greenhouse",
                    "job_url": real_job_url,
                    "job_title": "(read from page)",
                    "company": "(read from page)",
                },
                resume_path=resume_path,
                cover_letter_path=cover_letter_path,
                max_steps=60,
                # Lever 2: enable per-candidate memory recall across runs.
                # Second invocation of this test reuses memorized field values
                # and skips the LLM for identity fields.
                # MUST track the selected profile — was hardcoded to Harmain
                # which caused his memorized name/email/phone to be pre-filled
                # into Sabih's session (the "form filled with Harmain even when
                # TEST_CANDIDATE=sabih" bug).
                candidate_id=RESOLVED_CANDIDATE_ID,
                # Honor DRY_RUN_NO_SUBMIT — when true, the AI verifies the
                # form is complete via the pre-submit gate, then exits before
                # actually clicking submit. When false, it really files.
                stop_before_submit=_DRY_RUN,
            )
            logger.info(f"System prompt size = {len(loop._system_prompt)} chars")

            result = await loop.run(page, frame=frame_loc)
            final_status = result.status
            final_error = result.error
            actions_log = [
                {
                    "step": a.step,
                    "kind": a.kind,
                    "selector": a.selector,
                    "value": (a.value or "")[:200],
                    "field_label": a.field_label,
                    "reason": a.reason,
                    "confirmation": a.confirmation,
                }
                for a in result.actions
            ]
            logger.info(f"AgentLoop result: status={result.status} steps={result.steps_taken} error={result.error!r}")

            # Capture filled-form screenshot
            try:
                await page.screenshot(path=str(screenshot_path), full_page=True)
                logger.info(f"Screenshot saved → {screenshot_path}")
            except Exception as exc:
                logger.warning(f"screenshot failed: {exc}")

        finally:
            elapsed = time.monotonic() - started_at
            session = tele.get_session()
            tele.log_summary("[Tokens] FINAL")

            evidence = {
                "job_url": job_url,
                "candidate_name": CANDIDATE["name"],
                "status": final_status,
                "error": final_error,
                "elapsed_seconds": round(elapsed, 2),
                "tokens": {
                    "calls": len(session.calls),
                    "input": session.total_input,
                    "output": session.total_output,
                    "total": session.total,
                    "by_label": session.by_label,
                },
                "actions_count": len(actions_log),
            }
            (PROJECT_ROOT / "test_m4_evidence_tokens.json").write_text(
                json.dumps(evidence, indent=2)
            )
            (PROJECT_ROOT / "test_m4_evidence_actions.json").write_text(
                json.dumps(actions_log, indent=2)
            )
            logger.info(f"Evidence written to {PROJECT_ROOT}/test_m4_evidence_*.json")

            await asyncio.sleep(2.0)
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
