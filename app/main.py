from __future__ import annotations
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

"""
FastAPI Main Application and REST/WebSocket Gateway Module.

Entry point for the MeetingMind-AI backend service. Configures routing, database
schema initialization, meeting lifecycle control (start, leave, rename, delete),
real-time transcript polling, Instant Clarity generation, Vexa webhook handling,
action item management, and service health checks.
"""


import asyncio
from datetime import datetime
import os
import time
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
from app.api.system import router as system_router
from app.api.teams import router as teams_router
from app.api.websockets import router as websocket_router
from app.api.ws_manager import manager
from app.db.base import Base
from app.db.models import AgentAction, Meeting, Session, TeamMembership, Topic, TranscriptChunk, meeting_topics, User
from app.db.session import SessionLocal, engine
from app.engine.controller import ControllerAgent
from app.engine.email_service import build_email_html, send_meeting_email
from app.engine.summary_tasks import (
    active_summary_thoughts,
    cancel_summary_task,
    finalizing_meetings,
    is_meeting_summarizing,
    summary_starts,
    summary_tasks,
)
from app.engine.vexa_client import (
    TERMINAL_MEETING_STATUSES,
    _finalizing,
    poll_transcripts_from_vexa,
    monitor_meeting_until_terminal,
    sync_final_transcript_from_vexa,
    sync_speakers_from_vexa,
    update_meeting_status,
)

_resummarizing_tasks = summary_tasks
_resummarizing_start = summary_starts
_active_summary_thoughts = active_summary_thoughts


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
        conn.execute(text("ALTER TABLE agent_actions ADD COLUMN IF NOT EXISTS assignee_id INTEGER"))
        conn.execute(text("ALTER TABLE agent_actions ADD COLUMN IF NOT EXISTS tags JSONB"))
        conn.execute(text("ALTER TABLE transcript_chunks ADD COLUMN IF NOT EXISTS is_edited BOOLEAN DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE transcript_chunks ADD COLUMN IF NOT EXISTS original_text TEXT"))
        conn.execute(text("ALTER TABLE transcript_chunks ADD COLUMN IF NOT EXISTS original_speaker VARCHAR(120)"))
        conn.execute(text("ALTER TABLE transcript_chunks ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP WITH TIME ZONE"))
        conn.execute(text("ALTER TABLE transcript_chunks ADD COLUMN IF NOT EXISTS edited_by INTEGER"))
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
app.include_router(system_router)


# ── request models ────────────────────────────────────────────────────────────


class MeetingStartRequest(BaseModel):
    """Meeting Start Request Schema.

    Attributes:
        platform (str): Meeting platform identifier (e.g. 'google_meet', 'teams', 'zoom').
        native_id (str): Native meeting URL or code string.
        team_id (int | None): Optional team workspace ID.
        passcode (str | None): Optional meeting passcode.
        meeting_type (str): Agile meeting mode ('general', 'daily_standup', 'sprint_planning').
    """
    platform: str = Field(min_length=1)
    native_id: str = Field(min_length=1)
    team_id: int | None = None
    passcode: str | None = None
    meeting_type: str = "general"


class MeetingRenameRequest(BaseModel):
    """Meeting Rename and Mode Update Request Schema.

    Attributes:
        title (str | None): Optional new title string for the meeting (1-255 characters).
        meeting_type (str | None): Optional new meeting mode ('general', 'daily_standup', 'sprint_planning').
    """
    title: str | None = Field(default=None, min_length=1, max_length=255)
    meeting_type: str | None = None


class ClarityRequest(BaseModel):
    """Instant Clarity Request Schema.

    Attributes:
        mode (str): Explanation mode ('technical' or 'business').
        last_x_minutes (int | None): Optional minute cutoff window for recent transcript context.
    """
    mode: str = Field(pattern="^(technical|business)$", default="technical")
    last_x_minutes: int | None = Field(default=None, ge=1)


class TranscriptChunkUpdateRequest(BaseModel):
    """Transcript Chunk Update Request Schema.

    Attributes:
        speaker (str | None): Optional updated speaker name string.
        text (str | None): Optional updated utterance text string.
    """
    speaker: str | None = None
    text: str | None = None


class TranscriptChunkCreateRequest(BaseModel):
    """Transcript Chunk Create Request Schema.

    Attributes:
        speaker (str): Speaker name string.
        text (str): Utterance text string.
        timestamp (datetime | None): Optional UTC timestamp.
    """
    speaker: str = Field(min_length=1)
    text: str = Field(min_length=1)
    timestamp: datetime | None = None


