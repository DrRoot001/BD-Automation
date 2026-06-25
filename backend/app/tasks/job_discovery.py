from app.celery_app import celery_app
from app.module2.run_scrape import run_all


@celery_app.task(name="task:discover_jobs_all_platforms")
def discover_jobs_all_platforms():
    """Scrape all configured links (module2/links.py) and post results to /api/jobs."""
    return run_all()