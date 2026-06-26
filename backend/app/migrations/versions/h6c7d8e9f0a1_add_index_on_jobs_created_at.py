"""add_index_on_jobs_created_at

Revision ID: h6c7d8e9f0a1
Revises: g5b6c7d8e9f0
Create Date: 2026-06-25 19:56:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'h6c7d8e9f0a1'
down_revision: Union[str, Sequence[str], None] = 'g5b6c7d8e9f0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add index on jobs table for created_at column
    op.create_index('ix_jobs_created_at', 'jobs', ['created_at'], unique=False)


def downgrade() -> None:
    # Remove index
    op.drop_index('ix_jobs_created_at', table_name='jobs')
