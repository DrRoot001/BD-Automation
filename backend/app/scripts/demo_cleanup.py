"""Demo-data hygiene: remove development-noise applications before a stakeholder demo.

Dry-run by default — prints what would be removed. Pass --apply to execute.

    # from backend/, with PYTHONPATH including the repo root
    python -m app.scripts.demo_cleanup            # dry-run: ad-hoc test-run apps only
    python -m app.scripts.demo_cleanup --infra    # dry-run: also INFRA_ERROR failures
    python -m app.scripts.demo_cleanup --infra --apply

Removes, in scope order:
  1. Applications attached to "(untitled — ad-hoc test run)" placeholder jobs,
     plus those placeholder jobs themselves.
  2. With --infra: FAILED applications whose failure_reason = 'INFRA_ERROR'
     (dev-time infrastructure failures — 800+ rows from the June 2026 test runs).
     Jobs are kept; only the application + history rows go.
"""

import argparse
import asyncio

from sqlalchemy import text

from app.database import task_session

ADHOC_JOB_FILTER = "j.title ILIKE '%ad-hoc test run%' OR j.company ILIKE '%ad-hoc test run%'"


async def run(apply: bool, include_infra: bool) -> None:
    async with task_session() as s:
        adhoc_apps = (await s.execute(text(f"""
            SELECT a.id FROM applications a JOIN jobs j ON a.job_id = j.id
            WHERE {ADHOC_JOB_FILTER}
        """))).scalars().all()
        adhoc_jobs = (await s.execute(text(f"""
            SELECT j.id FROM jobs j WHERE {ADHOC_JOB_FILTER}
        """))).scalars().all()
        print(f"ad-hoc test-run applications: {len(adhoc_apps)}  |  placeholder jobs: {len(adhoc_jobs)}")

        infra_apps: list = []
        if include_infra:
            infra_apps = (await s.execute(text("""
                SELECT id FROM applications
                WHERE status = 'FAILED' AND failure_reason = 'INFRA_ERROR'
            """))).scalars().all()
            print(f"INFRA_ERROR failed applications: {len(infra_apps)}")

        app_ids = list({*adhoc_apps, *infra_apps})
        if not app_ids and not adhoc_jobs:
            print("Nothing to clean.")
            return

        if not apply:
            print("\nDRY RUN — re-run with --apply to delete the rows above.")
            return

        if app_ids:
            await s.execute(
                text("DELETE FROM application_history WHERE application_id = ANY(:ids)"),
                {"ids": app_ids},
            )
            await s.execute(
                text("DELETE FROM emails WHERE application_id = ANY(:ids)"),
                {"ids": app_ids},
            )
            await s.execute(
                text("DELETE FROM interviews WHERE application_id = ANY(:ids)"),
                {"ids": app_ids},
            )
            await s.execute(
                text("DELETE FROM applications WHERE id = ANY(:ids)"),
                {"ids": app_ids},
            )
        if adhoc_jobs:
            await s.execute(text("DELETE FROM jobs WHERE id = ANY(:ids)"), {"ids": adhoc_jobs})
        await s.commit()
        print(f"Deleted {len(app_ids)} applications and {len(adhoc_jobs)} placeholder jobs.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    parser.add_argument("--infra", action="store_true", help="also remove INFRA_ERROR failed applications")
    args = parser.parse_args()
    asyncio.run(run(apply=args.apply, include_infra=args.infra))
