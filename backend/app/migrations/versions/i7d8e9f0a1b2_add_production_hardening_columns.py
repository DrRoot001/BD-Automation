"""add_production_hardening_columns

Adds columns referenced in code but missing from the DB, and adds performance
indexes for production workloads.

Uses raw SQL with IF NOT EXISTS so this migration is safe to run multiple times
and won't abort the transaction if a column already exists.

Revision ID: i7d8e9f0a1b2
Revises: h6c7d8e9f0a1
Create Date: 2026-06-26 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'i7d8e9f0a1b2'
down_revision: Union[str, Sequence[str], None] = 'h6c7d8e9f0a1'
branch_labels = None
depends_on = None


def _add_column_if_not_exists(table: str, column: str, col_type: str) -> None:
    """Add a column only if it doesn't already exist (idempotent)."""
    op.execute(
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}"
    )


def upgrade() -> None:
    # ── candidates: extra profile fields used by browser automation ──────────
    _add_column_if_not_exists("candidates", "website", "VARCHAR(500)")
    _add_column_if_not_exists("candidates", "current_company", "VARCHAR(255)")
    _add_column_if_not_exists("candidates", "current_title", "VARCHAR(255)")
    _add_column_if_not_exists("candidates", "education", "TEXT")
    _add_column_if_not_exists("candidates", "salary_expectation", "VARCHAR(100)")

    # ── applications: updated_at (in schema.sql but may be missing from DB) ──
    _add_column_if_not_exists(
        "applications",
        "updated_at",
        "TIMESTAMP WITH TIME ZONE DEFAULT NOW()"
    )

    # ── jobs: ats_type (referenced by browser automation executor) ───────────
    _add_column_if_not_exists("jobs", "ats_type", "VARCHAR(50)")

    # ── Composite index: fast candidate+status filter on applications ─────────
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_applications_candidate_status "
        "ON applications(candidate_id, status)"
    )

    # ── Index on applications.created_at for dashboard ORDER BY queries ──────
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_applications_created_at "
        "ON applications(created_at)"
    )

    # ── HNSW index on jobs.embedding for fast cosine similarity search ───────
    # Requires pgvector >= 0.5.0 + the vector extension.
    # Skip gracefully if not supported (IVFFlat from initial migration is fallback).
    try:
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_jobs_embedding_hnsw "
            "ON jobs USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        )
    except Exception:
        pass  # Fallback: IVFFlat index from initial migration still works


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_jobs_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_applications_created_at")
    op.execute("DROP INDEX IF EXISTS ix_applications_candidate_status")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS ats_type")
    op.execute("ALTER TABLE applications DROP COLUMN IF EXISTS updated_at")
    op.execute("ALTER TABLE candidates DROP COLUMN IF EXISTS salary_expectation")
    op.execute("ALTER TABLE candidates DROP COLUMN IF EXISTS education")
    op.execute("ALTER TABLE candidates DROP COLUMN IF EXISTS current_title")
    op.execute("ALTER TABLE candidates DROP COLUMN IF EXISTS current_company")
    op.execute("ALTER TABLE candidates DROP COLUMN IF EXISTS website")
