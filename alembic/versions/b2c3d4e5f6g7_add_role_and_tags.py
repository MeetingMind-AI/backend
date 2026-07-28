"""add role and tags

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-27 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6g7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Apply schema migration: add role and notification_tags to team_memberships, and tags to agent_actions."""
    op.add_column('team_memberships', sa.Column('role', sa.String(length=50), server_default=sa.text("'member'"), nullable=False))
    op.add_column('team_memberships', sa.Column('notification_tags', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('agent_actions', sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    """Revert schema migration: drop tags, notification_tags, and role columns."""
    op.drop_column('agent_actions', 'tags')
    op.drop_column('team_memberships', 'notification_tags')
    op.drop_column('team_memberships', 'role')

