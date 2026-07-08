# module2/backfill_embeddings.py
# Standalone embedding backfill for scraped jobs.
#
# The scraper (run_scrape.py) only POSTs jobs; embeddings are normally generated
# by the `task:embed_jobs_batch` Celery task fired at job-creation time.
# When you run the scraper independently (no Celery worker up), those tasks never
# run and jobs sit with embedding IS NULL. This script fills them in on demand —
# no backend server or Celery worker required.
#
# Usage (from repo root, venv active):
#   python -m module2.backfill_embeddings                 # all jobs missing embeddings
#   python -m module2.backfill_embeddings --hours 24      # only jobs scraped in last 24h
#   python -m module2.backfill_embeddings --dry-run       # count only, write nothing
#   python -m module2.backfill_embeddings --batch-size 50 --limit 500

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Make `module2` importable when run directly, and load the backend .env so
# GEMINI_API_KEY / DATABASE_URL resolve exactly as the backend sees them.
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
load_dotenv(ROOT_DIR / "backend" / ".env")

import asyncpg  # noqa: E402

from module2.embedding.generator import (  # noqa: E402
    GEMINI_AVAILABLE,
    OPENAI_AVAILABLE,
    generate_embedding,
)


def _real_provider_available() -> bool:
    """True if a real embedding provider is configured. If not, generate_embedding
    silently returns deterministic PSEUDO vectors — which we must not write to the
    DB unless the user explicitly opts in."""
    if GEMINI_AVAILABLE and os.getenv("GEMINI_API_KEY"):
        return True
    if OPENAI_AVAILABLE and os.getenv("OPENAI_API_KEY"):
        return True
    return False


def _job_text(row: asyncpg.Record) -> str:
    """Match the text formula used by task:embed_jobs_batch so backfilled
    vectors live in the same space as those embedded at creation time."""
    return f"{row['title']} {row['company']} {row['description']} {row['skills']}"


def _to_vector_literal(vec: list[float]) -> str:
    """pgvector text input format, e.g. '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def backfill(hours: int | None, batch_size: int, limit: int | None, dry_run: bool) -> None:
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    # Supabase's pooler runs pgbouncer in transaction mode, which does not support
    # server-side prepared statements — asyncpg's default statement cache trips over
    # this ("prepared statement __asyncpg_stmt_N__ does not exist"). Disable it.
    conn = await asyncpg.connect(url, statement_cache_size=0)
    try:
        where = "embedding IS NULL"
        params: list = []
        if hours is not None:
            where += " AND created_at >= now() - ($1 || ' hours')::interval"
            params.append(str(hours))

        total = await conn.fetchval(f"SELECT count(*) FROM jobs WHERE {where}", *params)
        scope = f"last {hours}h" if hours is not None else "all time"
        print(f"[backfill] {total} job(s) missing embeddings ({scope})")
        if total == 0:
            return

        select_sql = (
            f"SELECT id, title, company, description, skills FROM jobs "
            f"WHERE {where} ORDER BY created_at DESC"
        )
        if limit is not None:
            select_sql += f" LIMIT {int(limit)}"

        rows = await conn.fetch(select_sql, *params)
        print(f"[backfill] processing {len(rows)} job(s)" + (" (DRY RUN)" if dry_run else ""))

        done = 0
        failed = 0
        for start in range(0, len(rows), batch_size):
            chunk = rows[start:start + batch_size]
            texts = [_job_text(r) for r in chunk]
            try:
                vectors = generate_embedding(texts)
            except Exception as exc:  # provider blew up on this batch — skip, keep going
                print(f"[backfill] batch {start}-{start + len(chunk)} failed: {exc}")
                failed += len(chunk)
                continue

            if len(vectors) != len(chunk):
                print(f"[backfill] batch {start}: expected {len(chunk)} vectors, got {len(vectors)} — skipping")
                failed += len(chunk)
                continue

            if dry_run:
                done += len(chunk)
            else:
                async with conn.transaction():
                    for row, vec in zip(chunk, vectors):
                        await conn.execute(
                            "UPDATE jobs SET embedding = $1::vector WHERE id = $2",
                            _to_vector_literal(vec),
                            row["id"],
                        )
                done += len(chunk)

            print(f"[backfill] {done}/{len(rows)} embedded" + (f", {failed} failed" if failed else ""))

        verb = "would embed" if dry_run else "embedded"
        print(f"[backfill] done: {verb} {done}, failed {failed}")
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill pgvector embeddings for scraped jobs.")
    parser.add_argument("--hours", type=int, default=None,
                        help="Only jobs created within the last N hours (default: all).")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="Jobs per embedding API call (default: 50).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap the number of jobs processed this run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be embedded; write nothing.")
    parser.add_argument("--allow-pseudo", action="store_true",
                        help="Permit writing deterministic pseudo-vectors when no real "
                             "embedding provider is configured (NOT recommended).")
    args = parser.parse_args()

    if not _real_provider_available() and not args.allow_pseudo:
        print("[backfill] ERROR: no real embedding provider configured "
              "(set GEMINI_API_KEY or OPENAI_API_KEY). generate_embedding would return "
              "PSEUDO vectors that pollute similarity search. Refusing to write.\n"
              "           Re-run with --allow-pseudo only if you truly want that.")
        sys.exit(1)

    asyncio.run(backfill(args.hours, args.batch_size, args.limit, args.dry_run))


if __name__ == "__main__":
    main()
