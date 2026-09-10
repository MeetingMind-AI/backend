"""
Teams, Memberships, Topics, and Prompt Customization REST API Module.

Manages team lifecycle operations including team creation, membership invites,
role-based access control, topic tagging, and custom persona prompt overrides.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import get_current_user_id
from app.db.models import Meeting, Team, TeamMembership, TeamPromptConfig, Topic, User, meeting_topics
from app.db.session import SessionLocal
from app.engine.prompts import PROMPT_DEFAULTS, PROMPT_READONLY_KEYS

router = APIRouter(tags=["teams"])

# Valid agile role identifiers accepted by the role update endpoint.
# 'admin' and 'member' are accepted as legacy aliases but stored/returned
# as 'scrum_master' / 'team_member' respectively.
AGILE_ROLES = {"scrum_master", "product_manager", "team_member"}

# Default notification preference tokens assigned when a member joins or changes role.
# Token format: "<dimension>:<value>" where dimension is one of:
#   type    — action category (e.g. 'type:blocker', 'type:to_do', 'type:insight')
#   business — business-context filter (e.g. 'business' enables business items)
#   technical — technical-context filter
# A token suffixed with ':off' (e.g. 'type:blocker:off') is an explicit suppression.
# Absence of positive type: tokens places the list in allowlist mode (see isNotificationTypeActive).
DEFAULT_ROLE_PREFERENCES: dict[str, list[str]] = {
    "scrum_master": ["type:blocker", "type:parking_lot", "type:to_schedule", "type:to_do", "type:insight"],
    "product_manager": ["type:insight", "type:to_do", "business"],
    "team_member": ["type:to_do", "technical"],
}


# ── helpers ──────────────────────────────────────────────────────────────────


def _assert_member(db, user_id: int, team_id: int) -> None:
    """Verify that a user is a member of the specified team.

    Args:
        db: Active database session.
        user_id (int): User ID to check.
        team_id (int): Team ID to check.

    Raises:
        HTTPException: HTTP 403 Forbidden if user is not a team member.
    """
    row = db.execute(
        select(TeamMembership).where(
            TeamMembership.user_id == user_id,
            TeamMembership.team_id == team_id,
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=403, detail="Not a member of this team")


def _assert_owner(db, user_id: int, team_id: int) -> Team:
    """Verify that a user is the owner of the specified team.

    Args:
        db: Active database session.
        user_id (int): User ID to check.
        team_id (int): Team ID to check.

    Returns:
        Team: Team ORM model instance if user is owner.

    Raises:
        HTTPException: 404 if team not found, 403 if user is not the team owner.
    """
    team = db.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    if team.owner_id != user_id:
        raise HTTPException(status_code=403, detail="Only the team owner can do this")
    return team


def _member_out(user: User, membership: TeamMembership) -> dict[str, Any]:
    """Format User and TeamMembership ORM models into a public JSON object.

    Args:
        user (User): User model instance.
        membership (TeamMembership): Membership model instance.

    Returns:
        dict[str, Any]: Formatted member attributes dictionary.
    """
    role = membership.role or "team_member"
    prefs = membership.notification_preferences
    if prefs is None:
        role_key = role if role in DEFAULT_ROLE_PREFERENCES else ("scrum_master" if role == "admin" else "team_member")
        prefs = list(DEFAULT_ROLE_PREFERENCES.get(role_key, DEFAULT_ROLE_PREFERENCES["team_member"]))
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "photo_url": f"/api/auth/photo/{user.id}" if user.photo else None,
        "role": role,
        "notification_preferences": prefs,
    }


def _topic_out(topic: Topic) -> dict[str, Any]:
    """Format Topic ORM model into a JSON object.

    Args:
        topic (Topic): Topic model instance.

    Returns:
        dict[str, Any]: Formatted topic dictionary.
    """
    return {
        "id": topic.id,
        "name": topic.name,
        "color": topic.color,
    }


# ── teams ─────────────────────────────────────────────────────────────────────


class TeamCreateRequest(BaseModel):
    """Team Creation Request Schema.

    Attributes:
        name (str): Name of the new team.
    """
    name: str


class TeamUpdateRequest(BaseModel):
    """Team Update Request Schema.

    Attributes:
        name (str): New name for the team.
    """
    name: str


class TransferOwnershipRequest(BaseModel):
    """Transfer Team Ownership Request Schema.

    Attributes:
        new_owner_id (int): User ID of the new team owner.
    """
    new_owner_id: int


@router.get("/api/teams")
def list_teams(user_id: int = Depends(get_current_user_id)) -> dict[str, Any]:
    """List all teams that the current authenticated user belongs to.

    Args:
        user_id (int): Primary key ID of authenticated user.

    Returns:
        dict[str, Any]: Dictionary containing list of teams with member counts.
    """
    with SessionLocal() as db:
        memberships = db.execute(
            select(TeamMembership).where(TeamMembership.user_id == user_id)
        ).scalars().all()

        result = []
        for m in memberships:
            team = db.get(Team, m.team_id)
            if not team:
                continue
            member_count = db.execute(
                select(TeamMembership).where(TeamMembership.team_id == team.id)
            ).scalars().all().__len__()
            result.append({
                "id": team.id,
                "name": team.name,
                "owner_id": team.owner_id,
                "is_owner": team.owner_id == user_id,
                "member_count": member_count,
                "created_at": team.created_at.isoformat(),
            })
        return {"teams": result}


@router.post("/api/teams")
def create_team(
    req: TeamCreateRequest, user_id: int = Depends(get_current_user_id)
) -> dict[str, Any]:
    """Create a new team workspace and designate creator as owner and member.

    Args:
        req (TeamCreateRequest): Request body with team name.
        user_id (int): Creator's user ID from auth dependency.

    Returns:
        dict[str, Any]: Dictionary describing newly created team.
    """
    with SessionLocal() as db:
        team = Team(
            name=req.name.strip(),
            owner_id=user_id,
            # A random 32-byte (64-char hex) token uniquely identifies the team's
            # shareable invite link.  The token is stable for the team's lifetime
            # and does not expire, so the owner can share the link at any time.
            invite_token=uuid.uuid4().hex,
        )
        db.add(team)
        db.flush()
        db.add(TeamMembership(
            user_id=user_id,
            team_id=team.id,
            role="scrum_master",
            notification_preferences=list(DEFAULT_ROLE_PREFERENCES["scrum_master"]),
        ))
        db.commit()
        db.refresh(team)
        return {
            "id": team.id,
            "name": team.name,
            "owner_id": team.owner_id,
            "is_owner": True,
            "member_count": 1,
            "created_at": team.created_at.isoformat(),
        }


@router.get("/api/teams/{team_id}")
def get_team(
    team_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, Any]:
    """Retrieve detailed information for a specific team.

    Args:
        team_id (int): Target team primary key ID.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Detailed team dictionary including member list and topics.

    Raises:
        HTTPException: 403 if not a member, 404 if team not found.
    """
    with SessionLocal() as db:
        _assert_member(db, user_id, team_id)
        team = db.get(Team, team_id)
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")

        memberships = db.execute(
            select(TeamMembership).where(TeamMembership.team_id == team_id)
        ).scalars().all()
        members = []
        for m in memberships:
            u = db.get(User, m.user_id)
            if u:
                members.append({**_member_out(u, m), "is_owner": u.id == team.owner_id})

        topics = db.execute(
            select(Topic).where(Topic.team_id == team_id)
        ).scalars().all()

        return {
            "id": team.id,
            "name": team.name,
            "owner_id": team.owner_id,
            "is_owner": team.owner_id == user_id,
            "invite_token": team.invite_token,
            "members": members,
            "topics": [_topic_out(t) for t in topics],
        }


@router.patch("/api/teams/{team_id}")
def update_team(
    team_id: int,
    req: TeamUpdateRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Update team metadata (only accessible by team owner).

    Args:
        team_id (int): Target team primary key ID.
        req (TeamUpdateRequest): Updated team settings payload.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Updated team attributes.

    Raises:
        HTTPException: 403 if user is not team owner.
    """
    with SessionLocal() as db:
        team = _assert_owner(db, user_id, team_id)
        team.name = req.name.strip()
        db.commit()
        return {"id": team.id, "name": team.name}


