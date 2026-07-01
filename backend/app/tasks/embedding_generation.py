"""Celery task for generating pgvector embeddings for jobs."""

import asyncio
import logging
from app.celery_app import celery_app
from app.models.job import Job

logger = logging.getLogger(__name__)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="task:generate_job_embedding", bind=True, max_retries=3)
def generate_job_embedding(self, job_id: str):
    """Generate pgvector embedding for a job."""

    async def _generate():
        from sqlalchemy import select
        from app.database import task_session  # NullPool — safe across event loops

        try:
            from module2.embedding.embedder import embed_text
        except ImportError:
            logger.error("[Embedding] module2.embedding.embedder not found")
            return False

        async with task_session() as db:
            result = await db.execute(select(Job).where(Job.id == job_id))
            job = result.scalar_one_or_none()
            if not job:
                logger.warning("[Embedding] Job %s not found", job_id)
                return False

            if job.embedding is not None:
                logger.info("[Embedding] Job %s already has embedding", job_id)
                return True

            text_to_embed = f"{job.title} {job.company} {job.description} {job.skills}"

            try:
                embedding = embed_text(text_to_embed)
                job.embedding = embedding
                await db.commit()
                logger.info("[Embedding] Successfully generated embedding for job %s", job_id)
                return True
            except Exception as e:
                logger.error("[Embedding] Failed to generate embedding for job %s: %s", job_id, e)
                raise

    try:
        success = _run(_generate())
        return {"success": success, "job_id": job_id}
    except Exception as exc:
        logger.error("[Embedding] Task failed for job %s: %s", job_id, exc)
        raise self.retry(exc=exc, countdown=30)
