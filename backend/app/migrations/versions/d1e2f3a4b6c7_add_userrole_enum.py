"""add_userrole_enum

Revision ID: d1e2f3a4b6c7
Revises: c8a1f3d4b5e6
Create Date: 2026-06-23 00:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b6c7'
down_revision: Union[str, Sequence[str], None] = 'c8a1f3d4b5e6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # create enum type
    op.execute("CREATE TYPE userrole AS ENUM ('admin', 'bd_user')")

    # alter users.role from string to enum
    # Convert existing text values to the new enum using an explicit cast.
    # This avoids the DatatypeMismatchError when existing rows use plain text.
    op.alter_column(
        'users',
        'role',
        existing_type=sa.String(),
        type_=sa.Enum('admin', 'bd_user', name='userrole'),
        postgresql_using="role::userrole",
        server_default=sa.text("'bd_user'"),
        existing_nullable=False,
    )


def downgrade() -> None:
    # revert column to text
    op.alter_column(
        'users',
        'role',
        existing_type=sa.Enum('admin', 'bd_user', name='userrole'),
        type_=sa.String(length=50),
        existing_nullable=False,
    )
    # drop enum type
    op.execute('DROP TYPE IF EXISTS userrole')
