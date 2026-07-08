"""add_index_on_application_history_app_created

Speeds up the dashboard applications endpoint: its per-row LATERAL subquery does
`WHERE application_id = :id ORDER BY created_at DESC LIMIT 1` for every returned
application. Without an index on application_history.application_id (an FK does
NOT create one in Postgres) each of those was a sequential scan.

Revision ID: k9f0a1b2c3d4
Revises: j8e9f0a1b2c3
Create Date: 2026-07-09 02:20:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'k9f0a1b2c3d4'
down_revision: Union[str, Sequence[str], None] = '2b1480d6a3fd'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Composite (application_id, created_at); Postgres scans backward for DESC.
    op.create_index(
        'ix_application_history_app_created',
        'application_history',
        ['application_id', 'created_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_application_history_app_created', table_name='application_history')
