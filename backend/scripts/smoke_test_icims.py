"""Smoke test for the iCIMS adapter end-to-end flow.

Mirrors ``smoke_test_rr.py`` but targets an iCIMS posting. Bypasses Celery,
pulls the candidate + latest resume / cover letter from the M1 API, builds an
ApplicationPackage with ``platform="icims"``, and runs ApplicationExecutor
inline so the AgentLoop drives the full Apply → email-consent → step-1 →
step-2 → Submit flow.

DRY_RUN_NO_SUBMIT is forced ON unless ``--really-submit`` is passed, so this
never fires a real application by default.

Usage (PowerShell):
    cd C:\\Users\\Rehan\\Desktop\\BD-Automator-Agent\\backend
    # 1. If you don't know Sabih's UUID:
    python -m scripts.smoke_test_icims --list-candidates
    # 2. Run the dry-run flow:
    python -m scripts.smoke_test_icims --candidate-id <sabih-uuid>
    # 3. Use a different iCIMS posting:
    python -m scripts.smoke_test_icims --candidate-id <uuid> \\
        --job-url https://careers.icims.com/careers-home/jobs/<other-id>

Environment knobs honoured (set before running if you want to override):
    OPENROUTER_API_KEY=sk-or-...     LLM key — auto-detected from sk-or-* prefix.
    DRY_RUN_NO_SUBMIT=true|false     Block the final submit click.
    USE_AGENT_LOOP=true|false        Disable the vision agent (debug only).
    AGENT_LOOP_WALL_TIMEOUT_S=480    Per-loop wall-clock ceiling.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from typing import Optional

import httpx

# ─────────────────────────────────────────────────────────────────────────────
# Reference posting used as the default --job-url. It's the Sr. Systems
# Engineer listing under careers.icims.com — the same URL the iCIMS adapter
# was built against. Override on the CLI for any other iCIMS posting.
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_ICIMS_URL = (
    "https://careers.icims.com/careers-home/jobs/6452"
    "?lang=en-gb&previousLocale=en-US"
)
_DEFAULT_JOB_TITLE = "Sr. Systems Engineer"
_DEFAULT_COMPANY = "iCIMS"
_DEFAULT_JOB_DESC = (
    "Sr. Systems Engineer at iCIMS. Operate and improve production "
    "infrastructure for the iCIMS Talent Platform across Linux, networking, "
    "virtualization, and cloud (AWS/Azure). Strong hands-on systems "
    "administration, scripting (Bash/Python), monitoring (Datadog/Grafana), "
    "and incident response. Required: 5+ years systems engineering, deep "
    "Linux internals, automation-first mindset, on-call experience."
)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _list_candidates(api_base: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{api_base}/candidates")
    if resp.status_code != 200:
        print(f"[!] {api_base}/candidates returned {resp.status_code}: {resp.text[:200]}")
        return
    for c in resp.json():
        print(f"  {c.get('id'):<40} {c.get('name'):<30} {c.get('email','')}")


async def _download_resume(resume_url: str) -> Optional[str]:
    import tempfile
    from urllib.parse import urlparse, unquote

    if not resume_url.startswith(("http://", "https://")):
        return resume_url if os.path.isfile(resume_url) else None

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(resume_url)
            resp.raise_for_status()
        parsed = urlparse(resume_url)
        fname = os.path.basename(unquote(parsed.path)) or "resume.pdf"
        if not fname.lower().endswith(".pdf"):
            fname += ".pdf"
        tmp = os.path.join(tempfile.gettempdir(), f"smoke_icims_{fname}")
        with open(tmp, "wb") as f:
            f.write(resp.content)
        return tmp
    except Exception as exc:
        print(f"[!] Resume download failed (will skip enrichment): {exc}")
        return None


async def _build_package(api_base: str, candidate_id: str, job_url: str,
                         job_title: str, company: str, job_description: str):
    from app.browser_automation.services.models import ApplicationPackage
    from app.browser_automation.services.resume_enricher import enrich_profile_from_resume

    async with httpx.AsyncClient(timeout=30) as client:
        cand_resp = await client.get(f"{api_base}/candidates/{candidate_id}")
        cand_resp.raise_for_status()
        cand = cand_resp.json()

        res_resp = await client.get(f"{api_base}/resumes/{candidate_id}")
        res_resp.raise_for_status()
        resumes = res_resp.json() or []

        apps_resp = await client.get(f"{api_base}/applications")
        apps_resp.raise_for_status()
        apps = apps_resp.json() or []

    # Latest tailored, else base.
    resumes.sort(key=lambda r: (not r.get("is_base"), r.get("version", 0)), reverse=True)
    resume = next(iter(resumes), None)
    if not resume:
        raise RuntimeError(f"No resume found for candidate {candidate_id}")
    resume_url = resume["file_url"]
    print(f"[i] Using resume: v{resume.get('version')} {resume_url}")

    cand_apps = [
        a for a in apps
        if str(a.get("candidate_id", "")).lower() == candidate_id.lower()
        and a.get("cover_letter_url")
    ]
    cand_apps.sort(key=lambda a: a.get("created_at") or "", reverse=True)
    cover_url = cand_apps[0]["cover_letter_url"] if cand_apps else None
    if cover_url:
        print(f"[i] Cover letter: {cover_url}")
    else:
        print(f"[i] No cover letter found for candidate (scanned {len(apps)} applications)")

    full_name = cand.get("name") or ""
    parts = full_name.strip().split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""

    work_auth = (cand.get("work_auth") or "us_authorized").lower()
    authorized = work_auth in ("us_authorized", "citizen", "green_card", "visa", "ead")

    location_value = (cand.get("location") or "").strip()
    salary_value = (cand.get("salary_expectation") or "").strip()

    profile = {
        "name": full_name,
        "first_name": first,
        "last_name": last,
        "email": cand.get("email") or "",
        "phone": cand.get("phone") or "",
        "location": location_value,
        "linkedin_url": cand.get("linkedin_url") or "",
        "website": cand.get("website") or "",
        "experience_years": str(cand.get("years_exp") or ""),
        "tech_stack": ", ".join(cand.get("tech_stack") or []),
        "current_company": cand.get("current_company") or "",
        "current_title": cand.get("current_title") or "",
        "education": cand.get("education") or "",
        "salary_expectation": salary_value,
        "work_authorization": "Yes" if authorized else "No",
        "sponsorship": "No" if authorized else "Yes",
        "agree_terms": "Yes",
        "willing_to_relocate": "Yes",
        "background_check": "Yes",
        "drug_test": "Yes",
        "referral_source": "LinkedIn",
        "start_date": "Immediately",
    }

    local_resume = await _download_resume(resume_url)
    if local_resume:
        before = profile.get("location", "")
        enrich_profile_from_resume(profile, local_resume)
        after = profile.get("location", "")
        if before != after:
            print(f"[i] Profile location enriched from resume: {before!r} → {after!r}")

    return ApplicationPackage(
        application_id=f"smoke-icims-{uuid.uuid4().hex[:8]}",
        candidate_id=candidate_id,
        job_id=f"smoke-{uuid.uuid4().hex[:8]}",
        job_title=job_title,
        job_description=job_description,
        job_url=job_url,
        platform="icims",
        ats_type="icims",
        company=company,
        resume_url=resume_url,
        cover_letter_url=cover_url,
        candidate_profile=profile,
        screening_answers={},
    )


async def _run(args) -> int:
    from app.browser_automation.services.executor import ApplicationExecutor

    package = await _build_package(
        args.api_base, args.candidate_id, args.job_url,
        args.job_title, args.company, args.job_description,
    )
    print(f"[i] Package built: app_id={package.application_id}")
    print(f"[i] Profile: name={package.candidate_profile['name']!r} "
          f"location={package.candidate_profile['location']!r}")
    print(f"[i] Resume:  {package.resume_url}")
    print(f"[i] Cover:   {package.cover_letter_url!r}")
    print(f"[i] Job URL: {package.job_url}")
    print(f"[i] Platform: {package.platform}")
    print(f"[i] DRY_RUN_NO_SUBMIT={os.getenv('DRY_RUN_NO_SUBMIT')!r}")
    print("-" * 70)

    executor = ApplicationExecutor()
    result = await executor.execute(package, retry_count=0)

    print("-" * 70)
    print(f"[result] status            = {result.status}")
    print(f"[result] error_message     = {result.error_message!r}")
    print(f"[result] screenshot_url    = {result.screenshot_url!r}")
    print(f"[result] confirmation_text = {result.confirmation_text!r}")
    print(f"[result] elapsed_seconds   = {result.execution_time_seconds:.1f}")
    return 0 if result.status in ("SUBMITTED", "FORM_COMPLETED") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate-id",
        help="UUID of the candidate (e.g. Sabih Ahmed). Use --list-candidates to discover.",
    )
    parser.add_argument(
        "--job-url",
        default=_DEFAULT_ICIMS_URL,
        help=f"iCIMS posting URL (default: {_DEFAULT_ICIMS_URL!r})",
    )
    parser.add_argument(
        "--job-title", default=_DEFAULT_JOB_TITLE,
        help=f"Job title (default: {_DEFAULT_JOB_TITLE!r})",
    )
    parser.add_argument(
        "--company", default=_DEFAULT_COMPANY,
        help=f"Company name (default: {_DEFAULT_COMPANY!r})",
    )
    parser.add_argument(
        "--job-description", default=_DEFAULT_JOB_DESC,
        help="Truncated to 1200 chars by AgentLoop. Default uses the reference iCIMS posting.",
    )
    parser.add_argument(
        "--api-base",
        default=os.getenv("M1_API_BASE_URL", "http://localhost:8000/api"),
        help="M1 API base URL (default: $M1_API_BASE_URL or http://localhost:8000/api)",
    )
    parser.add_argument(
        "--list-candidates", action="store_true",
        help="Print all candidates and exit (use to discover the UUID).",
    )
    parser.add_argument(
        "--really-submit", action="store_true",
        help="Actually submit (default: DRY_RUN_NO_SUBMIT=true).",
    )
    args = parser.parse_args()

    _setup_logging()

    # Hard default — never burn a real submission unless explicitly asked.
    if not args.really_submit:
        os.environ["DRY_RUN_NO_SUBMIT"] = "true"
    os.environ.setdefault("USE_AGENT_LOOP", "true")
    os.environ.setdefault("USE_PAGE_AGENT", "true")
    os.environ.setdefault("USE_LLM_FILLER", "true")
    # iCIMS has more fields per page (login + resume + ~10 profile fields) than
    # most ATSes, and step 2 reloads on the same URL — the loop can need
    # ~40 turns. Bump the wall-clock ceiling so an OpenRouter-throttled run
    # has time to finish both steps.
    os.environ.setdefault("AGENT_LOOP_WALL_TIMEOUT_S", "600")

    if args.list_candidates:
        asyncio.run(_list_candidates(args.api_base))
        return 0
    if not args.candidate_id:
        parser.error("--candidate-id is required (or use --list-candidates first)")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
