"""
SQLAlchemy ORM Models Module.

Defines the database schema for MeetingMind-AI, including Users, Auth Sessions,
Teams, Memberships, Meetings, Transcript Chunks, Action Items (Agent Actions),
Topics, and Team Prompt Customizations.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


# M2M association table linking Meetings and Topics
meeting_topics = Table(
    "meeting_topics",
    Base.metadata,
    Column(
        "meeting_id",
        ForeignKey("meetings.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "topic_id",
        ForeignKey("topics.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class User(Base):
    """User ORM Model.

    Represents registered application users, holding identity credentials (hashed password),
    profile avatar data, created timestamps, and relationships to authentication sessions,
    team memberships, and owned teams.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(
        String(150), unique=True, index=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    photo: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    sessions: Mapped[list[Session]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    memberships: Mapped[list[TeamMembership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    owned_teams: Mapped[list[Team]] = relationship(
        back_populates="owner", foreign_keys="Team.owner_id"
    )


class Session(Base):
    """Auth Session ORM Model.

    Represents active user login sessions authenticated via opaque HTTP cookie tokens.
    Automatically deleted when the associated User is deleted.
    """

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="sessions")


class Team(Base):
    """Team ORM Model.

    Represents organizational teams or workspaces containing members, custom prompt
    overrides, topics, and associated meetings.
    """

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    invite_token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    owner: Mapped[User | None] = relationship(
        back_populates="owned_teams", foreign_keys=[owner_id]
    )
    memberships: Mapped[list[TeamMembership]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )
    topics: Mapped[list[Topic]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )
    meetings: Mapped[list[Meeting]] = relationship(
        back_populates="team", foreign_keys="Meeting.team_id"
    )
    prompt_configs: Mapped[list[TeamPromptConfig]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )


class TeamMembership(Base):
    """Team Membership ORM Model.

    Junction table linking Users to Teams, tracking roles (e.g., owner, admin, member)
    and notification tag preferences for action items.
    """

    __tablename__ = "team_memberships"
    __table_args__ = (UniqueConstraint("user_id", "team_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False, server_default=text("'member'"))

    user: Mapped[User] = relationship(back_populates="memberships")
    team: Mapped[Team] = relationship(back_populates="memberships")


class Meeting(Base):
    """Meeting ORM Model.

    Stores real-time or recorded meeting metadata, execution status ('pending', 'running',
    'completed', 'failed'), JSON summaries, multi-persona debate discussion logs, participant
    speaker lists, and links to transcript chunks and action items.
    """

    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    vexa_meeting_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'pending'")
    )
    summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    discussion_log: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    speakers: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    transcript_chunks: Mapped[list[TranscriptChunk]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    agent_actions: Mapped[list[AgentAction]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    team: Mapped[Team | None] = relationship(
        back_populates="meetings", foreign_keys=[team_id]
    )
    topics: Mapped[list[Topic]] = relationship(
        secondary=meeting_topics, back_populates="meetings"
    )


class TranscriptChunk(Base):
    """Transcript Chunk ORM Model.

    Stores individual speaker audio/text utterances captured during a meeting,
    with exact UTC timestamps and speaker identity.
    """

    __tablename__ = "transcript_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    speaker: Mapped[str] = mapped_column(String(120), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    meeting: Mapped[Meeting] = relationship(back_populates="transcript_chunks")


class AgentAction(Base):
    """Agent Action (Action Item) ORM Model.

    Stores action items extracted by LLM personas or manually created by users,
    including assignee user ID, agent role, action type, description text, status
    ('pending', 'approved', 'rejected'), and tags.
    """

    __tablename__ = "agent_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    agent_role: Mapped[str] = mapped_column(String(120), nullable=False)
    action_type: Mapped[str] = mapped_column(String(120), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'pending'")
    )
    tags: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    meeting: Mapped[Meeting] = relationship(back_populates="agent_actions")
    assignee: Mapped["User"] = relationship(foreign_keys=[assignee_id])


class Topic(Base):
    """Topic ORM Model.

    Represents meeting categorization tags/labels associated with teams,
    including custom badge background colors (HEX).
    """

    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    color: Mapped[str] = mapped_column(
        String(7), nullable=False, server_default=text("'#4f8ef7'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    team: Mapped[Team] = relationship(back_populates="topics")
    meetings: Mapped[list[Meeting]] = relationship(
        secondary=meeting_topics, back_populates="topics"
    )


class TeamPromptConfig(Base):
    """Team Prompt Config ORM Model.

    Stores team-specific prompt overrides for multi-agent synthesis personas
    (e.g., 'tech_lead', 'product_manager', 'scrum_master').
    """

    __tablename__ = "team_prompt_configs"
    __table_args__ = (UniqueConstraint("team_id", "prompt_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prompt_key: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    team: Mapped[Team] = relationship(back_populates="prompt_configs")

