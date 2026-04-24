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
from sqlalchemy import select
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


def _segment_identity(timestamp: datetime, speaker: str, text: str) -> tuple[str, str, str]:
    return (timestamp.isoformat(), speaker, text)


def _word_count(text: str) -> int:
    return len(text.split())


def _is_meaningful_realtime_text(text: str) -> bool:
    return _word_count(text.strip()) >= REALTIME_MIN_WORDS


def _log_transcript_line(meeting_id: int, speaker: str, text: str, is_immutable: bool) -> None:
    phase = "final" if is_immutable else "live"
    compact_text = " ".join(text.split())
    if len(compact_text) > 280:
        compact_text = f"{compact_text[:277]}..."
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
        inserted = await sync_final_transcript_from_vexa(
            meeting_id=meeting_id,
            platform=platform,
            native_id=native_id,
            api_key=api_key,
        )
        print(f"[Vexa] Final transcript sync for meeting {meeting_id} inserted {inserted} chunks")
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
            print(f"[Vexa] Failed to generate final report for meeting {meeting_id}: {exc}")


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
        if str(item.get("native_meeting_id", "")).strip() != native_id:
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

    inserted_count = 0

    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            print(f"[Vexa] Local meeting {meeting_id} not found during final transcript sync")
            return 0

        existing_rows = db.execute(
            select(TranscriptChunk.timestamp, TranscriptChunk.speaker, TranscriptChunk.text).where(
                TranscriptChunk.meeting_id == meeting_id
            )
        ).all()
        existing_keys: set[tuple[str, str, str]] = set()

        for timestamp_value, speaker_value, text_value in existing_rows:
            if isinstance(timestamp_value, datetime):
                timestamp = timestamp_value
            else:
                timestamp = _parse_absolute_start_time(str(timestamp_value))

            speaker = str(speaker_value or "Unknown").strip() or "Unknown"
            text = str(text_value or "").strip()
            if not text:
                continue
            existing_keys.add(_segment_identity(timestamp, speaker, text))

        for segment in segments:
            if not isinstance(segment, dict):
                continue

            text = str(segment.get("text", "")).strip()
            if not text:
                continue

            absolute_start_time = str(segment.get("absolute_start_time", "")).strip()
            if not absolute_start_time:
                continue

            speaker = str(segment.get("speaker") or "Unknown").strip() or "Unknown"
            timestamp = _parse_absolute_start_time(absolute_start_time)
            identity = _segment_identity(timestamp, speaker, text)
            if identity in existing_keys:
                continue

            db.add(
                TranscriptChunk(
                    meeting_id=meeting_id,
                    speaker=speaker,
                    text=text,
                    timestamp=timestamp,
                )
            )
            existing_keys.add(identity)
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
            await asyncio.sleep(poll_interval)
            continue

        status_value = str(remote_meeting.get("status", "")).strip().lower()
        if status_value:
            update_meeting_status(meeting_id, status_value)

        if status_value in TERMINAL_MEETING_STATUSES:
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


