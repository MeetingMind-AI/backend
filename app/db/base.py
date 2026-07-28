"""
Database Models Aggregator Module for Alembic and SQLAlchemy.

Imports all ORM models and declarative Base class to ensure model metadata
is fully registered before running Alembic migrations or schema creation.
"""

from app.db.models import (  # noqa: F401
    AgentAction,
    Meeting,
    Session,
    Team,
    TeamMembership,
    Topic,
    TranscriptChunk,
    User,
    meeting_topics,
)
from app.db.session import Base

__all__ = [
    "Base",
    "AgentAction",
    "Meeting",
    "Session",
    "Team",
    "TeamMembership",
    "Topic",
    "TranscriptChunk",
    "User",
    "meeting_topics",
]
