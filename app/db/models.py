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


# M2M association table — no ORM class needed
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


class TeamMembership(Base):
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

    user: Mapped[User] = relationship(back_populates="memberships")
    team: Mapped[Team] = relationship(back_populates="memberships")


class Meeting(Base):
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
    __tablename__ = "agent_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_role: Mapped[str] = mapped_column(String(120), nullable=False)
    action_type: Mapped[str] = mapped_column(String(120), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'pending'")
    )

    meeting: Mapped[Meeting] = relationship(back_populates="agent_actions")


class Topic(Base):
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
