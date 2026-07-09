"""add paused flag to applications

Adds a per-application `paused` flag so an operator can hold a single queued
application from the dashboard. A paused row is skipped by the browser worker
and the stuck-application watchdog, and excluded from the per-candidate
active-count gate, so it stops consuming a worker slot until resumed.

Server default 0 keeps every existing row active. Backfilled explicitly for
safety on large tables.

Revision ID: l0a1b2c3d4e5
Revises: k9f0a1b2c3d4
Create Date: 2026-07-09 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'l0a1b2c3d4e5'
down_revision: Union[str, Sequence[str], None] = 'k9f0a1b2c3d4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'applications',
        sa.Column('paused', sa.Integer(), server_default='0', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('applications', 'paused')
