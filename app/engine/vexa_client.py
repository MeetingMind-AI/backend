from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import Meeting, TranscriptChunk
from app.db.session import SessionLocal
from app.engine.controller import ControllerAgent

VEXA_WS_URL = "ws://host.docker.internal:8056/ws"
TERMINAL_MEETING_STATUSES = {"completed", "failed"}
REALTIME_MIN_WORDS = 4
FINALIZATION_PROGRESS_INTERVAL_SECONDS = 5


def _vexa_ws_url() -> str:
    candidate = os.getenv("VEXA_WS_URL", VEXA_WS_URL).strip()
    return candidate or VEXA_WS_URL


def _vexa_api_base_url() -> str:
    configured = (
        os.getenv("VEXA_API_BASE_URL")
        or os.getenv("VEXA_API_URL")
        or "http://host.docker.internal:8056"
    ).strip()

    if configured.endswith("/bots"):
        configured = configured[:-5]

    return configured.rstrip("/")


def _append_api_key_query_param(ws_url: str, api_key: str) -> str:
    parsed = urllib.parse.urlparse(ws_url)
    query_items = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query_items["api_key"] = api_key
    rebuilt_query = urllib.parse.urlencode(query_items)
    return urllib.parse.urlunparse(parsed._replace(query=rebuilt_query))


def _websocket_connect_with_headers(ws_url: str, api_key: str):
    headers = [("X-API-Key", api_key)]
    kwargs = {
        "ping_interval": 20,
        "ping_timeout": 20,
        "close_timeout": 5,
    }

    try:
        return websockets.connect(ws_url, additional_headers=headers, **kwargs)
    except TypeError:
        return websockets.connect(ws_url, extra_headers=headers, **kwargs)


def _extract_segments(message: dict[str, Any]) -> list[dict[str, Any]]:
    payload = message.get("payload")
    if isinstance(payload, dict):
        nested_segments = payload.get("segments")
        if isinstance(nested_segments, list):
            return [segment for segment in nested_segments if isinstance(segment, dict)]

    root_segments = message.get("segments")
    if isinstance(root_segments, list):
        return [segment for segment in root_segments if isinstance(segment, dict)]

    return []


