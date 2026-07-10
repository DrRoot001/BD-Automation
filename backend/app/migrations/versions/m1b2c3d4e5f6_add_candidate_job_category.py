"""add job_category to candidates

Jobs already carry a `job_category` (ml / data / salesforce / servicenow /
dynamics) set at scrape time. Mirroring it on the candidate lets matching
hard-scope the pgvector search to the candidate's stack instead of relying on
embedding distance alone, and gives the UI a first-class stack selector.

Also normalizes the existing jobs.job_category values to lowercase in the same
migration (data had mixed 'ML'/'ml', 'Data'/'data' rows) so category equality
is a plain lowercase comparison everywhere.

Revision ID: m1b2c3d4e5f6
Revises: l0a1b2c3d4e5
Create Date: 2026-07-10 07:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'm1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'l0a1b2c3d4e5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'candidates',
        sa.Column('job_category', sa.String(length=100), nullable=True),
    )
    # Normalize existing job categories so matching can compare lowercase.
    op.execute("UPDATE jobs SET job_category = lower(job_category) WHERE job_category IS NOT NULL AND job_category != lower(job_category)")
    op.create_index('ix_jobs_job_category', 'jobs', ['job_category'])


def downgrade() -> None:
    op.drop_index('ix_jobs_job_category', table_name='jobs')
    op.drop_column('candidates', 'job_category')