async def listen_to_vexa(
    meeting_id: int,
    platform: str,
    native_id: str,
    api_key: str | None = None,
) -> None:
    await asyncio.sleep(2)

    vexa_api_key = (api_key or os.getenv("VEXA_API_KEY", "")).strip()
    if not vexa_api_key:
        print(f"[Vexa] VEXA_API_KEY is missing; listener disabled for meeting {meeting_id}")
        return

    ws_url = _vexa_ws_url()
    auth_ws_url = _append_api_key_query_param(ws_url, vexa_api_key)
    subscription_message = {
        "action": "subscribe",
        "meetings": [{"platform": platform, "native_id": native_id}],
    }

    #seen_absolute_start_times: set[str] = set()
    seen_immutable_summaries: set[str] = set()
    seen_transcript_log_keys: set[tuple[str, str]] = set()
    controller = ControllerAgent()
    max_attempts = 6

    for attempt in range(1, max_attempts + 1):
        if _is_local_meeting_terminal(meeting_id):
            print(f"[Vexa] Meeting {meeting_id} is terminal; stopping listener")
            return

        try:
            async with _websocket_connect_with_headers(auth_ws_url, vexa_api_key) as websocket:
                await websocket.send(json.dumps(subscription_message))

                async for raw_message in websocket:
                    message = _message_from_raw(raw_message)
                    if message is None:
                        continue

                    message_type = str(message.get("type", "")).strip()
                    if message_type == "error":
                        error_code = str(message.get("error") or "").strip()
                        error_details = message.get("details")
                        print(
                            f"[Vexa] Stream error for meeting {meeting_id}: "
                            f"{error_code} details={error_details}"
                        )

                        details_text = str(error_details or "")
                        if error_code in {"invalid_subscribe_payload", "authorization_service_error"}:
                            raise RuntimeError(
                                f"Subscription rejected for meeting {meeting_id}: "
                                f"{error_code} {details_text}"
                            )
                        continue

                    if message_type == "subscribed":
                        subscribed_meetings = message.get("meetings")
                        if not isinstance(subscribed_meetings, list) or len(subscribed_meetings) == 0:
                            raise RuntimeError(
                                f"Subscription acknowledged without active meetings for meeting {meeting_id}"
                            )
                        print(f"[Vexa] Subscribed to meeting stream: {meeting_id}")
                        continue

                    if message_type == "meeting.status":
                        status_payload = message.get("payload")
                        if isinstance(status_payload, dict):
                            status_value = str(status_payload.get("status", "")).strip().lower()
                            if status_value:
                                update_meeting_status(meeting_id, status_value)
                                print(f"[Vexa] Meeting {meeting_id} status -> {status_value}")
                                if status_value in TERMINAL_MEETING_STATUSES:
                                    if status_value == "completed":
                                        await _finalize_completed_meeting(
                                            controller=controller,
                                            meeting_id=meeting_id,
                                            platform=platform,
                                            native_id=native_id,
                                            api_key=vexa_api_key,
                                            source="websocket",
                                        )
                                    return
                            continue

                    if message_type not in {"transcript.mutable", "transcript.immutable"}:
                        continue

                    is_immutable = message_type == "transcript.immutable"

                    segments = _extract_segments(message)
                    for segment in segments:
                        text = str(segment.get("text", "")).strip()
                        if not text:
                            continue

                        absolute_start_time = str(segment.get("absolute_start_time", "")).strip()
                        if not absolute_start_time:
                            continue

                        speaker = str(segment.get("speaker") or "Unknown").strip() or "Unknown"
                        timestamp = _parse_absolute_start_time(absolute_start_time)

                        transcript_log_key = (absolute_start_time, text)
                        if transcript_log_key not in seen_transcript_log_keys:
                            _log_transcript_line(
                                meeting_id=meeting_id,
                                speaker=speaker,
                                text=text,
                                is_immutable=is_immutable,
                            )
                            seen_transcript_log_keys.add(transcript_log_key)

                        chunk_id: int | None = None
                        with SessionLocal() as db:
                            meeting = db.get(Meeting, meeting_id)
                            if meeting is None:
                                print(f"[Vexa] Meeting {meeting_id} not found; skipping segment")
                                continue

                            existing_chunk = db.execute(
                                select(TranscriptChunk)
                                .where(
                                    TranscriptChunk.meeting_id == meeting_id,
                                    TranscriptChunk.speaker == speaker,
                                    TranscriptChunk.timestamp == timestamp,
                                )
                                .limit(1)
                            ).scalar_one_or_none()
                            if existing_chunk is not None:
                                chunk_id = existing_chunk.id
                                if existing_chunk.text != text:
                                    existing_chunk.text = text
                                    try:
                                        db.commit()
                                    except SQLAlchemyError as exc:
                                        db.rollback()
                                        print(f"[Vexa] Failed to update transcript chunk: {exc}")
                                        continue
                            else:
                                chunk = TranscriptChunk(
                                    meeting_id=meeting_id,
                                    speaker=speaker,
                                    text=text,
                                    timestamp=timestamp,
                                )
                                db.add(chunk)

                                try:
                                    db.commit()
                                    db.refresh(chunk)
                                    chunk_id = chunk.id
                                except SQLAlchemyError as exc:
                                    db.rollback()
                                    print(f"[Vexa] Failed to save transcript chunk: {exc}")
                                    continue

                        try:
                            if not is_immutable:
                                continue
                            if absolute_start_time in seen_immutable_summaries:
                                continue
                            if not _is_meaningful_realtime_text(text):
                                seen_immutable_summaries.add(absolute_start_time)
                                continue

                            summary = await controller.summarize(text)
                            seen_immutable_summaries.add(absolute_start_time)
                            if summary == "IGNORE":
                                continue

                            print(
                                f"[Vexa Summary] meeting={meeting_id} "
                                f"chunk={chunk_id} speaker={speaker}: {summary}"
                            )
                        except Exception as exc:  # noqa: BLE001
                            print(f"[Vexa] Failed to summarize chunk for meeting {meeting_id}: {exc}")

                return
        except ConnectionClosed as exc:
            if exc.code == 4401:
                print(
                    f"[Vexa] Unauthorized WebSocket (4401) for meeting {meeting_id}. "
                    "Verify VEXA_API_KEY is available in backend runtime and matches the key used for /bots."
                )
                return

            if attempt >= max_attempts:
                print(
                    f"[Vexa] Listener stopped for meeting {meeting_id} after {attempt} attempts: "
                    f"WebSocket closed code={exc.code} reason={exc.reason}"
                )
                return

            backoff_seconds = min(2 ** (attempt - 1), 20)
            print(
                f"[Vexa] WebSocket closed for meeting {meeting_id} (code={exc.code}). "
                f"Retrying in {backoff_seconds}s"
            )
            await asyncio.sleep(backoff_seconds)
        except Exception as exc:  # noqa: BLE001
            if attempt >= max_attempts:
                print(
                    f"[Vexa] Listener stopped for meeting {meeting_id} after {attempt} attempts: {exc}"
                )
                return

            backoff_seconds = min(2 ** (attempt - 1), 20)
            print(
                f"[Vexa] Listener error for meeting {meeting_id}: {exc}. "
                f"Retrying in {backoff_seconds}s"
            )
            await asyncio.sleep(backoff_seconds)