# ── helpers ───────────────────────────────────────────────────────────────────


def _check_can_edit_meeting(db: Any, user_id: int, meeting: Meeting) -> bool:
    """Check whether user has admin/owner/creator privileges to edit meeting transcript/summary.

    Args:
        db: Active database session.
        user_id (int): User ID to verify.
        meeting (Meeting): Meeting instance.

    Returns:
        bool: True if authorized, False otherwise.
    """
    # Role-based governance check:
    # Restricts destructive/high-compute operations (transcript editing, deletion, re-summarization)
    # to team owners, admins, and scrum masters, or the meeting creator for team-less meetings.
    if meeting.team_id is not None:
        from app.db.models import Team, TeamMembership
        team = db.get(Team, meeting.team_id)
        if team and team.owner_id == user_id:
            return True
        membership = db.execute(
            select(TeamMembership).where(
                TeamMembership.user_id == user_id,
                TeamMembership.team_id == meeting.team_id,
            )
        ).scalar_one_or_none()
        if membership and membership.role in {"admin", "owner", "scrum_master"}:
            return True
        return False
    else:
        if meeting.created_by is None or meeting.created_by == user_id:
            return True
        return False


def _assert_can_edit_meeting(db: Any, user_id: int, meeting: Meeting) -> None:
    """Verify user has admin/owner permissions to edit transcript or trigger redo summary.

    Enforces meeting modification boundaries:
        - Team workspaces: Restricted to team owners, team admins, or scrum masters.
        - Personal meetings: Restricted to the original meeting creator.
        Prevents regular viewers from modifying audited records or launching heavy LLM tasks.

    Args:
        db: Active database session.
        user_id (int): User ID to verify.
        meeting (Meeting): Meeting instance.

    Raises:
        HTTPException: HTTP 403 Forbidden if not team admin/owner or meeting creator.
    """
    if not _check_can_edit_meeting(db, user_id, meeting):
        raise HTTPException(
            status_code=403,
            detail="Only team admins or owners can edit transcripts or redo summaries",
        )


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
) -> dict[str, Any]:
    """Deploy Vexa transcription bot and initiate background polling tasks for a new meeting.

    Args:
        request (MeetingStartRequest): Meeting start settings payload.
        background_tasks (BackgroundTasks): FastAPI background task manager.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Dictionary containing generated local `meeting_id` and `meeting_type`.

    Raises:
        HTTPException: 500/502 for Vexa deployment errors or DB failures.
    """
    if request.team_id is not None:
        with SessionLocal() as db:
            _assert_member(db, user_id, request.team_id)

    bot_payload = {
        "platform": request.platform,
        "native_meeting_id": request.native_id,
        "bot_name": "Meeting Mind",
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
            vexa_api_url = os.getenv("VEXA_API_URL", "http://gateway:8000/bots")
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
        import logging
        logging.error(f"HTTPError in start_meeting! URL: {vexa_api_url}, Exc: {type(exc)} {exc}")
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

        existing_is_terminal = (
            existing is not None
            and str(existing.status or "").strip().lower() in {"completed", "failed"}
        )

        m_type = (request.meeting_type or "general").strip().lower()
        if m_type == "sprint":
            m_type = "sprint_planning"
        if existing and not existing_is_terminal:
            # Reuse an in-progress meeting (e.g. reconnect / duplicate request)
            existing.status = status
            existing.title = title
            existing.meeting_type = m_type
            meeting = existing
        else:
            # Either no existing record, OR the existing one is already completed/failed.
            # With a single Vexa worker the API may return the same vexa_meeting_id for
            # a brand-new bot deployment.  We always create a fresh local record so the
            # new meeting starts with a clean slate (no old transcript / actions).
            # Append a short UUID suffix to satisfy the UNIQUE constraint when the old
            # record is still present.
            local_vexa_id = (
                f"{vexa_meeting_id}:{uuid.uuid4().hex[:8]}"
                if existing_is_terminal
                else vexa_meeting_id
            )
            meeting = Meeting(
                vexa_meeting_id=local_vexa_id,
                title=title,
                status=status,
                meeting_type=m_type,
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
                meeting.id,
                request.platform,
                request.native_id,
                vexa_api_key,
                vexa_remote_id=vexa_meeting_id,
            ),
        )

    background_tasks.add_task(run_meeting_tasks)
    return {"meeting_id": meeting.id, "meeting_type": meeting.meeting_type}


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
                vexa_api_url = os.getenv("VEXA_API_URL", "http://gateway:8000/bots")
                # Ensure the url ends with bots before appending platform/native_id
                base_bots_url = vexa_api_url if vexa_api_url.endswith("/bots") else f"{vexa_api_url}/bots"
                bot_response = await client.delete(
                    f"{base_bots_url}/{platform}/{native_id}",
                    headers={"X-API-Key": vexa_api_key},
                )
            bot_response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.info(f"[Leave] Failed to remove bot: {exc}")

    task = asyncio.create_task(
        _finalize_meeting(meeting_id, platform or "", native_id or "", vexa_api_key)
    )
    summary_tasks[meeting_id] = task
    summary_starts[meeting_id] = time.time()
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
    if meeting_id in finalizing_meetings:
        logger.info(f"[Leave] Finalization already running for meeting {meeting_id}; skipping")
        return
    finalizing_meetings.add(meeting_id)
    try:
        summary_starts.setdefault(meeting_id, time.time())
        active_summary_thoughts[meeting_id] = []

        async def on_summary_thought(thought_dict: dict[str, Any]) -> None:
            active_summary_thoughts.setdefault(meeting_id, []).append(thought_dict)
            try:
                from app.api.ws_manager import manager
                await manager.broadcast(meeting_id, {
                    "type": "summary_thought",
                    "thought": thought_dict,
                })
            except Exception as ws_err:
                logger.debug("Failed to broadcast thought via ws: %s", ws_err)

        update_meeting_status(meeting_id, "completed")

        if platform and native_id and api_key:
            try:
                await sync_final_transcript_from_vexa(
                    meeting_id, platform, native_id, api_key
                )
            except Exception as exc:
                logger.info(f"[Leave] Transcript sync failed: {exc}")
            try:
                await sync_speakers_from_vexa(meeting_id, platform, native_id, api_key)
            except Exception as exc:
                logger.info(f"[Leave] Speaker sync failed: {exc}")

        with SessionLocal() as db:
            meeting = db.get(Meeting, meeting_id)
            team_id = meeting.team_id if meeting else None
            controller = ControllerAgent()
            try:
                await controller.generate_final_report(
                    meeting_id, db, team_id=team_id, on_thought=on_summary_thought
                )
            except asyncio.CancelledError:
                logger.info(f"[Leave] Final report generation for meeting {meeting_id} was cancelled by user")
                raise
            except Exception as exc:
                logger.info(f"[Leave] Failed to generate final report: {exc}")
    finally:
        summary_tasks.pop(meeting_id, None)
        summary_starts.pop(meeting_id, None)
        finalizing_meetings.discard(meeting_id)


