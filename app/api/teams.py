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
from app.engine.prompts import PROMPT_DEFAULTS

router = APIRouter(tags=["teams"])


# ── helpers ──────────────────────────────────────────────────────────────────


def _assert_member(db, user_id: int, team_id: int) -> None:
    row = db.execute(
        select(TeamMembership).where(
            TeamMembership.user_id == user_id,
            TeamMembership.team_id == team_id,
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=403, detail="Not a member of this team")


def _assert_owner(db, user_id: int, team_id: int) -> Team:
    team = db.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    if team.owner_id != user_id:
        raise HTTPException(status_code=403, detail="Only the team owner can do this")
    return team


def _member_out(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "photo_url": f"/api/auth/photo/{user.id}" if user.photo else None,
    }


def _topic_out(topic: Topic) -> dict[str, Any]:
    return {
        "id": topic.id,
        "name": topic.name,
        "color": topic.color,
    }


# ── teams ─────────────────────────────────────────────────────────────────────


class TeamCreateRequest(BaseModel):
    name: str


class TeamUpdateRequest(BaseModel):
    name: str


@router.get("/api/teams")
def list_teams(user_id: int = Depends(get_current_user_id)) -> dict[str, Any]:
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
    with SessionLocal() as db:
        team = Team(
            name=req.name.strip(),
            owner_id=user_id,
            invite_token=uuid.uuid4().hex,
        )
        db.add(team)
        db.flush()
        db.add(TeamMembership(user_id=user_id, team_id=team.id))
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
                members.append({**_member_out(u), "is_owner": u.id == team.owner_id})

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
    with SessionLocal() as db:
        team = _assert_owner(db, user_id, team_id)
        team.name = req.name.strip()
        db.commit()
        return {"id": team.id, "name": team.name}


@router.post("/api/teams/{team_id}/leave")
def leave_team(
    team_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, Any]:
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


@router.get("/api/teams/{team_id}/invite")
def get_invite_link(
    team_id: int,
    request: Request,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
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
            db.add(TeamMembership(user_id=user_id, team_id=team.id))
            db.commit()
        return {"team_id": team.id, "team_name": team.name}


@router.get("/api/teams/{team_id}/members")
def list_members(
    team_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, Any]:
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
                    **_member_out(u),
                    "is_owner": u.id == (team.owner_id if team else None),
                })
        return {"members": members}


@router.delete("/api/teams/{team_id}/members/{target_user_id}")
def kick_member(
    team_id: int,
    target_user_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
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


# ── topics ────────────────────────────────────────────────────────────────────


class TopicCreateRequest(BaseModel):
    name: str
    color: str = "#4f8ef7"


class TopicUpdateRequest(BaseModel):
    name: str | None = None
    color: str | None = None


@router.get("/api/teams/{team_id}/topics")
def list_topics(
    team_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, Any]:
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
    "final_tech_lead": "Final Report: Tech Lead",
    "final_product_manager": "Final Report: Product Manager",
    "discussion_tech_lead": "Discussion: Tech Lead",
    "discussion_product_manager": "Discussion: Product Manager",
    "synthesis": "Final Synthesis",
    "instant_clarity_technical": "Instant Clarity: Technical",
    "instant_clarity_business": "Instant Clarity: Business",
}


class PromptUpdateRequest(BaseModel):
    prompt_text: str


@router.get("/api/teams/{team_id}/prompts")
def list_prompts(
    team_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, Any]:
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
                "updated_at": overrides[key].updated_at.isoformat() if key in overrides else None,
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
    if prompt_key not in VALID_PROMPT_KEYS:
        raise HTTPException(status_code=400, detail=f"Invalid prompt key: {prompt_key}")
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
    if prompt_key not in VALID_PROMPT_KEYS:
        raise HTTPException(status_code=400, detail=f"Invalid prompt key: {prompt_key}")
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
