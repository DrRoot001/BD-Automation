"""Smoke test for the Remote Rocketship → Greenhouse flow.

Bypasses Celery. Hits M1 directly for the candidate + latest resume / cover
letter URLs, builds an ApplicationPackage, and runs ApplicationExecutor
inline. DRY_RUN_NO_SUBMIT is forced ON unless --really-submit is passed,
so this NEVER fires a real submission by default.

Usage (PowerShell):
    cd C:\\Users\\Rehan\\Desktop\\BD-Automator-Agent\\backend
    python -m scripts.smoke_test_rr --candidate-id <sabih-uuid>

If you don't know Sabih's candidate UUID:
    python -m scripts.smoke_test_rr --list-candidates
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


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy libs
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
    """Download resume to a temp file (mirrors what executor does, but here
    we need the file BEFORE the executor runs so we can enrich the profile)."""
    import os
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
        tmp = os.path.join(tempfile.gettempdir(), f"smoke_{fname}")
        with open(tmp, "wb") as f:
            f.write(resp.content)
        return tmp
    except Exception as exc:
        print(f"[!] Resume download failed (will skip enrichment): {exc}")
        return None


async def _build_package(api_base: str, candidate_id: str, job_url: str):
    """Mirror hydrate_and_execute, minus the application-record lookup."""
    from app.browser_automation.services.models import ApplicationPackage
    from app.browser_automation.services.resume_enricher import enrich_profile_from_resume

    async with httpx.AsyncClient(timeout=30) as client:
        cand_resp = await client.get(f"{api_base}/candidates/{candidate_id}")
        cand_resp.raise_for_status()
        cand = cand_resp.json()

        res_resp = await client.get(f"{api_base}/resumes/{candidate_id}")
        res_resp.raise_for_status()
        resumes = res_resp.json() or []

        # Cover letters live on the `cover_letters` table (one per application),
        # NOT on the candidate record. Pull all applications, filter to this
        # candidate, and grab the most recent application whose
        # `cover_letter_url` is non-null. This matches what production's
        # hydrate_and_execute does when it has an application_id in hand —
        # for the smoke test we just pick "latest cover letter for candidate".
        apps_resp = await client.get(f"{api_base}/applications")
        apps_resp.raise_for_status()
        apps = apps_resp.json() or []

    # Prefer latest tailored, else latest base.
    resumes.sort(key=lambda r: (not r.get("is_base"), r.get("version", 0)), reverse=True)
    resume = next(iter(resumes), None)
    if not resume:
        raise RuntimeError(f"No resume found for candidate {candidate_id}")
    resume_url = resume["file_url"]
    print(f"[i] Using resume: v{resume.get('version')} {resume_url}")

    # Find the latest application FOR THIS CANDIDATE that has a cover_letter_url.
    sabih_apps = [
        a for a in apps
        if str(a.get("candidate_id", "")).lower() == candidate_id.lower()
        and a.get("cover_letter_url")
    ]
    sabih_apps.sort(key=lambda a: a.get("created_at") or "", reverse=True)
    cover_url = sabih_apps[0]["cover_letter_url"] if sabih_apps else None
    if cover_url:
        print(f"[i] Cover letter: {cover_url} (from application {sabih_apps[0].get('id')})")
    else:
        print(f"[i] No cover letter found for this candidate "
              f"(scanned {len(apps)} applications)")

    full_name = cand.get("name") or ""
    parts = full_name.strip().split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""

    work_auth = (cand.get("work_auth") or "us_authorized").lower()
    authorized = work_auth in ("us_authorized", "citizen", "green_card", "visa", "ead")

    # We do NOT hardcode location here. If the DB has a real city/state we
    # use it; if not, the resume enricher (below) reads it straight out of
    # the candidate's resume PDF. This keeps the system candidate-agnostic
    # — different resume, different location, zero code changes.
    location_value = (cand.get("location") or "").strip()

    # Salary: leave the DB value through. If empty, the deterministic policy
    # won't fire and the AI will derive the number from the job description.
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

    # Realistic JD that mirrors what M1 stores in prod. Critically includes
    # the salary range so the AgentLoop's system prompt (which truncates to
    # 1200 chars in executor.py) has the range visible and the AI doesn't
    # have to guess a number. Lifted verbatim from the live dv01 posting.
    job_description = (
        "MLOps Engineer at dv01. We're looking for an MLOps Engineer to build "
        "and operate the platform that gets our machine learning and AI work "
        "into production reliably. You'll own the lifecycle tooling and "
        "infrastructure that lets data science and engineering teams train, "
        "track, deploy, and monitor models. Hands-on senior IC role. Required: "
        "4-7 years MLOps/DevOps/platform-eng experience, MLflow or similar "
        "(W&B, Kubeflow, SageMaker), Kubernetes + Terraform, CI/CD for ML, "
        "Python or Go, PyTorch production deployment, IAM and secrets. "
        "Nice-to-have: GCP, Pulumi, GitHub Actions, vLLM/llama.cpp, MCP. "
        "Salary range $185,000 - $200,000. Remote within the continental USA."
    )

    # Resume-driven enrichment. Downloads the resume PDF, extracts text, and
    # fills profile gaps (currently: location). If the candidate's profile
    # already has a real city/state the enricher leaves it alone.
    local_resume = await _download_resume(resume_url)
    if local_resume:
        before = profile.get("location", "")
        enrich_profile_from_resume(profile, local_resume)
        after = profile.get("location", "")
        if before != after:
            print(f"[i] Profile location enriched from resume: {before!r} → {after!r}")

    return ApplicationPackage(
        application_id=f"smoke-rr-{uuid.uuid4().hex[:8]}",
        candidate_id=candidate_id,
        job_id=f"smoke-{uuid.uuid4().hex[:8]}",
        job_title="MLOps Engineer",
        job_description=job_description,
        job_url=job_url,
        platform="remoterocketship",
        ats_type="remoterocketship",
        company="dv01",
        resume_url=resume_url,
        cover_letter_url=cover_url,
        candidate_profile=profile,
        screening_answers={},
    )


async def _run(args) -> int:
    from app.browser_automation.services.executor import ApplicationExecutor

    package = await _build_package(args.api_base, args.candidate_id, args.job_url)
    print(f"[i] Package built: app_id={package.application_id}")
    print(f"[i] Profile: name={package.candidate_profile['name']!r} "
          f"location={package.candidate_profile['location']!r}")
    print(f"[i] Resume:  {package.resume_url}")
    print(f"[i] Cover:   {package.cover_letter_url!r}")
    print(f"[i] Job URL: {package.job_url}")
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
        help="UUID of the candidate to test with (e.g. Sabih)",
    )
    parser.add_argument(
        "--job-url",
        default="https://www.remoterocketship.com/company/dv01-co/jobs/mlops-engineer-united-states-remote/",
        help="Remote Rocketship job URL (defaults to the dv01 MLOps listing)",
    )
    parser.add_argument(
        "--api-base",
        default=os.getenv("M1_API_BASE_URL", "http://localhost:8000/api"),
        help="M1 API base URL (default: $M1_API_BASE_URL or http://localhost:8000/api)",
    )
    parser.add_argument(
        "--list-candidates", action="store_true",
        help="Print all candidates and exit (use to discover Sabih's UUID)",
    )
    parser.add_argument(
        "--really-submit", action="store_true",
        help="Actually submit (default: DRY_RUN_NO_SUBMIT=true)",
    )
    args = parser.parse_args()

    _setup_logging()

    # Hard default — never burn a real submission unless explicitly asked.
    if not args.really_submit:
        os.environ["DRY_RUN_NO_SUBMIT"] = "true"
    os.environ.setdefault("USE_AGENT_LOOP", "true")
    os.environ.setdefault("USE_PAGE_AGENT", "true")
    os.environ.setdefault("USE_LLM_FILLER", "true")
    # AgentLoop's default 240s wall-clock is too tight when LLM keys are
    # rate-limited (each step pauses through 3-4 fallback keys). 480s gives
    # the loop enough headroom to complete 17 fields under throttled conditions.
    os.environ.setdefault("AGENT_LOOP_WALL_TIMEOUT_S", "480")

    if args.list_candidates:
        asyncio.run(_list_candidates(args.api_base))
        return 0
    if not args.candidate_id:
        parser.error("--candidate-id is required (or use --list-candidates first)")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
