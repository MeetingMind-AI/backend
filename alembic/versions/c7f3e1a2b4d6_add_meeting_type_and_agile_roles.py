"""add_meeting_type_and_agile_roles

Revision ID: c7f3e1a2b4d6
Revises: 909768f18b41
Create Date: 2026-09-10 12:50:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c7f3e1a2b4d6'
down_revision = '909768f18b41'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add meeting_type column to meetings table with default 'general'
    op.add_column(
        'meetings',
        sa.Column('meeting_type', sa.String(length=50), server_default=sa.text("'general'"), nullable=False)
    )

    # 2. Backfill existing team_memberships:
    #    'admin' -> 'scrum_master', 'member' -> 'team_member', team owners -> 'scrum_master'
    op.execute("UPDATE team_memberships SET role = 'scrum_master' WHERE role = 'admin'")
    op.execute("UPDATE team_memberships SET role = 'team_member' WHERE role = 'member'")
    op.execute("""
        UPDATE team_memberships tm
        SET role = 'scrum_master'
        FROM teams t
        WHERE tm.team_id = t.id AND tm.user_id = t.owner_id
    """)

    # 3. Backfill default notification preferences for existing memberships where NULL
    op.execute("""
        UPDATE team_memberships
        SET notification_preferences = '["type:blocker", "type:parking_lot", "type:to_schedule", "type:to_do", "type:insight"]'::jsonb
        WHERE role = 'scrum_master' AND notification_preferences IS NULL
    """)
    op.execute("""
        UPDATE team_memberships
        SET notification_preferences = '["type:insight", "type:to_do", "business"]'::jsonb
        WHERE role = 'product_manager' AND notification_preferences IS NULL
    """)
    op.execute("""
        UPDATE team_memberships
        SET notification_preferences = '["type:to_do", "technical"]'::jsonb
        WHERE (role = 'team_member' OR role IS NULL) AND notification_preferences IS NULL
    """)


def downgrade() -> None:
    # Revert meeting_type column
    op.drop_column('meetings', 'meeting_type')

    # Revert roles
    op.execute("UPDATE team_memberships SET role = 'admin' WHERE role = 'scrum_master'")
    op.execute("UPDATE team_memberships SET role = 'member' WHERE role = 'team_member'")
