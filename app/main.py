"""
FastAPI Main Application and REST/WebSocket Gateway Module.

Entry point for the MeetingMind-AI backend service. Configures routing, database
schema initialization, meeting lifecycle control (start, leave, rename, delete),
real-time transcript polling, Instant Clarity generation, Vexa webhook handling,
action item management, and service health checks.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import router as auth_router
from app.api.deps import get_current_user_id
from app.api.teams import router as teams_router
from app.api.websockets import router as websocket_router
from app.api.ws_manager import manager
from app.db.base import Base
from app.db.models import AgentAction, Meeting, Session, TeamMembership, TranscriptChunk, meeting_topics, User
from app.db.session import SessionLocal, engine
from app.engine.controller import ControllerAgent
from app.engine.vexa_client import (
    TERMINAL_MEETING_STATUSES,
    poll_transcripts_from_vexa,
    monitor_meeting_until_terminal,
    sync_final_transcript_from_vexa,
    sync_speakers_from_vexa,
    update_meeting_status,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager for database schema migrations and startup setup.

    Ensures all database tables and column additions (e.g. `speakers` JSONB column)
    are created before accepting incoming HTTP requests.

    Args:
        app (FastAPI): The application instance.
    """
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE meetings ADD COLUMN IF NOT EXISTS speakers JSONB"))
        conn.execute(text("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'meetings' AND column_name = 'participants'
                ) THEN
                    UPDATE meetings SET speakers = participants WHERE speakers IS NULL AND participants IS NOT NULL;
                    ALTER TABLE meetings DROP COLUMN participants;
                END IF;
            END $$;
        """))
        conn.commit()
    yield


app = FastAPI(title="MeetingMind AI Backend", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(teams_router)
app.include_router(websocket_router)


# ── request models ────────────────────────────────────────────────────────────


class MeetingStartRequest(BaseModel):
    """Meeting Start Request Schema.

    Attributes:
        platform (str): Meeting platform identifier (e.g. 'google_meet', 'teams', 'zoom').
        native_id (str): Native meeting URL or code string.
        team_id (int | None): Optional team workspace ID.
        passcode (str | None): Optional meeting passcode.
    """
    platform: str = Field(min_length=1)
    native_id: str = Field(min_length=1)
    team_id: int | None = None
    passcode: str | None = None


class MeetingRenameRequest(BaseModel):
    """Meeting Rename Request Schema.

    Attributes:
        title (str): New title string for the meeting (1-255 characters).
    """
    title: str = Field(min_length=1, max_length=255)


class ClarityRequest(BaseModel):
    """Instant Clarity Request Schema.

    Attributes:
        mode (str): Explanation mode ('technical' or 'business').
        last_x_minutes (int | None): Optional minute cutoff window for recent transcript context.
    """
    mode: str = Field(pattern="^(technical|business)$", default="technical")
    last_x_minutes: int | None = Field(default=None, ge=1)


# ── helpers ───────────────────────────────────────────────────────────────────


def _find_local_meeting_id(
    vexa_meeting_id: str | None, platform: str, native_id: str
) -> int | None:
    """Look up local meeting primary key ID by remote Vexa meeting ID or platform/native_id fallback.

    Args:
        vexa_meeting_id (str | None): Remote Vexa meeting ID.
        platform (str): Platform identifier string.
        native_id (str): Native platform meeting ID string.

    Returns:
        int | None: Local meeting primary key ID if found, None otherwise.
    """
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
    """Extract platform and native ID from local meeting title string.

    Args:
        local_meeting_id (int): Primary key ID of the meeting.

    Returns:
        tuple[str, str]: Tuple of (platform, native_id).
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, local_meeting_id)
        if meeting is None:
            return "", ""
        title = str(meeting.title or "")
        if ":" not in title:
            return "", ""
        platform, native_id = title.split(":", 1)
        return platform.strip(), native_id.strip()