@router.post("/api/teams/{team_id}/leave")
def leave_team(
    team_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, Any]:
    """Remove authenticated user from team membership.

    Args:
        team_id (int): Target team primary key ID.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Success confirmation `{"ok": True}`.

    Raises:
        HTTPException: 400 if team owner tries to leave without transferring ownership.
    """
    with SessionLocal() as db:
        _assert_member(db, user_id, team_id)
        team = db.get(Team, team_id)
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        if team.owner_id == user_id:
            raise HTTPException(
                status_code=400,
                detail="Team owners cannot leave. Transfer ownership or delete the team first.",
            )
        membership = db.execute(
            select(TeamMembership).where(
                TeamMembership.user_id == user_id,
                TeamMembership.team_id == team_id,
            )
        ).scalar_one_or_none()
        if membership:
            db.delete(membership)
            db.commit()
        return {"ok": True}


@router.post("/api/teams/{team_id}/transfer-ownership")
def transfer_team_ownership(
    team_id: int,
    req: TransferOwnershipRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Transfer ownership of a team to another existing member.

    Args:
        team_id (int): Target team primary key ID.
        req (TransferOwnershipRequest): Payload with target new owner's user ID.
        user_id (int): Authenticated user ID (must be current owner).

    Returns:
        dict[str, Any]: Success response with updated owner_id.

    Raises:
        HTTPException: 403 if caller is not owner, 400 if self-transfer or target not in team.
    """
    with SessionLocal() as db:
        team = _assert_owner(db, user_id, team_id)
        if req.new_owner_id == user_id:
            raise HTTPException(
                status_code=400, detail="You are already the owner of this team"
            )

        new_owner_membership = db.execute(
            select(TeamMembership).where(
                TeamMembership.user_id == req.new_owner_id,
                TeamMembership.team_id == team_id,
            )
        ).scalar_one_or_none()
        if not new_owner_membership:
            raise HTTPException(
                status_code=400, detail="The selected user must be a member of the team"
            )

        team.owner_id = req.new_owner_id
        db.commit()
        return {"ok": True, "team_id": team.id, "owner_id": team.owner_id}


@router.delete("/api/teams/{team_id}")
def delete_team(
    team_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Permanently delete a team and all associated meetings, topics, and memberships.

    Args:
        team_id (int): Target team primary key ID.
        user_id (int): Authenticated user ID (must be team owner).

    Returns:
        dict[str, Any]: Success confirmation.

    Raises:
        HTTPException: 403 if caller is not owner, 404 if team not found.
    """
    with SessionLocal() as db:
        team = _assert_owner(db, user_id, team_id)

        # Explicitly delete all meetings associated with this team so their
        # transcript_chunks and agent_actions cascade cleanly.
        meetings = db.execute(
            select(Meeting).where(Meeting.team_id == team_id)
        ).scalars().all()
        for m in meetings:
            db.delete(m)

        db.delete(team)
        db.commit()
        return {"ok": True}


@router.get("/api/teams/{team_id}/invite")
def get_invite_link(
    team_id: int,
    request: Request,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Generate or fetch team invite link (only accessible by team owner).

    Args:
        team_id (int): Target team primary key ID.
        request (Request): HTTP request context for base URL resolution.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Dictionary containing full invite URL and token.

    Raises:
        HTTPException: 403 if user is not team owner.
    """
    with SessionLocal() as db:
        _assert_member(db, user_id, team_id)
        team = db.get(Team, team_id)
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        if team.owner_id != user_id:
            raise HTTPException(
                status_code=403, detail="Only the team owner can generate invite links"
            )
        base = str(request.base_url).rstrip("/")
        invite_url = f"{base}/join/{team.invite_token}"
        return {"invite_url": invite_url, "invite_token": team.invite_token}


@router.post("/api/teams/join/{invite_token}")
def join_team(
    invite_token: str, user_id: int = Depends(get_current_user_id)
) -> dict[str, Any]:
    """Join a team using a unique invite token.

    Args:
        invite_token (str): 64-character hex invite token string.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Dictionary with team_id and team_name.

    Raises:
        HTTPException: 404 if invite token is invalid.
    """
    with SessionLocal() as db:
        team = db.execute(
            select(Team).where(Team.invite_token == invite_token)
        ).scalar_one_or_none()
        if not team:
            raise HTTPException(status_code=404, detail="Invalid invite link")

        already = db.execute(
            select(TeamMembership).where(
                TeamMembership.user_id == user_id,
                TeamMembership.team_id == team.id,
            )
        ).scalar_one_or_none()
        if not already:
            db.add(TeamMembership(
                user_id=user_id,
                team_id=team.id,
                role="team_member",
                notification_preferences=list(DEFAULT_ROLE_PREFERENCES["team_member"]),
            ))
            db.commit()
        return {"team_id": team.id, "team_name": team.name}


@router.get("/api/teams/{team_id}/members")
def list_members(
    team_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, Any]:
    """List all members belonging to a team.

    Args:
        team_id (int): Target team primary key ID.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Dictionary containing list of member objects.
    """
    with SessionLocal() as db:
        _assert_member(db, user_id, team_id)
        team = db.get(Team, team_id)
        memberships = db.execute(
            select(TeamMembership).where(TeamMembership.team_id == team_id)
        ).scalars().all()
        members = []
        for m in memberships:
            u = db.get(User, m.user_id)
            if u:
                members.append({
                    **_member_out(u, m),
                    "is_owner": u.id == (team.owner_id if team else None),
                })
        return {"members": members}


@router.delete("/api/teams/{team_id}/members/{target_user_id}")
def kick_member(
    team_id: int,
    target_user_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Remove a member from a team (only accessible by team owner).

    Args:
        team_id (int): Target team primary key ID.
        target_user_id (int): User ID to kick from team.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Confirmation `{"ok": True}`.

    Raises:
        HTTPException: 400 if owner tries to kick self, 403 if caller is not owner.
    """
    with SessionLocal() as db:
        _assert_owner(db, user_id, team_id)
        if target_user_id == user_id:
            raise HTTPException(status_code=400, detail="Cannot kick yourself")
        membership = db.execute(
            select(TeamMembership).where(
                TeamMembership.user_id == target_user_id,
                TeamMembership.team_id == team_id,
            )
        ).scalar_one_or_none()
        if not membership:
            raise HTTPException(status_code=404, detail="Member not found")
        db.delete(membership)
        db.commit()
        return {"ok": True}


class TeamMemberUpdate(BaseModel):
    """Payload for updating a member's role or preferences."""
    role: str | None = None
    notification_preferences: list[str] | None = None


@router.patch("/api/teams/{team_id}/members/{target_user_id}")
def update_member(
    team_id: int,
    target_user_id: int,
    req: TeamMemberUpdate,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Update team member role or notification preferences.

    Args:
        team_id (int): Target team primary key ID.
        target_user_id (int): User ID of member being updated.
        req (TeamMemberUpdate): Payload containing role and/or notification preferences.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Updated member object.

    Raises:
        HTTPException: 403 if non-owner attempts to update role or someone else's preferences.
    """
    with SessionLocal() as db:
        _assert_member(db, user_id, team_id)
        team = db.get(Team, team_id)
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")

        is_owner = team.owner_id == user_id
        is_self = target_user_id == user_id

        # Only owners can change roles
        if req.role is not None:
            if not is_owner:
                raise HTTPException(status_code=403, detail="Only team owners can change roles")
            if req.role not in AGILE_ROLES and req.role not in {"admin", "member"}:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid role. Must be one of: {', '.join(sorted(AGILE_ROLES))}",
                )

        # Users can update their own notification preferences; owners can update any member's preferences
        if req.notification_preferences is not None:
            if not is_owner and not is_self:
                raise HTTPException(
                    status_code=403,
                    detail="Only the team owner can change notification preferences for other members",
                )

        membership = db.execute(
            select(TeamMembership).where(
                TeamMembership.user_id == target_user_id,
                TeamMembership.team_id == team_id,
            )
        ).scalar_one_or_none()

        if not membership:
            raise HTTPException(status_code=404, detail="Member not found")

        if req.role is not None:
            membership.role = req.role
            # Automatically refresh default notification preferences if not explicitly overridden
            if req.notification_preferences is None:
                role_key = (
                    req.role
                    if req.role in DEFAULT_ROLE_PREFERENCES
                    else ("scrum_master" if req.role == "admin" else "team_member")
                )
                membership.notification_preferences = list(
                    DEFAULT_ROLE_PREFERENCES.get(role_key, [])
                )

        if req.notification_preferences is not None:
            membership.notification_preferences = req.notification_preferences

        db.commit()

        u = db.get(User, target_user_id)
        return {**_member_out(u, membership), "is_owner": u.id == team.owner_id}


# ── topics ────────────────────────────────────────────────────────────────────


class TopicCreateRequest(BaseModel):
    """Topic Creation Request Schema.

    Attributes:
        name (str): Topic title.
        color (str): HEX color code string (default "#4f8ef7").
    """
    name: str
    color: str = "#4f8ef7"


class TopicUpdateRequest(BaseModel):
    """Topic Update Request Schema.

    Attributes:
        name (str | None): Optional new topic title.
        color (str | None): Optional new HEX color string.
    """
    name: str | None = None
    color: str | None = None


@router.get("/api/teams/{team_id}/topics")
def list_topics(
    team_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, Any]:
    """List all topic categories defined for a team.

    Args:
        team_id (int): Target team primary key ID.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Dictionary containing topic list.
    """
    with SessionLocal() as db:
        _assert_member(db, user_id, team_id)
        topics = db.execute(
            select(Topic).where(Topic.team_id == team_id)
        ).scalars().all()
        return {"topics": [_topic_out(t) for t in topics]}


@router.post("/api/teams/{team_id}/topics")
def create_topic(
    team_id: int,
    req: TopicCreateRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Create a new topic category for a team.

    Args:
        team_id (int): Target team primary key ID.
        req (TopicCreateRequest): Payload with topic name and color.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Newly created topic dictionary.
    """
    with SessionLocal() as db:
        _assert_member(db, user_id, team_id)
        topic = Topic(team_id=team_id, name=req.name.strip(), color=req.color)
        db.add(topic)
        db.commit()
        db.refresh(topic)
        return _topic_out(topic)


@router.patch("/api/teams/{team_id}/topics/{topic_id}")
def update_topic(
    team_id: int,
    topic_id: int,
    req: TopicUpdateRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Update topic name or badge color.

    Args:
        team_id (int): Target team primary key ID.
        topic_id (int): Target topic primary key ID.
        req (TopicUpdateRequest): Updated topic fields.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Updated topic dictionary.
    """
    with SessionLocal() as db:
        _assert_member(db, user_id, team_id)
        topic = db.get(Topic, topic_id)
        if not topic or topic.team_id != team_id:
            raise HTTPException(status_code=404, detail="Topic not found")
        if req.name is not None:
            topic.name = req.name.strip()
        if req.color is not None:
            topic.color = req.color
        db.commit()
        return _topic_out(topic)


@router.delete("/api/teams/{team_id}/topics/{topic_id}")
def delete_topic(
    team_id: int,
    topic_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Delete a topic from a team.

    Args:
        team_id (int): Target team primary key ID.
        topic_id (int): Target topic primary key ID.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Success confirmation `{"ok": True}`.
    """
    with SessionLocal() as db:
        _assert_member(db, user_id, team_id)
        topic = db.get(Topic, topic_id)
        if not topic or topic.team_id != team_id:
            raise HTTPException(status_code=404, detail="Topic not found")
        db.delete(topic)
        db.commit()
        return {"ok": True}


# ── prompts ───────────────────────────────────────────────────────────────────

VALID_PROMPT_KEYS = frozenset(PROMPT_DEFAULTS.keys())

PROMPT_LABELS: dict[str, str] = {
    "realtime_scrum_master": "Real-time Monitor",
    "realtime_user": "Real-time: User Prompt",
    "final_tech_lead": "Final Report: Tech Lead",
    "final_product_manager": "Final Report: Product Manager",
    "initial_analysis_user": "Final Report: User Prompt",
    "discussion_tech_lead": "Discussion: Tech Lead",
    "discussion_product_manager": "Discussion: Product Manager",
    "discussion_user": "Discussion: User Prompt",
    "synthesis": "Final Synthesis",
    "synthesis_user": "Synthesis: User Prompt",
    "instant_clarity_technical": "Instant Clarity: Technical",
    "instant_clarity_business": "Instant Clarity: Business",
    "instant_clarity_user": "Instant Clarity: User Prompt",
}

PROMPT_DESCRIPTIONS: dict[str, str] = {
    "realtime_scrum_master": "Sent as the system prompt during live transcript ingestion. Defines how each utterance is classified — summary, to-do, parking lot, or scheduling request — and enforces the JSON output format.",
    "realtime_user": "Data wrapper sent alongside the system prompt during live monitoring. Injects the raw transcript utterance and any pre-meeting context loaded from memory.",
    "final_tech_lead": "System prompt for the Tech Lead's initial independent analysis when a meeting ends. Controls the persona, focus areas (architecture, blockers, decisions), and the JSON structure of its output.",
    "final_product_manager": "System prompt for the Product Manager's initial independent analysis when a meeting ends. Controls the persona, focus areas (features, UX, roadmap), and the JSON structure of its output.",
    "initial_analysis_user": "Data context sent to both Tech Lead and PM during their parallel initial analysis. Contains the meeting ID, relevant past memories from Mem0, and the full transcript.",
    "discussion_tech_lead": "System prompt used by the Tech Lead during the cross-functional debate rounds (Tech Lead ↔ PM). Defines how it responds to the PM's analysis and structures its reply.",
    "discussion_product_manager": "System prompt used by the Product Manager during the cross-functional debate rounds (Tech Lead ↔ PM). Defines how it responds to the Tech Lead's analysis and structures its reply.",
    "discussion_user": "Full context bundle sent to each persona at every discussion round. Includes the transcript, both initial analyses, and the history of prior rounds.",
    "synthesis": "System prompt for the Scrum Master's final synthesis step. This AI reads all analyses and discussion output and produces the master JSON report shown in the meeting summary.",
    "synthesis_user": "Data bundle sent to the Scrum Master for synthesis. Contains Tech Lead findings, PM findings, the cross-functional discussion log, and the full transcript.",
    "instant_clarity_technical": "System prompt for the 'Explain Technical' button during a live meeting. Defines the Senior Engineer mentor persona — tone, depth, and what to include or omit.",
    "instant_clarity_business": "System prompt for the 'Explain Business' button during a live meeting. Defines the Executive PM persona — business framing, conciseness, and what to focus on.",
    "instant_clarity_user": "Data wrapper for Instant Clarity requests. Injects the recent transcript context and the fixed instruction to stay strictly within what was said.",
}

PROMPT_VARIABLES: dict[str, str] = {
    "realtime_user": "{transcript}, {pre_meeting_context}",
    "initial_analysis_user": "{meeting_id}, {past_memories}, {transcript}",
    "discussion_user": "{meeting_id}, {transcript}, {tech_lead_report}, {pm_report}, {discussion_history}, {round_num}",
    "synthesis_user": "{meeting_id}, {tech_lead_report}, {pm_report}, {discussion_log}, {transcript}",
    "instant_clarity_user": "{transcript_context}",
}


class PromptUpdateRequest(BaseModel):
    """Prompt Override Update Schema.

    Attributes:
        prompt_text (str): Customized system/user prompt template text.
    """
    prompt_text: str


@router.get("/api/teams/{team_id}/prompts")
def list_prompts(
    team_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, Any]:
    """List all AI prompt configurations for a team, including custom overrides.

    Args:
        team_id (int): Target team primary key ID.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: List of prompt settings with labels, descriptions, and defaults.
    """
    with SessionLocal() as db:
        _assert_owner(db, user_id, team_id)
        overrides = {
            row.prompt_key: row
            for row in db.execute(
                select(TeamPromptConfig).where(TeamPromptConfig.team_id == team_id)
            ).scalars().all()
        }
        result = [
            {
                "key": key,
                "label": PROMPT_LABELS[key],
                "text": overrides[key].prompt_text if key in overrides else PROMPT_DEFAULTS[key],
                "is_custom": key in overrides,
                "readonly": key in PROMPT_READONLY_KEYS,
                "description": PROMPT_DESCRIPTIONS.get(key),
                "updated_at": overrides[key].updated_at.isoformat() if key in overrides else None,
                "variables": PROMPT_VARIABLES.get(key),
            }
            for key in PROMPT_LABELS
        ]
        return {"prompts": result}


@router.put("/api/teams/{team_id}/prompts/{prompt_key}")
def upsert_prompt(
    team_id: int,
    prompt_key: str,
    req: PromptUpdateRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Create or update a team-specific prompt override (only team owner).

    Args:
        team_id (int): Target team primary key ID.
        prompt_key (str): Prompt configuration key.
        req (PromptUpdateRequest): Payload containing custom prompt text.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Status dictionary `{"key": prompt_key, "is_custom": True}`.

    Raises:
        HTTPException: 400 for invalid or read-only prompt keys, 403 if not team owner.
    """
    if prompt_key not in VALID_PROMPT_KEYS:
        raise HTTPException(status_code=400, detail=f"Invalid prompt key: {prompt_key}")
    if prompt_key in PROMPT_READONLY_KEYS:
        raise HTTPException(status_code=400, detail=f"Prompt '{prompt_key}' is read-only and cannot be customized")
    with SessionLocal() as db:
        _assert_owner(db, user_id, team_id)
        existing = db.execute(
            select(TeamPromptConfig).where(
                TeamPromptConfig.team_id == team_id,
                TeamPromptConfig.prompt_key == prompt_key,
            )
        ).scalar_one_or_none()
        if existing:
            existing.prompt_text = req.prompt_text.strip()
        else:
            db.add(TeamPromptConfig(
                team_id=team_id,
                prompt_key=prompt_key,
                prompt_text=req.prompt_text.strip(),
            ))
        db.commit()
        return {"key": prompt_key, "is_custom": True}


@router.delete("/api/teams/{team_id}/prompts/{prompt_key}")
def reset_prompt(
    team_id: int,
    prompt_key: str,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Reset a custom prompt override back to default (only team owner).

    Args:
        team_id (int): Target team primary key ID.
        prompt_key (str): Prompt configuration key.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Status dictionary `{"key": prompt_key, "is_custom": False}`.

    Raises:
        HTTPException: 400 for invalid/read-only prompt keys, 404 if no custom prompt exists.
    """
    if prompt_key not in VALID_PROMPT_KEYS:
        raise HTTPException(status_code=400, detail=f"Invalid prompt key: {prompt_key}")
    if prompt_key in PROMPT_READONLY_KEYS:
        raise HTTPException(status_code=400, detail=f"Prompt '{prompt_key}' is read-only and cannot be customized")
    with SessionLocal() as db:
        _assert_owner(db, user_id, team_id)
        existing = db.execute(
            select(TeamPromptConfig).where(
                TeamPromptConfig.team_id == team_id,
                TeamPromptConfig.prompt_key == prompt_key,
            )
        ).scalar_one_or_none()
        if not existing:
            raise HTTPException(status_code=404, detail="No custom prompt found for this key")
        db.delete(existing)
        db.commit()
        return {"key": prompt_key, "is_custom": False}


# ── meeting topics ─────────────────────────────────────────────────────────────


@router.post("/api/meetings/{meeting_id}/topics/{topic_id}")
def add_meeting_topic(
    meeting_id: int,
    topic_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Attach a topic badge to a meeting record.

    Args:
        meeting_id (int): Target meeting primary key ID.
        topic_id (int): Target topic primary key ID.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Confirmation `{"ok": True}`.

    Raises:
        HTTPException: 404 if meeting or topic not found, 400 if topic belongs to another team.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
        topic = db.get(Topic, topic_id)
        if not topic:
            raise HTTPException(status_code=404, detail="Topic not found")
        if meeting.team_id and topic.team_id != meeting.team_id:
            raise HTTPException(
                status_code=400, detail="Topic does not belong to this meeting's team"
            )
        existing = db.execute(
            select(meeting_topics).where(
                meeting_topics.c.meeting_id == meeting_id,
                meeting_topics.c.topic_id == topic_id,
            )
        ).first()
        if not existing:
            db.execute(
                meeting_topics.insert().values(meeting_id=meeting_id, topic_id=topic_id)
            )
            db.commit()
        return {"ok": True}


@router.delete("/api/meetings/{meeting_id}/topics/{topic_id}")
def remove_meeting_topic(
    meeting_id: int,
    topic_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Detach a topic badge from a meeting record.

    Args:
        meeting_id (int): Target meeting primary key ID.
        topic_id (int): Target topic primary key ID.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Confirmation `{"ok": True}`.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
        db.execute(
            delete(meeting_topics).where(
                meeting_topics.c.meeting_id == meeting_id,
                meeting_topics.c.topic_id == topic_id,
            )
        )
        db.commit()
        return {"ok": True}

