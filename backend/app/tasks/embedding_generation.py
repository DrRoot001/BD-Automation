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


# Bound the work per sweep so a single run can't monopolise the worker, and batch
# the embedding calls so a daily discovery backlog drains without recreating the
# rate-limit burst that per-job fire-and-forget tasks cause.
SWEEP_LIMIT = 1000
SWEEP_BATCH_SIZE = 50


async def _embed_job_rows(db, jobs) -> tuple[int, int]:
    """Embed a list of loaded Job rows in batches, committing per batch.

    One Gemini call per SWEEP_BATCH_SIZE-sized chunk instead of one call per job:
    a discovery burst that inserts dozens of jobs costs a couple of batched API
    calls (and a single pooled connection) rather than N fresh connections each
    doing a ~14s single-text embed — which is what starved the Supabase pooler and
    500'd concurrent API requests. Returns (embedded, failed).
    """
    from module2.embedding.generator import generate_embedding

    embedded = 0
    failed = 0
    for start in range(0, len(jobs), SWEEP_BATCH_SIZE):
        chunk = jobs[start:start + SWEEP_BATCH_SIZE]
        texts = [f"{j.title} {j.company} {j.description} {j.skills}" for j in chunk]
        try:
            vectors = generate_embedding(texts)
        except Exception as exc:
            logger.error("[Embedding] batch %d failed: %s", start, exc)
            failed += len(chunk)
            continue

        if len(vectors) != len(chunk):
            logger.error(
                "[Embedding] batch %d: expected %d vectors, got %d — skipping",
                start, len(chunk), len(vectors),
            )
            failed += len(chunk)
            continue

        for job, vec in zip(chunk, vectors):
            job.embedding = vec
        await db.commit()
        embedded += len(chunk)

    return embedded, failed


@celery_app.task(name="task:embed_jobs_batch", bind=True, max_retries=2)
def embed_jobs_batch(self, job_ids):
    """Embed a specific set of newly-created jobs in one batched task.

    Fired once per bulk /api/jobs insert instead of one fire-and-forget task per
    job. Only touches rows still missing an embedding (idempotent if the sweep or
    a concurrent insert already handled some), and skips entirely if no real
    provider is configured so we never persist pseudo vectors.
    """

    async def _run_batch():
        from app.database import task_session  # NullPool — safe across event loops
        from sqlalchemy import select

        if not job_ids:
            return {"embedded": 0}

        if not _real_embedding_provider_available():
            logger.warning(
                "[EmbedBatch] No real embedding provider configured "
                "(GEMINI_API_KEY / OPENAI_API_KEY); skipping to avoid writing pseudo vectors."
            )
            return {"embedded": 0, "skipped": True}

        async with task_session() as db:
            result = await db.execute(
                select(Job).where(Job.id.in_(job_ids), Job.embedding.is_(None))
            )
            jobs = result.scalars().all()
            if not jobs:
                return {"embedded": 0}

            logger.info("[EmbedBatch] Embedding %d newly-created job(s)", len(jobs))
            embedded, failed = await _embed_job_rows(db, jobs)

        logger.info("[EmbedBatch] Done: embedded %d, failed %d", embedded, failed)
        return {"embedded": embedded, "failed": failed}

    try:
        return _run(_run_batch())
    except Exception as exc:
        logger.error("[EmbedBatch] Task failed: %s", exc)
        raise self.retry(exc=exc, countdown=30)


@celery_app.task(name="task:generate_job_embedding", bind=True, max_retries=3)
def generate_job_embedding(self, job_id: str):
    """Backward-compatible single-job embedding — superseded by embed_jobs_batch.

    Kept REGISTERED (not deleted) because producers on other machines share this
    Redis broker and may still enqueue this task name. If the worker doesn't
    recognise it, Celery discards the message as 'unregistered' and the job's
    embedding is silently dropped until the 15-min sweep backfills it. Delegates
    to the same batched, provider-guarded path as embed_jobs_batch.
    """

    async def _generate():
        from app.database import task_session  # NullPool — safe across event loops
        from sqlalchemy import select

        if not _real_embedding_provider_available():
            logger.warning(
                "[Embedding] No real embedding provider configured; skipping job %s "
                "(sweep will backfill).", job_id,
            )
            return {"success": False, "skipped": True, "job_id": job_id}

        async with task_session() as db:
            result = await db.execute(
                select(Job).where(Job.id == job_id, Job.embedding.is_(None))
            )
            job = result.scalar_one_or_none()
            if job is None:
                # Not found, or already embedded — nothing to do.
                return {"success": True, "job_id": job_id}

            embedded, _failed = await _embed_job_rows(db, [job])
            return {"success": embedded == 1, "job_id": job_id}

    try:
        return _run(_generate())
    except Exception as exc:
        logger.error("[Embedding] Task failed for job %s: %s", job_id, exc)
        raise self.retry(exc=exc, countdown=30)


def _real_embedding_provider_available() -> bool:
    """True if a real embedding provider is configured. If not, generate_embedding
    silently returns deterministic PSEUDO vectors — which we must never persist, as
    they pollute cosine-similarity matching. Skip the sweep instead."""
    import os

    from module2.embedding.generator import GEMINI_AVAILABLE, OPENAI_AVAILABLE

    if GEMINI_AVAILABLE and os.getenv("GEMINI_API_KEY"):
        return True
    if OPENAI_AVAILABLE and os.getenv("OPENAI_API_KEY"):
        return True
    return False


@celery_app.task(name="task:sweep_missing_job_embeddings", bind=True, max_retries=0)
def sweep_missing_job_embeddings(self):
    """Self-heal: embed any jobs left with embedding IS NULL.

    The per-insert `embed_jobs_batch` task has limited retries; a Gemini
    rate-limit during a discovery burst (or a worker blip) can drop embeddings,
    silently excluding those jobs from resume↔job matching. This periodic sweep
    (see beat_schedule) re-embeds them in batches so a transient failure only
    delays embedding by one cycle instead of losing it.
    """

    async def _sweep():
        from importlib.util import find_spec

        from app.database import task_session
        from sqlalchemy import select

        if find_spec("module2.embedding.generator") is None:
            logger.error("[EmbeddingSweep] module2.embedding.generator not found")
            return {"embedded": 0, "skipped": True}

        if not _real_embedding_provider_available():
            logger.warning(
                "[EmbeddingSweep] No real embedding provider configured "
                "(GEMINI_API_KEY / OPENAI_API_KEY); skipping to avoid writing pseudo vectors."
            )
            return {"embedded": 0, "skipped": True}

        async with task_session() as db:
            result = await db.execute(
                select(Job).where(Job.embedding.is_(None)).limit(SWEEP_LIMIT)
            )
            jobs = result.scalars().all()
            if not jobs:
                return {"embedded": 0, "missing": 0}

            logger.info("[EmbeddingSweep] Found %d job(s) missing embeddings", len(jobs))
            embedded, failed = await _embed_job_rows(db, jobs)

        logger.info("[EmbeddingSweep] Done: embedded %d, failed %d", embedded, failed)
        return {"embedded": embedded, "failed": failed}

    return _run(_sweep())