@app.post("/api/meetings/{meeting_id}/redispatch")
async def redispatch_meeting(
    meeting_id: int,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)

    platform, native_id = _get_local_meeting_context(meeting_id)
    if not platform or not native_id:
        raise HTTPException(status_code=400, detail="Cannot determine meeting platform or native ID")

    vexa_api_key = os.getenv("VEXA_API_KEY", "")
    if not vexa_api_key:
        raise HTTPException(status_code=500, detail="VEXA_API_KEY is not configured")

    headers = {"X-API-Key": vexa_api_key, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            vexa_api_url = os.getenv("VEXA_API_URL", "http://gateway:8000/bots")
            bot_response = await client.post(
                vexa_api_url,
                json={"platform": platform, "native_meeting_id": native_id, "bot_name": "Meeting Mind", "transcribe_enabled": True},
                headers=headers,
            )
        bot_response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=exc.response.text or "Failed to redeploy Vexa bot",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Failed to contact Vexa bot service: {exc}"
        ) from exc

    update_meeting_status(meeting_id, "requested")

    async def run_meeting_tasks():
        await asyncio.gather(
            poll_transcripts_from_vexa(meeting_id, platform, native_id, vexa_api_key, ws_manager=manager),
            monitor_meeting_until_terminal(meeting_id, platform, native_id, vexa_api_key),
        )

    background_tasks.add_task(run_meeting_tasks)
    return {"ok": True, "status": "requested"}


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

            is_summarizing = is_meeting_summarizing(m.id, m.status)
            result.append({
                "id": m.id,
                "title": m.title,
                "status": m.status,
                "meeting_type": getattr(m, "meeting_type", "general") or "general",
                "summary": m.summary,
                "is_summarizing": is_summarizing,
                "summarizing_started_at": summary_starts.get(m.id),
                "discussion_log": m.discussion_log or [],
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
        can_edit = _check_can_edit_meeting(db, user_id, meeting)
        from app.db.models import Topic
        topic_rows = db.execute(
            select(meeting_topics).where(meeting_topics.c.meeting_id == meeting_id)
        ).all()
        topics = []
        for row in topic_rows:
            t = db.get(Topic, row.topic_id)
            if t:
                topics.append({"id": t.id, "name": t.name, "color": t.color})
        is_summarizing = is_meeting_summarizing(meeting.id, meeting.status)
        return {
            "id": meeting.id,
            "title": meeting.title,
            "status": meeting.status,
            "meeting_type": getattr(meeting, "meeting_type", "general") or "general",
            "summary": meeting.summary,
            "is_summarizing": is_summarizing,
            "summarizing_started_at": summary_starts.get(meeting.id),
            "live_summary_thoughts": active_summary_thoughts.get(meeting.id, []),
            "discussion_log": meeting.discussion_log or [],
            "team_id": meeting.team_id,
            "can_edit": can_edit,
            "created_at": meeting.created_at.isoformat() if meeting.created_at else None,
            "topics": topics,
            "speakers": meeting.speakers or [],
        }


@app.patch("/api/meetings/{meeting_id}")
def update_meeting(
    meeting_id: int,
    request: MeetingRenameRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Update title and/or meeting mode of a meeting record.

    Args:
        meeting_id (int): Primary key ID of meeting.
        request (MeetingRenameRequest): Payload with optional title and meeting_type.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Updated meeting metadata dictionary.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
        _assert_can_edit_meeting(db, user_id, meeting)
        if request.title is not None:
            meeting.title = request.title.strip()
        if request.meeting_type is not None:
            m_type = request.meeting_type.strip().lower()
            if m_type == "sprint":
                m_type = "sprint_planning"
            meeting.meeting_type = m_type
        try:
            db.commit()
            db.refresh(meeting)
        except SQLAlchemyError as exc:
            db.rollback()
            raise HTTPException(
                status_code=500, detail=f"Failed to update meeting: {exc}"
            ) from exc
    return {"ok": True, "title": meeting.title, "meeting_type": meeting.meeting_type}


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(
    meeting_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Permanently delete meeting record and all associated transcripts and action items.

    Args:
        meeting_id (int): Primary key ID of meeting.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Status dictionary indicating successful deletion.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
        _assert_can_edit_meeting(db, user_id, meeting)
        try:
            db.delete(meeting)
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            raise HTTPException(
                status_code=500, detail=f"Failed to delete meeting: {exc}"
            ) from exc
    return {"ok": True}


async def _resummarize_meeting_task(meeting_id: int, team_id: int | None) -> None:
    """Background task to regenerate meeting summary without blocking HTTP gateway.

    Lifecycle:
        1. Initializes active_summary_thoughts and records starting timestamp.
        2. Streams live persona reasoning thoughts via WebSocket manager.broadcast().
        3. Invokes ControllerAgent.generate_final_report with full multi-agent BOLAA debate.
        4. Handles asyncio.CancelledError cleanly if user clicks 'Stop Summary'.
        5. Finally block guarantees cleanup of tracking dictionaries to prevent memory leaks.
    """
    try:
        summary_starts[meeting_id] = time.time()
        active_summary_thoughts[meeting_id] = []

        # Real-time thought streaming callback: captures multi-agent debate thoughts
        # and broadcasts them instantly over WebSocket to connected frontend clients.
        async def on_summary_thought(thought_dict: dict[str, Any]) -> None:
            active_summary_thoughts.setdefault(meeting_id, []).append(thought_dict)
            try:
                from app.api.ws_manager import manager
                await manager.broadcast(meeting_id, {
                    "type": "summary_thought",
                    "thought": thought_dict,
                })
            except Exception as ws_err:
                logger.debug("Failed to broadcast thought via ws: %s", ws_err)

        with SessionLocal() as db:
            controller = ControllerAgent()
            try:
                await controller.generate_final_report(
                    meeting_id, db, team_id=team_id, on_thought=on_summary_thought
                )
            except asyncio.CancelledError:
                logger.info("[Resummarize] Task for meeting %s was cancelled by user", meeting_id)
                raise
            except Exception as exc:
                logger.exception("Failed to resummarize meeting %s in background: %s", meeting_id, exc)
    finally:
        # Guaranteed cleanup: ensure registry keys are removed on completion, error, or cancellation
        summary_tasks.pop(meeting_id, None)
        summary_starts.pop(meeting_id, None)


@app.post("/api/meetings/{meeting_id}/resummarize")
async def resummarize_meeting(
    meeting_id: int,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Regenerate meeting summary and insights from current transcript chunks (admin/owner only).

    Args:
        meeting_id (int): Primary key ID of target meeting.
        background_tasks (BackgroundTasks): Background tasks manager.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Status dictionary indicating regeneration has started in background.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        _assert_can_edit_meeting(db, user_id, meeting)
        team_id = meeting.team_id

        # Cancel any already-running task for this meeting
        old_task = summary_tasks.get(meeting_id)
        if old_task and not old_task.done():
            old_task.cancel()

        # Clear existing summary so frontend recognizes regeneration in progress
        meeting.summary = None
        db.commit()

        active_summary_thoughts[meeting_id] = []
        task = asyncio.create_task(_resummarize_meeting_task(meeting_id, team_id))
        summary_tasks[meeting_id] = task
        summary_starts[meeting_id] = time.time()

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
            "ok": True,
            "status": "processing",
            "message": "Summary regeneration running in background.",
            "meeting": {
                "id": meeting.id,
                "title": meeting.title,
                "status": meeting.status,
                "summary": None,
                "is_summarizing": True,
                "summarizing_started_at": summary_starts.get(meeting_id),
                "live_summary_thoughts": [],
                "team_id": meeting.team_id,
                "can_edit": True,
                "created_at": meeting.created_at.isoformat() if meeting.created_at else None,
                "topics": topics,
                "speakers": meeting.speakers or [],
            },
        }


@app.post("/api/meetings/{meeting_id}/stop-summary")
async def stop_summary_generation(
    meeting_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Cancel any active background summary generation for a meeting.

    Args:
        meeting_id (int): Primary key ID of target meeting.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Cancellation status confirmation.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        _assert_can_edit_meeting(db, user_id, meeting)

    stopped = cancel_summary_task(meeting_id)

    try:
        from app.api.ws_manager import manager
        await manager.broadcast(meeting_id, {
            "type": "summary_stopped",
            "meeting_id": meeting_id,
        })
    except Exception as ws_err:
        logger.debug("Failed to broadcast stop summary event: %s", ws_err)

    return {
        "ok": True,
        "message": "Summary generation stopped successfully." if stopped else "No active summary generation to stop."
    }


@app.get("/api/meetings/{meeting_id}/summary-thoughts")
def get_summary_thoughts(
    meeting_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Retrieve live thoughts generated during active summary synthesis.

    Args:
        meeting_id (int): Primary key ID of target meeting.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: List of thought records.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)

    return {
        "ok": True,
        "meeting_id": meeting_id,
        "thoughts": _active_summary_thoughts.get(meeting_id, []),
    }


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
        dict[str, Any]: Dictionary containing list of transcript chunk items and edit permissions.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
        can_edit = _check_can_edit_meeting(db, user_id, meeting)
        chunks = db.execute(
            select(TranscriptChunk)
            .where(TranscriptChunk.meeting_id == meeting_id)
            .order_by(TranscriptChunk.timestamp.asc())
        ).scalars().all()
        return {
            "meeting_id": meeting_id,
            "status": meeting.status,
            "can_edit": can_edit,
            "chunks": [
                {
                    "id": c.id,
                    "speaker": c.speaker,
                    "text": c.text,
                    "timestamp": c.timestamp.isoformat() if hasattr(c.timestamp, "isoformat") else str(c.timestamp) if c.timestamp else None,
                    "is_edited": bool(c.is_edited),
                    "original_text": c.original_text,
                    "original_speaker": c.original_speaker,
                    "edited_at": c.edited_at.isoformat() if hasattr(c.edited_at, "isoformat") and c.edited_at else None,
                }
                for c in chunks
            ],
        }


@app.patch("/api/meetings/{meeting_id}/transcript/{chunk_id}")
def update_transcript_chunk(
    meeting_id: int,
    chunk_id: int,
    request: TranscriptChunkUpdateRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Edit a transcript chunk (admin/owner only). Preserves original text/speaker on first edit.

    Args:
        meeting_id (int): Primary key ID of target meeting.
        chunk_id (int): Primary key ID of target transcript chunk.
        request (TranscriptChunkUpdateRequest): Payload containing new speaker/text.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Updated chunk dictionary with edited status and original versions.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        _assert_can_edit_meeting(db, user_id, meeting)
        chunk = db.get(TranscriptChunk, chunk_id)
        if not chunk or chunk.meeting_id != meeting_id:
            raise HTTPException(status_code=404, detail="Transcript chunk not found")

        # Transcript revision audit trail mechanism:
        # On first edit, freeze the raw speech-to-text text and diarized speaker into
        # original_text and original_speaker. Subsequent edits update text/speaker but leave
        # the initial raw capture intact, enabling non-destructive edits and lossless revert.
        if not chunk.is_edited or chunk.original_text is None:
            chunk.original_text = chunk.text
            chunk.original_speaker = chunk.speaker

        if request.text is not None:
            chunk.text = request.text.strip()
        if request.speaker is not None:
            chunk.speaker = request.speaker.strip()

        from datetime import datetime, timezone
        chunk.is_edited = True
        chunk.edited_at = datetime.now(timezone.utc)
        chunk.edited_by = user_id

        # Update meeting speakers list if speaker was modified or added
        if request.speaker and meeting.speakers is not None:
            current_speakers = set(meeting.speakers)
            current_speakers.add(chunk.speaker)
            meeting.speakers = list(current_speakers)

        try:
            db.commit()
            db.refresh(chunk)
        except SQLAlchemyError as exc:
            db.rollback()
            raise HTTPException(
                status_code=500, detail=f"Failed to update transcript chunk: {exc}"
            ) from exc

        return {
            "ok": True,
            "chunk": {
                "id": chunk.id,
                "meeting_id": chunk.meeting_id,
                "speaker": chunk.speaker,
                "text": chunk.text,
                "timestamp": chunk.timestamp.isoformat() if hasattr(chunk.timestamp, "isoformat") else str(chunk.timestamp) if chunk.timestamp else None,
                "is_edited": chunk.is_edited,
                "original_text": chunk.original_text,
                "original_speaker": chunk.original_speaker,
                "edited_at": chunk.edited_at.isoformat() if hasattr(chunk.edited_at, "isoformat") and chunk.edited_at else None,
            },
        }


@app.post("/api/meetings/{meeting_id}/transcript/{chunk_id}/revert")
def revert_transcript_chunk(
    meeting_id: int,
    chunk_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Revert an edited transcript chunk back to its original raw version (admin/owner only).

    Args:
        meeting_id (int): Primary key ID of target meeting.
        chunk_id (int): Primary key ID of target transcript chunk.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Reverted chunk dictionary.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        _assert_can_edit_meeting(db, user_id, meeting)
        chunk = db.get(TranscriptChunk, chunk_id)
        if not chunk or chunk.meeting_id != meeting_id:
            raise HTTPException(status_code=404, detail="Transcript chunk not found")

        # Lossless revert: Restore text and speaker to original STT values,
        # reset is_edited flag, and clear editor attribution metadata.
        if chunk.is_edited and chunk.original_text is not None:
            chunk.text = chunk.original_text
            if chunk.original_speaker is not None:
                chunk.speaker = chunk.original_speaker
            chunk.is_edited = False
            chunk.original_text = None
            chunk.original_speaker = None
            chunk.edited_at = None
            chunk.edited_by = None

            try:
                db.commit()
                db.refresh(chunk)
            except SQLAlchemyError as exc:
                db.rollback()
                raise HTTPException(
                    status_code=500, detail=f"Failed to revert transcript chunk: {exc}"
                ) from exc

        return {
            "ok": True,
            "chunk": {
                "id": chunk.id,
                "meeting_id": chunk.meeting_id,
                "speaker": chunk.speaker,
                "text": chunk.text,
                "timestamp": chunk.timestamp.isoformat() if hasattr(chunk.timestamp, "isoformat") else str(chunk.timestamp) if chunk.timestamp else None,
                "is_edited": chunk.is_edited,
                "original_text": chunk.original_text,
                "original_speaker": chunk.original_speaker,
                "edited_at": chunk.edited_at.isoformat() if hasattr(chunk.edited_at, "isoformat") and chunk.edited_at else None,
            },
        }


@app.delete("/api/meetings/{meeting_id}/transcript/{chunk_id}")
def delete_transcript_chunk(
    meeting_id: int,
    chunk_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Delete a transcript chunk from a meeting (admin/owner only).

    Args:
        meeting_id (int): Primary key ID of target meeting.
        chunk_id (int): Primary key ID of target transcript chunk.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Confirmation dictionary `{"ok": True}`.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        _assert_can_edit_meeting(db, user_id, meeting)
        chunk = db.get(TranscriptChunk, chunk_id)
        if not chunk or chunk.meeting_id != meeting_id:
            raise HTTPException(status_code=404, detail="Transcript chunk not found")

        try:
            db.delete(chunk)
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            raise HTTPException(
                status_code=500, detail=f"Failed to delete transcript chunk: {exc}"
            ) from exc

        return {"ok": True}


@app.post("/api/meetings/{meeting_id}/transcript")
def create_transcript_chunk(
    meeting_id: int,
    request: TranscriptChunkCreateRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    """Manually insert a transcript chunk (admin/owner only).

    Args:
        meeting_id (int): Primary key ID of target meeting.
        request (TranscriptChunkCreateRequest): Payload containing speaker, text, and optional timestamp.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, Any]: Created transcript chunk dictionary.
    """
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        _assert_can_edit_meeting(db, user_id, meeting)

        from datetime import datetime, timezone
        ts = request.timestamp or datetime.now(timezone.utc)
        chunk = TranscriptChunk(
            meeting_id=meeting_id,
            speaker=request.speaker.strip(),
            text=request.text.strip(),
            timestamp=ts,
            is_edited=True,
            edited_at=datetime.now(timezone.utc),
            edited_by=user_id,
        )
        db.add(chunk)

        # Update speakers list
        if meeting.speakers is None:
            meeting.speakers = []
        if chunk.speaker not in meeting.speakers:
            meeting.speakers = list(meeting.speakers) + [chunk.speaker]

        try:
            db.commit()
            db.refresh(chunk)
        except SQLAlchemyError as exc:
            db.rollback()
            raise HTTPException(
                status_code=500, detail=f"Failed to create transcript chunk: {exc}"
            ) from exc

        return {
            "ok": True,
            "chunk": {
                "id": chunk.id,
                "meeting_id": chunk.meeting_id,
                "speaker": chunk.speaker,
                "text": chunk.text,
                "timestamp": chunk.timestamp.isoformat() if hasattr(chunk.timestamp, "isoformat") else str(chunk.timestamp) if chunk.timestamp else None,
                "is_edited": chunk.is_edited,
                "original_text": chunk.original_text,
                "original_speaker": chunk.original_speaker,
                "edited_at": chunk.edited_at.isoformat() if hasattr(chunk.edited_at, "isoformat") and chunk.edited_at else None,
            },
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

        meeting_ids = {a.meeting_id for a, _, _, _ in rows}
        meeting_topics_map: dict[int, list[dict[str, Any]]] = {}
        if meeting_ids:
            topic_rows = db.execute(
                select(meeting_topics.c.meeting_id, Topic.id, Topic.name, Topic.color)
                .join(Topic, meeting_topics.c.topic_id == Topic.id)
                .where(meeting_topics.c.meeting_id.in_(meeting_ids))
            ).all()
            for m_id, t_id, t_name, t_color in topic_rows:
                meeting_topics_map.setdefault(m_id, []).append({
                    "id": t_id,
                    "name": t_name,
                    "color": t_color,
                })

        grouped: dict[str, dict[str, list[dict[str, Any]]]] = {
            "parking_lot": {"pending": [], "accepted": [], "rejected": [], "archived": []},
            "to_do": {"pending": [], "accepted": [], "rejected": [], "archived": []},
            "to_schedule": {"pending": [], "accepted": [], "rejected": [], "archived": []},
            "blocker": {"pending": [], "accepted": [], "rejected": [], "archived": []},
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
                "topics": meeting_topics_map.get(a.meeting_id, []),
                "assignee": {
                    "id": u.id,
                    "name": u.name,
                    "photo_url": f"/api/auth/photo/{u.id}" if u.photo else None
                } if u else None
            }
            bucket = a.status if a.status in ["accepted", "rejected", "archived"] else "pending"
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

        topic_rows = db.execute(
            select(Topic.id, Topic.name, Topic.color)
            .join(meeting_topics, meeting_topics.c.topic_id == Topic.id)
            .where(meeting_topics.c.meeting_id == meeting_id)
        ).all()
        m_topics = [{"id": t_id, "name": t_name, "color": t_color} for t_id, t_name, t_color in topic_rows]

        rows = db.execute(
            select(AgentAction, User)
            .outerjoin(User, AgentAction.assignee_id == User.id)
            .where(AgentAction.meeting_id == meeting_id)
            .order_by(AgentAction.id.desc())
        ).all()

        grouped: dict[str, dict[str, list[dict[str, Any]]]] = {
            "parking_lot": {"pending": [], "accepted": [], "rejected": [], "archived": []},
            "to_do": {"pending": [], "accepted": [], "rejected": [], "archived": []},
            "to_schedule": {"pending": [], "accepted": [], "rejected": [], "archived": []},
            "blocker": {"pending": [], "accepted": [], "rejected": [], "archived": []},
        }
        for a, u in rows:
            t = a.action_type
            if t not in grouped:
                continue
            entry = {
                "id": a.id,
                "agent_role": a.agent_role,
                "action_type": t,
                "content": a.content,
                "status": a.status,
                "tags": a.tags or [],
                "topics": m_topics,
                "assignee": {
                    "id": u.id,
                    "name": u.name,
                    "photo_url": f"/api/auth/photo/{u.id}" if u.photo else None
                } if u else None
            }
            bucket = a.status if a.status in ["accepted", "rejected", "archived"] else "pending"
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
    status: str | None = Field(default=None, pattern="^(accepted|rejected|pending|archived)$")
    content: str | None = None
    assignee_id: int | None = None
    action_type: str | None = Field(default=None, pattern="^(parking_lot|to_do|to_schedule|blocker)$")
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


@app.delete("/api/meetings/{meeting_id}/actions/{action_id}")
def delete_action(
    meeting_id: int,
    action_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, str]:
    """Delete an action item.

    Args:
        meeting_id (int): Primary key ID of meeting.
        action_id (int): Primary key ID of action item.
        user_id (int): Authenticated user ID.

    Returns:
        dict[str, str]: Confirmation dictionary.
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
        db.delete(action)
        db.commit()
        return {"status": "ok"}

class ActionCreateRequest(BaseModel):
    """Manual Action Item Creation Schema.

    Attributes:
        action_type (str): Action category ('parking_lot', 'to_do', 'to_schedule').
        content (str): Text description of action item.
        assignee_id (int | None): Optional assigned user ID.
        tags (list[str] | None): Optional list of tags.
    """
    action_type: str = Field(pattern="^(parking_lot|to_do|to_schedule|blocker)$")
    content: str
    assignee_id: int | None = None
    status: str = Field(default="accepted", pattern="^(accepted|pending|rejected)$")
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
            status=request.status,
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


class EmailSendRequest(BaseModel):
    recipient_ids: list[int]


def _build_actions_dict(db: Any, meeting_id: int) -> dict[str, Any]:
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
        "blocker": {"pending": [], "accepted": [], "rejected": []},
    }
    for a, u in rows:
        t = a.action_type
        if t not in grouped:
            continue
        entry: dict[str, Any] = {
            "id": a.id,
            "agent_role": a.agent_role,
            "action_type": t,
            "content": a.content,
            "status": a.status,
            "tags": a.tags or [],
            "assignee": {
                "id": u.id,
                "name": u.name,
                "photo_url": f"/api/auth/photo/{u.id}" if u.photo else None,
            } if u else None,
        }
        bucket = "accepted" if a.status == "accepted" else ("rejected" if a.status == "rejected" else "pending")
        grouped[t][bucket].append(entry)
    return grouped


@app.get("/api/meetings/{meeting_id}/email-preview")
def get_email_preview(
    meeting_id: int,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
        meeting_dict = {
            "title": meeting.title,
            "created_at": meeting.created_at.isoformat() if meeting.created_at else None,
            "summary": meeting.summary,
            "speakers": meeting.speakers or [],
        }
        actions_dict = _build_actions_dict(db, meeting_id)
    html = build_email_html(meeting_dict, actions_dict)
    return {"html": html}


@app.post("/api/meetings/{meeting_id}/send-email")
async def send_email(
    meeting_id: int,
    request: EmailSendRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, Any]:
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        if meeting.team_id:
            _assert_member(db, user_id, meeting.team_id)
        meeting_dict = {
            "title": meeting.title,
            "created_at": meeting.created_at.isoformat() if meeting.created_at else None,
            "summary": meeting.summary,
            "speakers": meeting.speakers or [],
        }
        actions_dict = _build_actions_dict(db, meeting_id)
        recipients = db.execute(
            select(User).where(User.id.in_(request.recipient_ids))
        ).scalars().all()
        to_emails = [u.email for u in recipients if u.email]

    if not to_emails:
        raise HTTPException(status_code=400, detail="No valid recipient emails found")

    raw_title = meeting_dict["title"]
    title = ":".join(raw_title.split(":")[1:]) if ":" in raw_title else raw_title
    subject = f"Meeting Report — {title}"
    html = build_email_html(meeting_dict, actions_dict)

    try:
        await send_meeting_email(to_emails, subject, html)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to send email: {exc}") from exc

    return {"ok": True, "sent_to": to_emails}


@app.get("/health", tags=["health"])
def health_check() -> dict[str, str]:
    """Liveness check endpoint returning standard status response.

    Returns:
        dict[str, str]: `{"status": "ok"}`
    """
    return {"status": "ok"}

