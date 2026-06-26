"""add_unique_constraint_to_applications

Revision ID: g5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-06-25 19:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'g5b6c7d8e9f0'
down_revision: Union[str, Sequence[str], None] = 'f4a5b6c7d8e9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add unique constraint to applications table on (candidate_id, job_id)
    op.create_unique_constraint('uq_applications_candidate_job', 'applications', ['candidate_id', 'job_id'])


def downgrade() -> None:
    # Remove unique constraint
    op.drop_constraint('uq_applications_candidate_job', 'applications', type_='unique')
