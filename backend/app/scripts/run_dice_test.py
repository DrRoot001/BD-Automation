"""Live Dice Easy Apply test harness.

Self-contained: builds an ApplicationPackage directly and runs the
ApplicationExecutor against a real Dice posting. Does NOT require M1/M3/DB to
be up — status PATCHes fail gracefully (transition_status swallows connection
errors) and the M3 screening call only fires on the scripted-pipeline fallback.

DEFAULT IS DRY RUN: the AgentLoop logs in, clicks Easy Apply, walks the 3-step
wizard, uploads the resume, answers the questions, reaches Review, and STOPS
before clicking the real Submit (returns confirmation='dry_run_stopped_before_submit').
Pass --submit to actually file the application.

Usage (from backend/):
    # Safe validation run (no real submission) — watch the browser:
    python -m app.scripts.run_dice_test

    # Real submission:
    python -m app.scripts.run_dice_test --submit

    # Override location / work-auth / headless:
    python -m app.scripts.run_dice_test --location "Austin, Texas, United States" --work-auth "H1B"

Requires DICE_EMAIL / DICE_PASSWORD in backend/.env.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parent.parent
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(dotenv_path=str(BACKEND_DIR / ".env"))

_DEFAULT_JOB_URL = "https://www.dice.com/job-detail/fa6ea40a-e44c-48ed-954b-4846372bf35f"
# Sabih Haider — matches the accumulating field_memory file.
_DEFAULT_CANDIDATE_ID = "76a9f624-ad21-41ac-bf10-fad936413b75"
# Resume + cover letter both live in Supabase storage (public buckets
# "resume" / "cover_letter"). The executor downloads https URLs (incl. private
# buckets via anon key) the same way production hydration does.
_DEFAULT_RESUME = (
    "https://rdfnydteruigmajebxsm.supabase.co/storage/v1/object/public/"
    "resume/76a9f624-ad21-41ac-bf10-fad936413b75_base_13efab50.pdf"
)
_DEFAULT_COVER_LETTER = (
    "https://rdfnydteruigmajebxsm.supabase.co/storage/v1/object/public/"
    "cover_letter/76a9f624-ad21-41ac-bf10-fad936413b75_50c924d1-daa3-4b4a-a188-6a69e444b28e_cl.pdf"
)


def _configure_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    log_file = os.path.join(BACKEND_DIR, "dice_test.log")
    fileh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fileh.setLevel(logging.DEBUG)
    fileh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(fileh)
    logging.getLogger("run_dice_test").info(f"Debug log → {log_file}")


async def _run(args: argparse.Namespace) -> int:
    logger = logging.getLogger("run_dice_test")

    if not os.getenv("DICE_EMAIL") or not os.getenv("DICE_PASSWORD"):
        logger.error("DICE_EMAIL / DICE_PASSWORD not set in backend/.env — aborting")
        return 2

    # Resume / cover letter may be a local path OR an http(s) URL (the executor
    # downloads URLs, including Supabase buckets, via _resolve_file_to_local_path).
    def _missing(path: str) -> bool:
        return bool(path) and not path.startswith(("http://", "https://")) and not os.path.isfile(path)

    if not args.resume or _missing(args.resume):
        logger.error(f"Resume not found: {args.resume!r}")
        return 2
    cover_letter = args.cover_letter or None
    if cover_letter == "none":
        cover_letter = None
    if cover_letter and _missing(cover_letter):
        logger.warning(f"Cover letter not found ({cover_letter!r}); continuing without it")
        cover_letter = None

    # Dry-run unless --submit. Must be set before executor.execute() reads it.
    os.environ["DRY_RUN_NO_SUBMIT"] = "false" if args.submit else "true"
    os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true" if args.headless else "false")

    from app.browser_automation.services.executor import ApplicationExecutor
    from app.browser_automation.services.models import ApplicationPackage

    # Sabih's profile. work_authorization_type drives the Dice Work Authorization
    # dropdown; location must be a real place so the Google-Places autocomplete
    # returns suggestions.
    candidate_profile = {
        "name": "Sabih Haider",
        "first_name": "Sabih",
        "last_name": "Haider",
        "email": args.email,
        "phone": args.phone,
        "location": args.location,
        "linkedin_url": "",
        "website": "",
        "work_authorization": "Yes",
        "work_authorization_type": args.work_auth,
        "sponsorship": "No",
        "experience_years": "5",
    }

    package = ApplicationPackage(
        application_id=str(uuid4()),
        candidate_id=args.candidate_id,
        job_id=str(uuid4()),
        job_title="(Dice test)",
        job_description="",
        job_url=args.job_url,
        platform="dice",
        ats_type="dice",
        company="",
        resume_url=args.resume,
        cover_letter_url=cover_letter,
        candidate_profile=candidate_profile,
        # Pin work authorization so the AI uses it verbatim instead of guessing.
        # Candidate is a US citizen, authorized to work in the US, no sponsorship.
        screening_answers={
            "Work Authorization": args.work_auth,
            "What is your work authorization status?": args.work_auth,
            "Are you legally authorized to work in the United States?": "Yes",
            "Will you now or in the future require sponsorship for employment visa status?": "No",
        },
    )

    mode = "REAL SUBMIT" if args.submit else "DRY RUN (no submit)"
    print("=" * 70)
    print(f"  DICE EASY APPLY TEST — {mode}")
    print(f"  job_url   : {args.job_url}")
    print(f"  candidate : {args.candidate_id}")
    print(f"  resume    : {args.resume}")
    print(f"  cover_ltr : {cover_letter or '(none)'}")
    print(f"  location  : {args.location}  |  work_auth: {args.work_auth}")
    print(f"  headless  : {os.environ['PLAYWRIGHT_HEADLESS']}")
    print("=" * 70)

    result = await ApplicationExecutor().execute(package)

    print("\n" + "=" * 70)
    print("  RESULT")
    print("=" * 70)
    print(f"  status        : {result.status}")
    print(f"  confirmation  : {result.confirmation_text}")
    print(f"  screenshot    : {result.screenshot_url}")
    print(f"  error         : {result.error_message}")
    print(f"  elapsed (s)   : {result.execution_time_seconds:.1f}")
    print("=" * 70)
    return 0 if result.status in ("SUBMITTED", "FORM_COMPLETED") else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Dice Easy Apply live test")
    parser.add_argument("--job-url", default=_DEFAULT_JOB_URL)
    parser.add_argument("--candidate-id", default=_DEFAULT_CANDIDATE_ID)
    parser.add_argument("--resume", default=_DEFAULT_RESUME)
    parser.add_argument("--cover-letter", default=_DEFAULT_COVER_LETTER,
                        help="Cover letter URL/path from Supabase. Pass 'none' to skip.")
    parser.add_argument("--email", default="sabih0364@gmail.com")
    parser.add_argument("--phone", default="+13025550123")
    parser.add_argument("--location", default="New York, New York, United States")
    parser.add_argument("--work-auth", default="US Citizen",
                        help="One of: US Citizen / Green Card Holder / H1B / OPT / TN Visa / Other")
    parser.add_argument("--submit", action="store_true",
                        help="Actually submit (default is a safe dry run)")
    parser.add_argument("--headless", action="store_true",
                        help="Run Chrome headless (default: visible so you can watch)")
    args = parser.parse_args()

    _configure_logging()
    rc = asyncio.run(_run(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
