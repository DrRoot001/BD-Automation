import asyncio

from app.database import task_session
from app.models.application import Application
from app.models.application_history import ApplicationHistory
from app.models.job import Job
from app.models.resume import Resume
from app.tasks.browser_automation import execute_application
from sqlalchemy import select


async def main():
    async with task_session() as s:
        res = await s.execute(
            select(Application)
            .where(
                Application.candidate_id == 'b5de7447-d15c-41f7-b778-15aaa87ad4fc',
                Application.status.in_(['BLOCKED', 'FAILED'])
            )
        )
        apps = res.scalars().all()
        print(f"Found {len(apps)} blocked/failed applications to retry.")

        for app in apps:
            job = await s.get(Job, app.job_id)
            if not job:
                continue

            # Retrieve tailored resume URL
            resume_url = ""
            if app.resume_id:
                resume = await s.get(Resume, app.resume_id)
                if resume:
                    resume_url = resume.file_url

            # Fetch screening answers from history
            screening_answers = {}
            history_stmt = (
                select(ApplicationHistory)
                .where(
                    ApplicationHistory.application_id == app.id,
                    ApplicationHistory.to_status == "QUEUED"
                )
                .order_by(ApplicationHistory.created_at.desc())
                .limit(1)
            )
            history_rec = (await s.execute(history_stmt)).scalars().first()
            if history_rec:
                meta = getattr(history_rec, "meta_data", None) or getattr(history_rec, "metadata", None)
                if isinstance(meta, dict):
                    screening_answers = meta.get("screening_answers") or {}

            old_status = app.status

            # Reset application status and details in DB
            app.status = "QUEUED"
            app.retry_count = 0
            app.error_message = None
            app.failure_reason = None

            # Write history
            history = ApplicationHistory(
                application_id=app.id,
                from_status=old_status,
                to_status="QUEUED",
                meta_data={"screening_answers": screening_answers, "info": "Retry triggered automatically by agent"}
            )
            s.add(history)
            await s.commit()

            # Dispatch celery task
            package = {
                "application_id": str(app.id),
                "candidate_id": str(app.candidate_id),
                "job_id": str(app.job_id),
                "job_url": job.source_url or "",
                "platform": (job.source or "").lower(),
                "ats_type": job.job_type or "",
                "resume_url": resume_url,
                "cover_letter_url": app.cover_letter_url or "",
                "screening_answers": screening_answers
            }
            execute_application.apply_async(args=[package], queue="queue:application_execution")
            print(f"Retried application {app.id} for job {job.title} ({job.source})")

if __name__ == '__main__':
    asyncio.run(main())
