from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import router as auth_router
from app.api.deps import get_current_user_id
from app.api.teams import router as teams_router
from app.api.websockets import router as websocket_router
from app.api.ws_manager import manager
from app.db.base import Base
from app.db.models import AgentAction, Meeting, Session, TranscriptChunk, meeting_topics
from app.db.session import SessionLocal, engine
from app.engine.controller import ControllerAgent
from app.engine.vexa_client import (
    TERMINAL_MEETING_STATUSES,
    poll_transcripts_from_vexa,
    monitor_meeting_until_terminal,
    sync_final_transcript_from_vexa,
    update_meeting_status,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="MeetingMind AI Backend", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(teams_router)
app.include_router(websocket_router)


# ── request models ────────────────────────────────────────────────────────────


class MeetingStartRequest(BaseModel):
    platform: str = Field(min_length=1)
    native_id: str = Field(min_length=1)
    team_id: int | None = None


class MeetingRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class ClarityRequest(BaseModel):
    mode: str = Field(pattern="^(technical|business)$", default="technical")
    last_x_minutes: int | None = Field(default=None, ge=1)


# ── helpers ───────────────────────────────────────────────────────────────────


def _find_local_meeting_id(
    vexa_meeting_id: str | None, platform: str, native_id: str
) -> int | None:
    with SessionLocal() as db:
        if vexa_meeting_id:
            by_vexa = (
                select(Meeting.id)
                .where(Meeting.vexa_meeting_id == vexa_meeting_id)
                .limit(1)
            )
            result = db.execute(by_vexa).scalar_one_or_none()
            if result is not None:
                return int(result)

        if platform and native_id:
            fallback_title = f"{platform}:{native_id}"
            by_title = (
                select(Meeting.id)
                .where(Meeting.title == fallback_title)
                .order_by(Meeting.created_at.desc())
                .limit(1)
            )
            result = db.execute(by_title).scalar_one_or_none()
            if result is not None:
                return int(result)

    return None


def _get_local_meeting_context(local_meeting_id: int) -> tuple[str, str]:
    with SessionLocal() as db:
        meeting = db.get(Meeting, local_meeting_id)
        if meeting is None:
            return "", ""
        title = str(meeting.title or "")
        if ":" not in title:
            return "", ""
        platform, native_id = title.split(":", 1)
        return platform.strip(), native_id.strip()


def _user_id_from_cookie(mm_session: str | None) -> int | None:
    if not mm_session:
        return None
    with SessionLocal() as db:
        row = db.execute(
            select(Session).where(Session.token == mm_session)
        ).scalar_one_or_none()
        return int(row.user_id) if row else None


# ── meeting endpoints ─────────────────────────────────────────────────────────


@app.post("/api/meetings/start")
async def start_meeting(
    request: MeetingStartRequest,
    background_tasks: BackgroundTasks,
    mm_session: str | None = Cookie(default=None),
) -> dict[str, int]:
    user_id = _user_id_from_cookie(mm_session)

    bot_payload = {
        "platform": request.platform,
        "native_meeting_id": request.native_id,
        "transcribe_enabled": True,
    }

    vexa_api_key = os.getenv("VEXA_API_KEY", "")
    if not vexa_api_key:
        raise HTTPException(status_code=500, detail="VEXA_API_KEY is not configured")

    headers = {"X-API-Key": vexa_api_key, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            bot_response = await client.post(
                "http://host.docker.internal:8056/bots",
                json=bot_payload,
                headers=headers,
            )
        bot_response.raise_for_status()
        raw: Any = bot_response.json() if bot_response.content else {}
        deployed: dict[str, Any] = raw if isinstance(raw, dict) else {}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=exc.response.text or "Failed to deploy Vexa bot",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Failed to contact Vexa bot service: {exc}"
        ) from exc

    vexa_meeting_id = str(
        deployed.get("id")
        or deployed.get("meeting_id")
        or deployed.get("vexa_meeting_id")
        or f"{request.platform}:{request.native_id}:{uuid.uuid4().hex}"
    )
    title = str(deployed.get("title") or f"{request.platform}:{request.native_id}")
    status = str(deployed.get("status") or "requested")

    with SessionLocal() as db:
        existing = db.execute(
            select(Meeting).where(Meeting.vexa_meeting_id == vexa_meeting_id).limit(1)
        ).scalar_one_or_none()

        if existing:
            existing.status = status
            existing.title = title
            meeting = existing
        else:
            meeting = Meeting(
                vexa_meeting_id=vexa_meeting_id,
                title=title,
                status=status,
                team_id=request.team_id,
                created_by=user_id,
            )
            db.add(meeting)

        try:
            db.commit()
            db.refresh(meeting)
        except SQLAlchemyError as exc:
            db.rollback()
            raise HTTPException(
                status_code=500, detail=f"Failed to create/update meeting: {exc}"
            ) from exc

    async def run_meeting_tasks():
        await asyncio.gather(
            poll_transcripts_from_vexa(
                meeting.id,
                request.platform,
                request.native_id,
                vexa_api_key,
                ws_manager=manager,
            ),
            monitor_meeting_until_terminal(
                meeting.id, request.platform, request.native_id, vexa_api_key
            ),
        )

    background_tasks.add_task(run_meeting_tasks)
    return {"meeting_id": meeting.id}


@app.post("/api/meetings/{meeting_id}/leave")
async def leave_meeting(
    meeting_id: int, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        platform, native_id = _get_local_meeting_context(meeting_id)

    vexa_api_key = os.getenv("VEXA_API_KEY", "")

    if platform and native_id and vexa_api_key:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.delete(
                    f"http://host.docker.internal:8056/bots/{platform}/{native_id}",
                    headers={"X-API-Key": vexa_api_key},
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"[Leave] Failed to remove bot: {exc}")

    background_tasks.add_task(
        _finalize_meeting, meeting_id, platform or "", native_id or "", vexa_api_key
    )
    return {"ok": True}


async def _finalize_meeting(
    meeting_id: int, platform: str, native_id: str, api_key: str
) -> None:
    update_meeting_status(meeting_id, "completed")

    if platform and native_id and api_key:
        try:
            await sync_final_transcript_from_vexa(
                meeting_id, platform, native_id, api_key
            )
        except Exception as exc:
            print(f"[Leave] Transcript sync failed: {exc}")

    with SessionLocal() as db:
        controller = ControllerAgent()
        try:
            await controller.generate_final_report(meeting_id, db)
        except Exception as exc:
            print(f"[Leave] Failed to generate final report: {exc}")


@app.post("/api/meetings/{meeting_id}/explain")
async def explain_meeting(meeting_id: int, request: ClarityRequest) -> dict[str, str]:
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        controller = ControllerAgent()
        explanation = await controller.generate_instant_clarity(
            meeting_id=meeting_id,
            db_session=db,
            mode=request.mode,
            last_x_minutes=request.last_x_minutes,
        )
    return {"explanation": explanation}


@app.post("/api/vexa/webhook")
async def handle_vexa_webhook(
    event: dict[str, Any],
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict[str, Any]:
    webhook_secret = os.getenv("VEXA_WEBHOOK_SECRET", "").strip()
    if webhook_secret:
        auth_header = request.headers.get("authorization", "")
        if auth_header != f"Bearer {webhook_secret}":
            raise HTTPException(status_code=401, detail="Invalid webhook Authorization header")

    raw_event_type = str(event.get("event_type") or event.get("event") or "").strip()
    event_type = raw_event_type.lower()
    if event_type not in {"meeting.status_change", "meeting.completed"}:
        return {"ok": True, "ignored_event_type": raw_event_type or None}

    meeting_payload = event.get("meeting")
    if not isinstance(meeting_payload, dict):
        raise HTTPException(
            status_code=422,
            detail="meeting payload is required for status change events",
        )

    platform = str(meeting_payload.get("platform") or "").strip()
    native_id = str(meeting_payload.get("native_meeting_id") or "").strip()
    remote_meeting_id = str(meeting_payload.get("id") or "").strip()

    status_to = ""
    status_change = event.get("status_change")
    if isinstance(status_change, dict):
        status_to = str(status_change.get("to") or "").strip().lower()
    if not status_to:
        status_to = str(meeting_payload.get("status") or "").strip().lower()
    if not status_to and event_type == "meeting.completed":
        status_to = "completed"

    if not native_id:
        data_payload = meeting_payload.get("data")
        if isinstance(data_payload, dict):
            native_id = str(data_payload.get("native_meeting_id") or "").strip()

    local_meeting_id = _find_local_meeting_id(
        vexa_meeting_id=remote_meeting_id or None,
        platform=platform,
        native_id=native_id,
    )
    if local_meeting_id is None:
        return {
            "ok": True,
            "ignored": "meeting_not_tracked",
            "vexa_meeting_id": remote_meeting_id or None,
        }

    if status_to:
        update_meeting_status(local_meeting_id, status_to)

    if not platform or not native_id:
        fallback_platform, fallback_native_id = _get_local_meeting_context(
            local_meeting_id
        )
        platform = platform or fallback_platform
        native_id = native_id or fallback_native_id

    if status_to in TERMINAL_MEETING_STATUSES and platform and native_id:
        background_tasks.add_task(
            sync_final_transcript_from_vexa,
            local_meeting_id,
            platform,
            native_id,
        )

    return {
        "ok": True,
        "meeting_id": local_meeting_id,
        "status": status_to or None,
        "final_sync_scheduled": bool(
            status_to in TERMINAL_MEETING_STATUSES and platform and native_id
        ),
    }


@app.get("/api/meetings")
def list_meetings(
    team_id: int | None = Query(default=None),
) -> dict[str, Any]:
    with SessionLocal() as db:
        stmt = select(Meeting).order_by(Meeting.created_at.desc())
        if team_id is not None:
            stmt = stmt.where(Meeting.team_id == team_id)
        meetings = db.execute(stmt).scalars().all()

        result = []
        for m in meetings:
            topic_rows = db.execute(
                select(meeting_topics).where(meeting_topics.c.meeting_id == m.id)
            ).all()
            from app.db.models import Topic
            topics = []
            for row in topic_rows:
                t = db.get(Topic, row.topic_id)
                if t:
                    topics.append({"id": t.id, "name": t.name, "color": t.color})

            result.append({
                "id": m.id,
                "title": m.title,
                "status": m.status,
                "summary": m.summary,
                "team_id": m.team_id,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "topics": topics,
            })
        return {"meetings": result}


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        return {
            "id": meeting.id,
            "title": meeting.title,
            "status": meeting.status,
            "summary": meeting.summary,
            "team_id": meeting.team_id,
            "created_at": meeting.created_at.isoformat() if meeting.created_at else None,
        }


@app.patch("/api/meetings/{meeting_id}")
def rename_meeting(meeting_id: int, request: MeetingRenameRequest) -> dict[str, Any]:
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        meeting.title = request.title
        try:
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            raise HTTPException(
                status_code=500, detail=f"Failed to rename meeting: {exc}"
            ) from exc
    return {"id": meeting_id, "title": request.title}


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting_record(meeting_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        try:
            db.delete(meeting)
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            raise HTTPException(
                status_code=500, detail=f"Failed to delete meeting: {exc}"
            ) from exc
    return {"ok": True}


@app.get("/api/meetings/{meeting_id}/transcript")
def get_transcript(meeting_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        chunks = db.execute(
            select(TranscriptChunk)
            .where(TranscriptChunk.meeting_id == meeting_id)
            .order_by(TranscriptChunk.timestamp.asc())
        ).scalars().all()
        return {
            "meeting_id": meeting_id,
            "status": meeting.status,
            "chunks": [
                {
                    "id": c.id,
                    "speaker": c.speaker,
                    "text": c.text,
                    "timestamp": c.timestamp.isoformat(),
                }
                for c in chunks
            ],
        }


@app.get("/api/actions")
def list_all_actions(
    team_id: int | None = Query(default=None),
) -> dict[str, Any]:
    with SessionLocal() as db:
        stmt = (
            select(AgentAction, Meeting.title, Meeting.created_at)
            .join(Meeting, AgentAction.meeting_id == Meeting.id)
            .order_by(AgentAction.id.desc())
        )
        if team_id is not None:
            stmt = stmt.where(Meeting.team_id == team_id)

        rows = db.execute(stmt).all()

        grouped: dict[str, dict[str, list[dict[str, Any]]]] = {
            "parking_lot": {"pending": [], "accepted": [], "rejected": []},
            "to_do": {"pending": [], "accepted": [], "rejected": []},
            "to_schedule": {"pending": [], "accepted": [], "rejected": []},
        }
        for a, m_title, m_created in rows:
            if a.action_type not in grouped:
                continue
            entry = {
                "id": a.id,
                "meeting_id": a.meeting_id,
                "meeting_title": m_title,
                "meeting_date": m_created.isoformat() if m_created else None,
                "agent_role": a.agent_role,
                "action_type": a.action_type,
                "content": a.content,
                "status": a.status,
            }
            bucket = "accepted" if a.status == "accepted" else ("rejected" if a.status == "rejected" else "pending")
            grouped[a.action_type][bucket].append(entry)
        return grouped


@app.get("/api/meetings/{meeting_id}/actions")
def list_actions(meeting_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        rows = db.execute(
            select(AgentAction)
            .where(AgentAction.meeting_id == meeting_id)
            .order_by(AgentAction.id.desc())
        ).scalars().all()

        grouped: dict[str, dict[str, list[dict[str, Any]]]] = {
            "parking_lot": {"pending": [], "accepted": [], "rejected": []},
            "to_do": {"pending": [], "accepted": [], "rejected": []},
            "to_schedule": {"pending": [], "accepted": [], "rejected": []},
        }
        for a in rows:
            t = (
                a.action_type
                if a.action_type not in ("blocker", "conflict")
                else "parking_lot"
            )
            if t not in grouped:
                continue
            entry = {
                "id": a.id,
                "agent_role": a.agent_role,
                "action_type": t,
                "content": a.content,
                "status": a.status,
            }
            bucket = "accepted" if a.status == "accepted" else ("rejected" if a.status == "rejected" else "pending")
            grouped[t][bucket].append(entry)
        return grouped


class ActionReviewRequest(BaseModel):
    status: str | None = Field(default=None, pattern="^(accepted|rejected|pending)$")
    content: str | None = None


@app.patch("/api/meetings/{meeting_id}/actions/{action_id}")
def review_action(
    meeting_id: int, action_id: int, request: ActionReviewRequest
) -> dict[str, Any]:
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        action = db.get(AgentAction, action_id)
        if not action or action.meeting_id != meeting_id:
            raise HTTPException(status_code=404, detail="Action not found")
        if request.status is not None:
            action.status = request.status
        if request.content is not None:
            action.content = request.content
        try:
            db.commit()
            db.refresh(action)
        except SQLAlchemyError as exc:
            db.rollback()
            raise HTTPException(
                status_code=500, detail=f"Failed to update action: {exc}"
            ) from exc
        status = action.status
        content = action.content
    return {"ok": True, "id": action_id, "status": status, "content": content}


@app.get("/health", tags=["health"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}