def _assert_member(db: Any, user_id: int, team_id: int) -> None:
    """Verify user membership in team.

    Args:
        db: Active database session.
        user_id (int): User ID to verify.
        team_id (int): Team ID to verify.

    Raises:
        HTTPException: HTTP 403 Forbidden if not a team member.
    """
    row = db.execute(
        select(TeamMembership).where(
            TeamMembership.user_id == user_id,
            TeamMembership.team_id == team_id,
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=403, detail="Not a member of this team")


# ── meeting endpoints ─────────────────────────────────────────────────────────


@app.post("/api/meetings/start")
async def start_meeting(
    request: MeetingStartRequest,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, int]:
    """Deploy Vexa transcription bot and initiate background polling tasks for a new meeting.

    Args:
        request (MeetingStartRequest): Meeting start settings payload.
        background_tasks (BackgroundTasks): FastAPI background task manager.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, int]: Dictionary containing generated local `meeting_id`.

    Raises:
        HTTPException: 500/502 for Vexa deployment errors or DB failures.
    """
    if request.team_id is not None:
        with SessionLocal() as db:
            _assert_member(db, user_id, request.team_id)

    bot_payload = {
        "platform": request.platform,
        "native_meeting_id": request.native_id,
        "transcribe_enabled": True,
    }
    if request.passcode:
        bot_payload["passcode"] = request.passcode

    vexa_api_key = os.getenv("VEXA_API_KEY", "")
    if not vexa_api_key:
        raise HTTPException(status_code=500, detail="VEXA_API_KEY is not configured")

    headers = {"X-API-Key": vexa_api_key, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            vexa_api_url = os.getenv("VEXA_API_URL", "http://host.docker.internal:18056/bots")
            bot_response = await client.post(
                vexa_api_url,
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
        """Execute async polling and monitoring loops for active meeting."""
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
    meeting_id: int,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Remove Vexa bot from meeting and schedule background finalization.

    Args:
        meeting_id (int): Primary key ID of local meeting.
        background_tasks (BackgroundTasks): Background tasks manager.
        user_id (int): Authenticated user ID.

    Returns:
        JSONResponse: HTTP 202 Accepted response with status payload.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
        platform, native_id = _get_local_meeting_context(meeting_id)

    vexa_api_key = os.getenv("VEXA_API_KEY", "")

    if platform and native_id and vexa_api_key:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                vexa_api_url = os.getenv("VEXA_API_URL", "http://host.docker.internal:18056/bots")
                # Ensure the url ends with bots before appending platform/native_id
                base_bots_url = vexa_api_url if vexa_api_url.endswith("/bots") else f"{vexa_api_url}/bots"
                bot_response = await client.delete(
                    f"{base_bots_url}/{platform}/{native_id}",
                    headers={"X-API-Key": vexa_api_key},
                )
            bot_response.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"[Leave] Failed to remove bot: {exc}")

    background_tasks.add_task(
        _finalize_meeting, meeting_id, platform or "", native_id or "", vexa_api_key
    )
    return JSONResponse(
        status_code=202,
        content={
            "ok": True,
            "message": "Meeting finalization is running in the background.",
        },
    )


async def _finalize_meeting(
    meeting_id: int, platform: str, native_id: str, api_key: str
) -> None:
    """Async background task for final transcript sync, speaker sync, and report generation.

    Args:
        meeting_id (int): Primary key ID of the meeting.
        platform (str): Platform name string.
        native_id (str): Native meeting URL/ID.
        api_key (str): Vexa API key.
    """
    update_meeting_status(meeting_id, "completed")

    if platform and native_id and api_key:
        try:
            await sync_final_transcript_from_vexa(
                meeting_id, platform, native_id, api_key
            )
        except Exception as exc:
            print(f"[Leave] Transcript sync failed: {exc}")
        try:
            await sync_speakers_from_vexa(meeting_id, platform, native_id, api_key)
        except Exception as exc:
            print(f"[Leave] Speaker sync failed: {exc}")

    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        team_id = meeting.team_id if meeting else None
        controller = ControllerAgent()
        try:
            await controller.generate_final_report(meeting_id, db, team_id=team_id)
        except Exception as exc:
            print(f"[Leave] Failed to generate final report: {exc}")


@app.post("/api/meetings/{meeting_id}/explain")
async def explain_meeting(
    meeting_id: int,
    request: ClarityRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, str]:
    """Generate Instant Clarity technical or business explanation for recent meeting content.

    Args:
        meeting_id (int): Target meeting primary key ID.
        request (ClarityRequest): Clarity mode and window payload.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, str]: Dictionary containing generated explanation string.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
        controller = ControllerAgent()
        explanation = await controller.generate_instant_clarity(
            meeting_id=meeting_id,
            db_session=db,
            mode=request.mode,
            last_x_minutes=request.last_x_minutes,
            team_id=meeting.team_id,
        )
    return {"explanation": explanation}


@app.post("/api/vexa/webhook")
async def handle_vexa_webhook(
    event: dict[str, Any],
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict[str, Any]:
    """Ingress handler for Vexa bot status change and completion webhooks.

    Args:
        event (dict[str, Any]): Webhook JSON payload.
        background_tasks (BackgroundTasks): Background tasks runner.
        request (Request): HTTP request context for authentication header inspection.

    Returns:
        dict[str, Any]: Processing status confirmation object.

    Raises:
        HTTPException: 401 if webhook secret header is invalid, 422 if payload invalid.
    """
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
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """List meetings accessible to user, optionally filtered by team ID.

    Args:
        team_id (int | None): Optional team primary key filter.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: List of meeting summaries with topics and speaker lists.
    """
    with SessionLocal() as db:
        if team_id is not None:
            _assert_member(db, user_id, team_id)
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
                "speakers": m.speakers or [],
            })
        return {"meetings": result}


@app.get("/api/meetings/{meeting_id}")
def get_meeting(
    meeting_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Retrieve detailed record for a single meeting.

    Args:
        meeting_id (int): Primary key ID of target meeting.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Meeting record dictionary.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
        from app.db.models import Topic
        topic_rows = db.execute(
            select(meeting_topics).where(meeting_topics.c.meeting_id == meeting_id)
        ).all()
        topics = []
        for row in topic_rows:
            t = db.get(Topic, row.topic_id)
            if t:
                topics.append({"id": t.id, "name": t.name, "color": t.color})
        return {
            "id": meeting.id,
            "title": meeting.title,
            "status": meeting.status,
            "summary": meeting.summary,
            "team_id": meeting.team_id,
            "created_at": meeting.created_at.isoformat() if meeting.created_at else None,
            "topics": topics,
            "speakers": meeting.speakers or [],
        }


@app.patch("/api/meetings/{meeting_id}")
def rename_meeting(
    meeting_id: int,
    request: MeetingRenameRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Update title of a meeting record.

    Args:
        meeting_id (int): Primary key ID of meeting.
        request (MeetingRenameRequest): Payload with new title string.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Updated meeting title dictionary.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
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
def delete_meeting_record(
    meeting_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Delete meeting record and associated transcript chunks and action items.

    Args:
        meeting_id (int): Target meeting primary key ID.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Success confirmation `{"ok": True}`.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
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
def get_transcript(
    meeting_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Retrieve chronologically ordered transcript chunks for a meeting.

    Args:
        meeting_id (int): Primary key ID of target meeting.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Dictionary containing list of transcript chunk items.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
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
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """List all action items grouped by category and approval status across meetings.

    Args:
        team_id (int | None): Optional team primary key filter.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Categorized action items dictionary ('parking_lot', 'to_do', 'to_schedule').
    """
    with SessionLocal() as db:
        if team_id is not None:
            _assert_member(db, user_id, team_id)
        stmt = (
            select(AgentAction, Meeting.title, Meeting.created_at, User)
            .join(Meeting, AgentAction.meeting_id == Meeting.id)
            .outerjoin(User, AgentAction.assignee_id == User.id)
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
        for a, m_title, m_created, u in rows:
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
                "tags": a.tags or [],
                "assignee": {
                    "id": u.id,
                    "name": u.name,
                    "photo_url": f"/api/auth/photo/{u.id}" if u.photo else None
                } if u else None
            }
            bucket = "accepted" if a.status == "accepted" else ("rejected" if a.status == "rejected" else "pending")
            grouped[a.action_type][bucket].append(entry)
        return grouped


@app.get("/api/meetings/{meeting_id}/actions")
def list_actions(
    meeting_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """List action items for a single meeting grouped by type and status.

    Args:
        meeting_id (int): Primary key ID of target meeting.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Grouped action items dictionary.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
        rows = db.execute(
            select(AgentAction, User)
            .outerjoin(User, AgentAction.assignee_id == User.id)
            .where(AgentAction.meeting_id == meeting_id)
            .order_by(AgentAction.id.desc())
        ).all()

        grouped: dict[str, dict[str, list[dict[str, Any]]]] = {
            "parking_lot": {"pending": [], "accepted": [], "rejected": []},
            "to_do": {"pending": [], "accepted": [], "rejected": []},
            "to_schedule": {"pending": [], "accepted": [], "rejected": []},
        }
        for a, u in rows:
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
                "tags": a.tags or [],
                "assignee": {
                    "id": u.id,
                    "name": u.name,
                    "photo_url": f"/api/auth/photo/{u.id}" if u.photo else None
                } if u else None
            }
            bucket = "accepted" if a.status == "accepted" else ("rejected" if a.status == "rejected" else "pending")
            grouped[t][bucket].append(entry)
        return grouped


class ActionReviewRequest(BaseModel):
    """Action Item Review/Update Schema.

    Attributes:
        status (str | None): Optional status ('accepted', 'rejected', 'pending').
        content (str | None): Optional updated text content.
        assignee_id (int | None): Optional assigned user ID.
        action_type (str | None): Optional action category.
        tags (list[str] | None): Optional list of tags.
    """
    status: str | None = Field(default=None, pattern="^(accepted|rejected|pending)$")
    content: str | None = None
    assignee_id: int | None = None
    action_type: str | None = Field(default=None, pattern="^(parking_lot|to_do|to_schedule)$")
    tags: list[str] | None = None


@app.patch("/api/meetings/{meeting_id}/actions/{action_id}")
def review_action(
    meeting_id: int,
    action_id: int,
    request: ActionReviewRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Review or edit an existing action item (status, assignee, tags, content).

    Args:
        meeting_id (int): Primary key ID of meeting.
        action_id (int): Primary key ID of action item.
        request (ActionReviewRequest): Update request fields.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Confirmation dictionary with updated fields.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
        action = db.get(AgentAction, action_id)
        if not action or action.meeting_id != meeting_id:
            raise HTTPException(status_code=404, detail="Action not found")
        update_data = request.model_dump(exclude_unset=True)
        if "status" in update_data:
            action.status = update_data["status"]
        if "content" in update_data:
            action.content = update_data["content"]
        if "assignee_id" in update_data:
            action.assignee_id = update_data["assignee_id"]
        if "action_type" in update_data:
            action.action_type = update_data["action_type"]
        if "tags" in update_data:
            action.tags = update_data["tags"]
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
        tags = action.tags or []
    return {"ok": True, "id": action_id, "status": status, "content": content, "tags": tags}


class ActionCreateRequest(BaseModel):
    """Manual Action Item Creation Schema.

    Attributes:
        action_type (str): Action category ('parking_lot', 'to_do', 'to_schedule').
        content (str): Text description of action item.
        assignee_id (int | None): Optional assigned user ID.
        tags (list[str] | None): Optional list of tags.
    """
    action_type: str = Field(pattern="^(parking_lot|to_do|to_schedule)$")
    content: str
    assignee_id: int | None = None
    tags: list[str] | None = None


@app.post("/api/meetings/{meeting_id}/actions")
def create_action(
    meeting_id: int,
    request: ActionCreateRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Manually create a new action item for a meeting.

    Args:
        meeting_id (int): Primary key ID of meeting.
        request (ActionCreateRequest): Action item attributes payload.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Newly created action item dictionary.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
        
        action = AgentAction(
            meeting_id=meeting_id,
            agent_role="manual",
            action_type=request.action_type,
            content=request.content,
            assignee_id=request.assignee_id,
            status="pending",
            tags=request.tags or [],
        )
        db.add(action)
        try:
            db.commit()
            db.refresh(action)
        except SQLAlchemyError as exc:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to create action: {exc}") from exc
            
        u = None
        if action.assignee_id:
            u = db.get(User, action.assignee_id)
            
        return {
            "id": action.id,
            "meeting_id": action.meeting_id,
            "agent_role": action.agent_role,
            "action_type": action.action_type,
            "content": action.content,
            "status": action.status,
            "tags": action.tags or [],
            "assignee": {
                "id": u.id,
                "name": u.name,
                "photo_url": f"/api/auth/photo/{u.id}" if u.photo else None
            } if u else None
        }


@app.get("/health", tags=["health"])
def health_check() -> dict[str, str]:
    """Liveness check endpoint returning standard status response.

    Returns:
        dict[str, str]: `{"status": "ok"}`
    """
    return {"status": "ok"}

