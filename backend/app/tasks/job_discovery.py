from app.celery_app import celery_app

@celery_app.task(name="task:discover_jobs_all_platforms")
def discover_jobs_all_platforms():
    """Placeholder — Module 2 will implement the real logic."""
    pass