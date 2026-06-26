"""add_automation_controls

Revision ID: j8e9f0a1b2c3
Revises: i7d8e9f0a1b2
Create Date: 2026-06-26 02:44:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'j8e9f0a1b2c3'
down_revision: Union[str, None] = 'i7d8e9f0a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use raw SQL to make operations idempotent
    
    # 1. Add automation_paused
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns 
            WHERE table_name='candidates' AND column_name='automation_paused'
        ) THEN
            ALTER TABLE candidates ADD COLUMN automation_paused INTEGER NOT NULL DEFAULT 0;
        END IF;
    END
    $$;
    """)
    
    # 2. Add max_daily_apps_override
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns 
            WHERE table_name='candidates' AND column_name='max_daily_apps_override'
        ) THEN
            ALTER TABLE candidates ADD COLUMN max_daily_apps_override INTEGER NULL;
        END IF;
    END
    $$;
    """)


def downgrade() -> None:
    op.drop_column('candidates', 'automation_paused')
    op.drop_column('candidates', 'max_daily_apps_override')
