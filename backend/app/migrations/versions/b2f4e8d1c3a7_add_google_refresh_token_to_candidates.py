"""add google_refresh_token to candidates

Revision ID: b2f4e8d1c3a7
Revises: a6e7509b4602
Create Date: 2026-06-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2f4e8d1c3a7'
down_revision: Union[str, None] = 'a6e7509b4602'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('candidates', sa.Column('google_refresh_token', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('candidates', 'google_refresh_token')
