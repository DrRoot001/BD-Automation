"""add_failure_reason_and_candidate_index

Revision ID: e3f4a5b6c7d8
Revises: d1e2f3a4b6c7
Create Date: 2026-06-24 20:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3f4a5b6c7d8'
down_revision: Union[str, Sequence[str], None] = 'd1e2f3a4b6c7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add failure_reason column
    op.add_column('applications', sa.Column('failure_reason', sa.String(length=50), nullable=True))
    
    # Add index on candidate_id
    op.create_index('idx_applications_candidate_id', 'applications', ['candidate_id'], unique=False)


def downgrade() -> None:
    # Remove index
    op.drop_index('idx_applications_candidate_id', table_name='applications')
    
    # Remove failure_reason column
    op.drop_column('applications', 'failure_reason')