def _parse_absolute_start_time(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.now(timezone.utc)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _message_from_raw(raw_message: str | bytes) -> dict[str, Any] | None:
    if isinstance(raw_message, bytes):
        try:
            raw_message = raw_message.decode("utf-8")
        except UnicodeDecodeError:
            return None

    try:
        message = json.loads(raw_message)
    except json.JSONDecodeError:
        return None

    if not isinstance(message, dict):
        return None
    return message


def _parse_optional_iso_datetime(value: str) -> datetime | None:
    normalized = value.strip().replace("Z", "+00:00")
    if not normalized:
        return None

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _should_replace_transcript_segment(
    existing_segment: dict[str, Any] | None,
    incoming_segment: dict[str, Any],
) -> bool:
    incoming_text = str(incoming_segment.get("text", "")).strip()
    if not incoming_text:
        return False

    if existing_segment is None:
        return True

    existing_text = str(existing_segment.get("text", "")).strip()
    incoming_updated_at = _parse_optional_iso_datetime(str(incoming_segment.get("updated_at") or ""))
    existing_updated_at = _parse_optional_iso_datetime(str(existing_segment.get("updated_at") or ""))

    if incoming_updated_at and existing_updated_at:
        if incoming_updated_at > existing_updated_at:
            return True
        if incoming_updated_at < existing_updated_at:
            return False

    if existing_text.endswith("...") and not incoming_text.endswith("..."):
        return True

    return len(incoming_text) >= len(existing_text)


def _word_count(text: str) -> int:
    return len(text.split())


def _is_meaningful_realtime_text(text: str) -> bool:
    return _word_count(text.strip()) >= REALTIME_MIN_WORDS


def _should_log_transcript_update(
    is_immutable: bool,
    text: str,
    previous_logged_text: str,
) -> bool:
    cleaned_text = text.strip()
    if not cleaned_text:
        return False
    if cleaned_text == previous_logged_text:
        return False

    if is_immutable:
        return True

    if _word_count(cleaned_text) < REALTIME_MIN_WORDS:
        return False

    if previous_logged_text and len(cleaned_text) <= len(previous_logged_text):
        return False

    return True


def _log_transcript_line(meeting_id: int, speaker: str, text: str, is_immutable: bool) -> None:
    phase = "final" if is_immutable else "live"
    compact_text = " ".join(text.split())
    print(f"[Vexa Transcript] meeting={meeting_id} phase={phase} speaker={speaker}: {compact_text}")


async def _emit_finalization_progress(meeting_id: int, done: asyncio.Event, source: str) -> None:
    elapsed = 0
    while not done.is_set():
        print(
            f"[Vexa] Finalization in progress for meeting {meeting_id} "
            f"(source={source}, elapsed={elapsed}s)"
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=FINALIZATION_PROGRESS_INTERVAL_SECONDS)
            return
        except TimeoutError:
            elapsed += FINALIZATION_PROGRESS_INTERVAL_SECONDS


async def _finalize_completed_meeting(
    controller: ControllerAgent,
    meeting_id: int,
    platform: str,
    native_id: str,
    api_key: str,
    source: str,
) -> None:
    print(f"[Vexa] Starting finalization for meeting {meeting_id} (source={source})")
    progress_done = asyncio.Event()
    progress_task = asyncio.create_task(_emit_finalization_progress(meeting_id, progress_done, source))
    started = time.monotonic()

    try:
        upserted = await sync_final_transcript_from_vexa(
            meeting_id=meeting_id,
            platform=platform,
            native_id=native_id,
            api_key=api_key,
        )
        print(f"[Vexa] Final transcript sync for meeting {meeting_id} upserted {upserted} chunks")
        await _generate_and_log_final_report(controller, meeting_id)
    finally:
        progress_done.set()
        await progress_task

    duration = round(time.monotonic() - started, 2)
    print(f"[Vexa] Finalization complete for meeting {meeting_id} in {duration}s")


async def _generate_and_log_final_report(controller: ControllerAgent, meeting_id: int) -> None:
    with SessionLocal() as db:
        try:
            await controller.generate_final_report(meeting_id, db)
        except Exception as exc:  # noqa: BLE001
            print(f"[Vexa] Failed to generate final report for meeting {meeting_id}: {type(exc).__name__}: {str(exc)}")


def _is_local_meeting_terminal(meeting_id: int) -> bool:
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            return True
        status_value = str(meeting.status or "").strip().lower()
        return status_value in TERMINAL_MEETING_STATUSES


def _extract_latest_remote_meeting(
    meetings_payload: Any,
    platform: str,
    native_id: str,
) -> dict[str, Any] | None:
    if not isinstance(meetings_payload, dict):
        return None

    meetings = meetings_payload.get("meetings")
    if not isinstance(meetings, list):
        return None

    matched: list[dict[str, Any]] = []
    for item in meetings:
        if not isinstance(item, dict):
            continue
        if str(item.get("platform", "")).strip() != platform:
            continue
        item_native_id = str(item.get("native_meeting_id") or item.get("platform_specific_id") or "").strip()
        if item_native_id != native_id:
            continue
        matched.append(item)

    if not matched:
        return None

    def sort_key(item: dict[str, Any]) -> tuple[str, str]:
        updated = str(item.get("updated_at") or "")
        created = str(item.get("created_at") or "")
        return (updated, created)

    return max(matched, key=sort_key)


def update_meeting_status(meeting_id: int, status_value: str) -> None:
    normalized = status_value.strip()
    if not normalized:
        return

    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            return
        if str(meeting.status or "") == normalized:
            return

        meeting.status = normalized
        try:
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            print(f"[Vexa] Failed to update meeting status for {meeting_id}: {exc}")


async def sync_final_transcript_from_vexa(
    meeting_id: int,
    platform: str,
    native_id: str,
    api_key: str | None = None,
) -> int:
    vexa_api_key = (api_key or os.getenv("VEXA_API_KEY", "")).strip()
    if not vexa_api_key:
        print(f"[Vexa] Cannot sync final transcript for meeting {meeting_id}: missing API key")
        return 0

    base_url = _vexa_api_base_url()
    url = f"{base_url}/transcripts/{platform}/{native_id}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers={"X-API-Key": vexa_api_key})
        response.raise_for_status()
        payload: Any = response.json() if response.content else {}
    except httpx.HTTPError as exc:
        print(f"[Vexa] Failed to fetch final transcript for meeting {meeting_id}: {exc}")
        return 0

    if not isinstance(payload, dict):
        return 0

    segments = payload.get("segments")
    if not isinstance(segments, list):
        return 0

    canonical_segments_by_abs_start: dict[str, dict[str, Any]] = {}
    for segment in segments:
        if not isinstance(segment, dict):
            continue

        absolute_start_time = str(segment.get("absolute_start_time", "")).strip()
        if not absolute_start_time:
            continue

        existing = canonical_segments_by_abs_start.get(absolute_start_time)
        if _should_replace_transcript_segment(existing, segment):
            canonical_segments_by_abs_start[absolute_start_time] = segment

    if not canonical_segments_by_abs_start:
        return 0

    canonical_segments = [
        canonical_segments_by_abs_start[key]
        for key in sorted(canonical_segments_by_abs_start.keys())
    ]

    inserted_count = 0

    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            print(f"[Vexa] Local meeting {meeting_id} not found during final transcript sync")
            return 0

        db.execute(delete(TranscriptChunk).where(TranscriptChunk.meeting_id == meeting_id))

        for segment in canonical_segments:
            text = str(segment.get("text", "")).strip()
            if not text:
                continue

            absolute_start_time = str(segment.get("absolute_start_time", "")).strip()
            if not absolute_start_time:
                continue

            speaker = str(segment.get("speaker") or "Unknown").strip() or "Unknown"
            timestamp = _parse_absolute_start_time(absolute_start_time)

            db.add(
                TranscriptChunk(
                    meeting_id=meeting_id,
                    speaker=speaker,
                    text=text,
                    timestamp=timestamp,
                )
            )
            inserted_count += 1

        try:
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            print(f"[Vexa] Failed to persist final transcript for meeting {meeting_id}: {exc}")
            return 0

    return inserted_count


async def monitor_meeting_until_terminal(
    meeting_id: int,
    platform: str,
    native_id: str,
    api_key: str | None = None,
    timeout_seconds: int = 7200,
) -> None:
    vexa_api_key = (api_key or os.getenv("VEXA_API_KEY", "")).strip()
    if not vexa_api_key:
        print(f"[Vexa] Cannot poll meeting lifecycle for {meeting_id}: missing API key")
        return

    base_url = _vexa_api_base_url()
    meetings_url = f"{base_url}/meetings"
    poll_interval = int(os.getenv("VEXA_MEETING_POLL_INTERVAL_SECONDS", "10"))
    if poll_interval < 3:
        poll_interval = 3

    deadline = time.monotonic() + max(timeout_seconds, poll_interval)
    controller = ControllerAgent()
    while time.monotonic() < deadline:
        if _is_local_meeting_terminal(meeting_id):
            return

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(meetings_url, headers={"X-API-Key": vexa_api_key})
            response.raise_for_status()
            payload: Any = response.json() if response.content else {}
        except httpx.HTTPError as exc:
            print(f"[Vexa] Meeting poll failed for meeting {meeting_id}: {exc}")
            await asyncio.sleep(poll_interval)
            continue

        remote_meeting = _extract_latest_remote_meeting(payload, platform, native_id)
        if not remote_meeting:
            print(f"[Vexa] Poller could not find remote meeting for {platform}:{native_id} in {len(payload.get('meetings', []))} meetings")
            await asyncio.sleep(poll_interval)
            continue

        status_value = str(remote_meeting.get("status", "")).strip().lower()
        if status_value:
            update_meeting_status(meeting_id, status_value)

        if status_value in TERMINAL_MEETING_STATUSES:
            print(f"[Vexa] Poller detected terminal status '{status_value}' for meeting {meeting_id}")
            if status_value == "completed":
                await _finalize_completed_meeting(
                    controller=controller,
                    meeting_id=meeting_id,
                    platform=platform,
                    native_id=native_id,
                    api_key=vexa_api_key,
                    source="poller",
                )
            return


        await asyncio.sleep(poll_interval)

    print(f"[Vexa] Meeting poll timeout for meeting {meeting_id} after {timeout_seconds}s")


async def poll_transcripts_from_vexa(
    meeting_id: int,
    platform: str,
    native_id: str,
    api_key: str | None = None,
    poll_interval: int = 15,
) -> None:
    vexa_api_key = (api_key or os.getenv("VEXA_API_KEY", "")).strip()
    if not vexa_api_key:
        print(f"[Vexa] VEXA_API_KEY is missing; transcript polling disabled for meeting {meeting_id}")
        return

    controller = ControllerAgent()

    while not _is_local_meeting_terminal(meeting_id):
        try:
            upserted = await sync_final_transcript_from_vexa(
                meeting_id=meeting_id,
                platform=platform,
                native_id=native_id,
                api_key=vexa_api_key,
            )
            if upserted > 0:
                print(f"[Vexa] Synced {upserted} clean transcript segments for meeting {meeting_id}")
        except Exception as exc:
            print(f"[Vexa] Transcript poll failed for meeting {meeting_id}: {exc}")
        
        await asyncio.sleep(poll_interval)
    
    print(f"[Vexa] Meeting {meeting_id} is terminal; stopping transcript polling")

