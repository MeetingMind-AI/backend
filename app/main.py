from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.api.websockets import router as websocket_router
from app.db.models import Meeting, TranscriptChunk
from app.db.session import SessionLocal
from app.engine.vexa_client import (
    TERMINAL_MEETING_STATUSES,
    poll_transcripts_from_vexa,
    monitor_meeting_until_terminal,
    sync_final_transcript_from_vexa,
    update_meeting_status,
)

app = FastAPI(title="MeetingMind AI Backend")

app.include_router(websocket_router)


class MeetingStartRequest(BaseModel):
    platform: str = Field(min_length=1)
    native_id: str = Field(min_length=1)


def _find_local_meeting_id(vexa_meeting_id: str | None, platform: str, native_id: str) -> int | None:
    with SessionLocal() as db:
        if vexa_meeting_id:
            by_vexa_stmt = select(Meeting.id).where(Meeting.vexa_meeting_id == vexa_meeting_id).limit(1)
            by_vexa_result = db.execute(by_vexa_stmt).scalar_one_or_none()
            if by_vexa_result is not None:
                return int(by_vexa_result)

        if platform and native_id:
            fallback_title = f"{platform}:{native_id}"
            by_title_stmt = (
                select(Meeting.id)
                .where(Meeting.title == fallback_title)
                .order_by(Meeting.created_at.desc())
                .limit(1)
            )
            by_title_result = db.execute(by_title_stmt).scalar_one_or_none()
            if by_title_result is not None:
                return int(by_title_result)

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


@app.post("/api/meetings/start")
async def start_meeting(request: MeetingStartRequest, background_tasks: BackgroundTasks) -> dict[str, int]:
    bot_payload = {
        "platform": request.platform,
        "native_meeting_id": request.native_id,
        "transcribe_enabled": True,
    }

    vexa_api_key = os.getenv("VEXA_API_KEY", "")
    if not vexa_api_key:
        raise HTTPException(status_code=500, detail="VEXA_API_KEY is not configured")

    headers = {
        "X-API-Key": vexa_api_key,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            bot_response = await client.post(
                "http://host.docker.internal:8056/bots",
                json=bot_payload,
                headers=headers,
            )
        bot_response.raise_for_status()
        raw_bot_data: Any = bot_response.json() if bot_response.content else {}
        deployed_bot_data: dict[str, Any] = raw_bot_data if isinstance(raw_bot_data, dict) else {}
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text or "Failed to deploy Vexa bot"
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to contact Vexa bot service: {exc}") from exc

    vexa_meeting_id = str(
        deployed_bot_data.get("id")
        or deployed_bot_data.get("meeting_id")
        or deployed_bot_data.get("vexa_meeting_id")
        or f"{request.platform}:{request.native_id}:{uuid.uuid4().hex}"
    )
    title = str(deployed_bot_data.get("title") or f"{request.platform}:{request.native_id}")
    status = str(deployed_bot_data.get("status") or "requested")

    with SessionLocal() as db:
        # 1. Check if the meeting already exists
        existing_stmt = select(Meeting).where(Meeting.vexa_meeting_id == vexa_meeting_id).limit(1)
        meeting = db.execute(existing_stmt).scalar_one_or_none()

        if meeting:
            # 2. If it exists, just update its status and title
            meeting.status = status
            meeting.title = title
        else:
            # 3. If it doesn't exist, create it
            meeting = Meeting(
                vexa_meeting_id=vexa_meeting_id,
                title=title,
                status=status,
            )
            db.add(meeting)

        try:
            db.commit()
            db.refresh(meeting)
        except SQLAlchemyError as exc:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to create/update meeting: {exc}") from exc

    async def run_meeting_tasks():
        await asyncio.gather(
            poll_transcripts_from_vexa(meeting.id, request.platform, request.native_id, vexa_api_key),
            monitor_meeting_until_terminal(meeting.id, request.platform, request.native_id, vexa_api_key)
        )

    background_tasks.add_task(run_meeting_tasks)

    return {"meeting_id": meeting.id}


@app.post("/api/meetings/{meeting_id}/leave")
async def leave_meeting(meeting_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")

        platform, native_id = _get_local_meeting_context(meeting_id)
        if not platform or not native_id:
            raise HTTPException(status_code=400, detail="Cannot determine platform/native_id for meeting")

    vexa_api_key = os.getenv("VEXA_API_KEY", "")
    if not vexa_api_key:
        raise HTTPException(status_code=500, detail="VEXA_API_KEY is not configured")

    headers = {
        "X-API-Key": vexa_api_key,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.delete(
                f"http://host.docker.internal:8056/bots/{platform}/{native_id}",
                headers=headers,
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text or "Failed to leave meeting") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to contact Vexa service: {exc}") from exc

    return {"ok": True}


@app.post("/api/vexa/webhook")
async def handle_vexa_webhook(
    event: dict[str, Any],
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict[str, Any]:
    webhook_secret = os.getenv("VEXA_WEBHOOK_SECRET", "").strip()
    if webhook_secret:
        auth_header = request.headers.get("authorization", "")
        expected_header = f"Bearer {webhook_secret}"
        if auth_header != expected_header:
            raise HTTPException(status_code=401, detail="Invalid webhook Authorization header")

    raw_event_type = str(event.get("event_type") or event.get("event") or "").strip()
    event_type = raw_event_type.lower()
    if event_type not in {"meeting.status_change", "meeting.completed"}:
        return {"ok": True, "ignored_event_type": raw_event_type or None}

    meeting_payload = event.get("meeting")
    if not isinstance(meeting_payload, dict):
        raise HTTPException(status_code=422, detail="meeting payload is required for status change events")

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
        fallback_platform, fallback_native_id = _get_local_meeting_context(local_meeting_id)
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


@app.get("/api/meetings/{meeting_id}/transcript")
def get_transcript(meeting_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        chunks = (
            db.execute(
                select(TranscriptChunk)
                .where(TranscriptChunk.meeting_id == meeting_id)
                .order_by(TranscriptChunk.timestamp.asc())
            )
            .scalars()
            .all()
        )
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


@app.get("/health", tags=["health"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}
