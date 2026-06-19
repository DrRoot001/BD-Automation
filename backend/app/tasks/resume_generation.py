import asyncio
from app.celery_app import celery_app
from module3.orchestrator import orchestrate_application_package

@celery_app.task(name="task:prepare_application_package")
def prepare_application_package(candidate_id: str, job_id: str):
    """
    Celery task that triggers the Module 3 orchestration pipeline for a candidate and job.
    Since orchestrate_application_package is async, we run it using asyncio.
    """
    print(f"[CELERY] task:prepare_application_package started for candidate={candidate_id}, job={job_id}")
    
    # Run the async orchestrator synchronously within the Celery worker
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(
        orchestrate_application_package(
            candidate_id=str(candidate_id),
            job_id=str(job_id)
        )
    )
    
    print(f"[CELERY] task:prepare_application_package completed for candidate={candidate_id}, job={job_id}")
    return result
