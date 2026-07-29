"""
Vexa API Integration and Transcript Sync Engine Module.

Coordinates meeting lifecycle monitoring, remote Vexa bot status polling,
transcript segment synchronization, speaker list extraction, real-time broadcast,
and automatic final report generation upon meeting completion.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from app.api.ws_manager import ConnectionManager
from app.db.models import AgentAction, Meeting, TranscriptChunk
from app.db.session import SessionLocal
from app.engine.controller import ControllerAgent
from app.engine.prompts import get_team_prompts

TERMINAL_MEETING_STATUSES = {"completed", "failed"}
FINALIZATION_PROGRESS_INTERVAL_SECONDS = 5
SYSTEM_PARTICIPANT_NAMES = {"meeting audio"}

_seen_chunk_sigs: dict[int, set[str]] = {}


def _get_chunk_sigs(meeting_id: int) -> set[str]:
    """Generate deduplication signature strings for existing meeting transcript chunks.

    Args:
        meeting_id (int): Target meeting primary key ID.

    Returns:
        set[str]: Set of signature strings formatted as 'Speaker|||Text'.
    """
    with SessionLocal() as db:
        chunks = (
            db.execute(
                select(TranscriptChunk).where(TranscriptChunk.meeting_id == meeting_id)
            )
            .scalars()
            .all()
        )
        return {f"{c.speaker}|||{c.text.strip()}" for c in chunks}


def _vexa_api_base_url() -> str:
    """Resolve and normalize the base HTTP API URL for the Vexa transcription service.

    Reads environment variables `VEXA_API_BASE_URL` or `VEXA_API_URL`.

    Returns:
        str: Normalized base URL string without trailing slash or '/bots' suffix.
    """
    configured = (
        os.getenv("VEXA_API_BASE_URL")
        or os.getenv("VEXA_API_URL")
        or "http://host.docker.internal:18056"
    ).strip()

    if configured.endswith("/bots"):
        configured = configured[:-5]

    return configured.rstrip("/")


def _parse_absolute_start_time(value: str) -> datetime:
    """Parse ISO timestamp string into timezone-aware UTC datetime object.

    Args:
        value (str): ISO 8601 timestamp string.

    Returns:
        datetime: UTC datetime instance.
    """
    normalized = value.replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.now(timezone.utc)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_optional_iso_datetime(value: str) -> datetime | None:
    """Parse optional ISO timestamp string, returning None if empty or invalid.

    Args:
        value (str): ISO timestamp string.

    Returns:
        datetime | None: Parsed UTC datetime object or None.
    """
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
    """Determine whether an incoming transcript segment should replace an existing segment.

    Args:
        existing_segment (dict[str, Any] | None): Existing segment payload dictionary.
        incoming_segment (dict[str, Any]): Incoming segment payload dictionary.

    Returns:
        bool: True if incoming segment should replace existing segment, False otherwise.
    """
    incoming_text = str(incoming_segment.get("text", "")).strip()
    if not incoming_text:
        return False

    if existing_segment is None:
        return True

    existing_text = str(existing_segment.get("text", "")).strip()
    incoming_updated_at = _parse_optional_iso_datetime(
        str(incoming_segment.get("updated_at") or "")
    )
    existing_updated_at = _parse_optional_iso_datetime(
        str(existing_segment.get("updated_at") or "")
    )

    if incoming_updated_at and existing_updated_at:
        if incoming_updated_at > existing_updated_at:
            return True
        if incoming_updated_at < existing_updated_at:
            return False

    if existing_text.endswith("...") and not incoming_text.endswith("..."):
        return True

    return len(incoming_text) >= len(existing_text)


async def _emit_finalization_progress(
    meeting_id: int, done: asyncio.Event, source: str
) -> None:
    """Periodically print progress log messages while meeting finalization is running.

    Args:
        meeting_id (int): Primary key ID of the meeting.
        done (asyncio.Event): Event set when finalization completes.
        source (str): Source identifier (e.g. 'poller' or 'webhook').
    """
    elapsed = 0
    while not done.is_set():
        print(
            f"[Vexa] Finalization in progress for meeting {meeting_id} "
            f"(source={source}, elapsed={elapsed}s)"
        )
        try:
            await asyncio.wait_for(
                done.wait(), timeout=FINALIZATION_PROGRESS_INTERVAL_SECONDS
            )
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
    """Coordinate final transcript sync, speaker list sync, and final report generation for a completed meeting.

    Args:
        controller (ControllerAgent): ControllerAgent orchestrator instance.
        meeting_id (int): Primary key ID of target meeting.
        platform (str): Meeting platform name (e.g. 'google_meet', 'teams', 'zoom').
        native_id (str): Native meeting URL or code.
        api_key (str): Vexa API key string.
        source (str): Trigger source string.
    """
    print(f"[Vexa] Starting finalization for meeting {meeting_id} (source={source})")
    progress_done = asyncio.Event()
    progress_task = asyncio.create_task(
        _emit_finalization_progress(meeting_id, progress_done, source)
    )
    started = time.monotonic()

    try:
        upserted = await sync_final_transcript_from_vexa(
            meeting_id=meeting_id,
            platform=platform,
            native_id=native_id,
            api_key=api_key,
        )
        print(
            f"[Vexa] Final transcript sync for meeting {meeting_id} upserted {upserted} chunks"
        )
        await sync_speakers_from_vexa(meeting_id, platform, native_id, api_key)
        await _generate_and_log_final_report(controller, meeting_id)
    finally:
        progress_done.set()
        await progress_task

    duration = round(time.monotonic() - started, 2)
    print(f"[Vexa] Finalization complete for meeting {meeting_id} in {duration}s")


async def _generate_and_log_final_report(
    controller: ControllerAgent, meeting_id: int
) -> None:
    """Trigger ControllerAgent to generate and persist final meeting summary report.

    Args:
        controller (ControllerAgent): ControllerAgent orchestrator instance.
        meeting_id (int): Target meeting primary key ID.
    """
    with SessionLocal() as db:
        try:
            meeting = db.get(Meeting, meeting_id)
            team_id = meeting.team_id if meeting else None
            await controller.generate_final_report(meeting_id, db, team_id=team_id)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[Vexa] Failed to generate final report for meeting {meeting_id}: {type(exc).__name__}: {str(exc)}"
            )


def _is_local_meeting_terminal(meeting_id: int) -> bool:
    """Check if local meeting status is in terminal state ('completed' or 'failed').

    Args:
        meeting_id (int): Target meeting primary key ID.

    Returns:
        bool: True if terminal or meeting record does not exist, False otherwise.
    """
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
    """Filter Vexa meetings API JSON response to find latest matching remote meeting.

    Args:
        meetings_payload (Any): JSON response from Vexa /meetings endpoint.
        platform (str): Meeting platform name.
        native_id (str): Native platform meeting ID.

    Returns:
        dict[str, Any] | None: Matching meeting dictionary or None.
    """
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
        """Extract updated_at and created_at timestamps for remote meeting comparison.

        Args:
            item (dict[str, Any]): Remote meeting item.

        Returns:
            tuple[str, str]: Sorting key tuple of (updated_at, created_at).
        """
        updated = str(item.get("updated_at") or "")
        created = str(item.get("created_at") or "")
        return (updated, created)

    return max(matched, key=sort_key)


def update_meeting_status(meeting_id: int, status_value: str) -> None:
    """Persist updated status for a local meeting record.

    Args:
        meeting_id (int): Primary key ID of target meeting.
        status_value (str): New status string (e.g. 'running', 'completed', 'failed').
    """
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


def _filter_speakers(raw: list[Any]) -> list[str]:
    """Filter out system participant names (e.g. 'meeting audio') from speaker lists.

    Args:
        raw (list[Any]): Raw speaker/participant names list.

    Returns:
        list[str]: Filtered participant name strings.
    """
    return [
        name for p in raw
        if (name := str(p).strip()) and name.lower() not in SYSTEM_PARTICIPANT_NAMES
    ]


async def sync_speakers_from_vexa(
    meeting_id: int,
    platform: str,
    native_id: str,
    api_key: str | None = None,
) -> list[str]:
    """Fetch participant list from Vexa API and store speakers in local Meeting record.

    Args:
        meeting_id (int): Primary key ID of local meeting.
        platform (str): Meeting platform name.
        native_id (str): Native meeting identifier.
        api_key (str | None): Optional Vexa API key string.

    Returns:
        list[str]: List of synced speaker names.
    """
    vexa_api_key = (api_key or os.getenv("VEXA_API_KEY", "")).strip()
    if not vexa_api_key:
        return []

    base_url = _vexa_api_base_url()
    delays = [2, 8, 20]

    for attempt, delay in enumerate(delays, start=1):
        await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{base_url}/meetings", headers={"X-API-Key": vexa_api_key}
                )
            response.raise_for_status()
            payload: Any = response.json() if response.content else {}
        except httpx.HTTPError as exc:
            print(f"[Vexa] Speaker sync attempt {attempt} failed for meeting {meeting_id}: {exc}")
            continue

        remote_meeting = _extract_latest_remote_meeting(payload, platform, native_id)
        if not remote_meeting:
            print(f"[Vexa] Speaker sync attempt {attempt}: meeting {meeting_id} not found in Vexa yet")
            continue

        raw = remote_meeting.get("data", {}).get("participants", [])
        speakers = _filter_speakers(raw if isinstance(raw, list) else [])

        if not speakers:
            print(f"[Vexa] Speaker sync attempt {attempt}: no speakers yet for meeting {meeting_id}")
            continue

        with SessionLocal() as db:
            meeting = db.get(Meeting, meeting_id)
            if meeting is not None:
                meeting.speakers = speakers
                try:
                    db.commit()
                    print(f"[Vexa] Synced {len(speakers)} speakers for meeting {meeting_id}: {speakers}")
                except SQLAlchemyError as exc:
                    db.rollback()
                    print(f"[Vexa] Failed to persist speakers for meeting {meeting_id}: {exc}")
        return speakers

    print(f"[Vexa] Speaker sync exhausted all retries for meeting {meeting_id}")
    return []


async def sync_final_transcript_from_vexa(
    meeting_id: int,
    platform: str,
    native_id: str,
    api_key: str | None = None,
) -> int:
    """Fetch merged transcript segments from Vexa API and replace local TranscriptChunk records.

    Args:
        meeting_id (int): Primary key ID of local meeting.
        platform (str): Meeting platform string.
        native_id (str): Native meeting URL/ID string.
        api_key (str | None): Vexa API key string.

    Returns:
        int: Number of transcript chunks upserted into database.
    """
    vexa_api_key = (api_key or os.getenv("VEXA_API_KEY", "")).strip()
    if not vexa_api_key:
        print(
            f"[Vexa] Cannot sync final transcript for meeting {meeting_id}: missing API key"
        )
        return 0

    base_url = _vexa_api_base_url()
    url = f"{base_url}/transcripts/{platform}/{native_id}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers={"X-API-Key": vexa_api_key})
        response.raise_for_status()
        payload: Any = response.json() if response.content else {}
    except httpx.HTTPError as exc:
        print(
            f"[Vexa] Failed to fetch final transcript for meeting {meeting_id}: {exc}"
        )
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
            print(
                f"[Vexa] Local meeting {meeting_id} not found during final transcript sync"
            )
            return 0

        new_chunks: list[TranscriptChunk] = []
        for segment in canonical_segments:
            text = str(segment.get("text", "")).strip()
            if not text:
                continue

            absolute_start_time = str(segment.get("absolute_start_time", "")).strip()
            if not absolute_start_time:
                continue

            speaker = str(segment.get("speaker") or "Unknown").strip() or "Unknown"
            timestamp = _parse_absolute_start_time(absolute_start_time)

            new_chunks.append(
                TranscriptChunk(
                    meeting_id=meeting_id,
                    speaker=speaker,
                    text=text,
                    timestamp=timestamp,
                )
            )
            inserted_count += 1

        if new_chunks:
            # Only delete existing chunks when there are replacement segments to insert.
            # If Vexa returns empty data, we preserve the live-captured transcript.
            db.execute(
                delete(TranscriptChunk).where(TranscriptChunk.meeting_id == meeting_id)
            )
            db.add_all(new_chunks)
        else:
            print(
                f"[Vexa] No canonical segments from Vexa for meeting {meeting_id}; "
                "preserving existing transcript chunks"
            )

        try:
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            print(
                f"[Vexa] Failed to persist final transcript for meeting {meeting_id}: {exc}"
            )
            return 0

    return inserted_count


async def monitor_meeting_until_terminal(
    meeting_id: int,
    platform: str,
    native_id: str,
    api_key: str | None = None,
    timeout_seconds: int = 7200,
) -> None:
    """Asynchronous polling loop that monitors Vexa meeting lifecycle state until terminal state is reached.

    Args:
        meeting_id (int): Local meeting primary key ID.
        platform (str): Platform name (e.g. 'google_meet').
        native_id (str): Native meeting identifier.
        api_key (str | None): Vexa API key string.
        timeout_seconds (int): Maximum polling duration in seconds (default 7200s).
    """
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
                response = await client.get(
                    meetings_url, headers={"X-API-Key": vexa_api_key}
                )
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

    print(
        f"[Vexa] Meeting poll timeout for meeting {meeting_id} after {timeout_seconds}s"
    )


async def poll_transcripts_from_vexa(
    meeting_id: int,
    platform: str,
    native_id: str,
    api_key: str | None = None,
    poll_interval: int = 3,
    ws_manager: ConnectionManager | None = None,
) -> None:
    """Asynchronous polling loop that fetches real-time transcripts, broadcasts to WebSocket clients, and triggers AI analysis.

    Args:
        meeting_id (int): Primary key ID of local meeting.
        platform (str): Platform name string.
        native_id (str): Native meeting URL or code.
        api_key (str | None): Vexa API key string.
        poll_interval (int): Seconds between poll attempts (default 3s).
        ws_manager (ConnectionManager | None): Active WebSocket connection manager for broadcasting.
    """
    vexa_api_key = (api_key or os.getenv("VEXA_API_KEY", "")).strip()
    if not vexa_api_key:
        print(
            f"[Vexa] VEXA_API_KEY is missing; transcript polling disabled for meeting {meeting_id}"
        )
        return

    controller = ControllerAgent()
    _seen_chunk_sigs[meeting_id] = _get_chunk_sigs(meeting_id)
    team_prompts = None
    with SessionLocal() as db:
        meeting = db.get(Meeting, meeting_id)
        team_id = meeting.team_id if meeting else None
        team_prompts = get_team_prompts(team_id, db)

    while not _is_local_meeting_terminal(meeting_id):
        try:
            old_sigs = _seen_chunk_sigs.get(meeting_id, set())
            upserted = await sync_final_transcript_from_vexa(
                meeting_id=meeting_id,
                platform=platform,
                native_id=native_id,
                api_key=vexa_api_key,
            )
            if upserted > 0:
                print(
                    f"[Vexa] Synced {upserted} clean transcript segments for meeting {meeting_id}"
                )

                new_sigs = _get_chunk_sigs(meeting_id)
                added_sigs = new_sigs - old_sigs
                _seen_chunk_sigs[meeting_id] = new_sigs

                if added_sigs:
                    with SessionLocal() as db:
                        chunks = (
                            db.execute(
                                select(TranscriptChunk)
                                .where(TranscriptChunk.meeting_id == meeting_id)
                                .order_by(TranscriptChunk.timestamp.asc())
                            )
                            .scalars()
                            .all()
                        )

                    for c in chunks:
                        sig = f"{c.speaker}|||{c.text.strip()}"
                        if sig not in added_sigs:
                            continue

                        chunk_data = {
                            "id": c.timestamp.isoformat(),
                            "speaker": c.speaker,
                            "text": c.text,
                            "timestamp": c.timestamp.isoformat(),
                            "is_final": not c.text.endswith("..."),
                        }
                        if ws_manager:
                            await ws_manager.broadcast(
                                meeting_id,
                                {"event": "transcript_chunk", "data": chunk_data},
                            )

                        # --- Background Summarization Task ---
                        async def _process_chunk_summary(chunk_text: str, current_meeting_id: int):
                            try:
                                with SessionLocal() as db_session:
                                    pending_rows = (
                                        db_session.execute(
                                            select(AgentAction.content).where(
                                                AgentAction.meeting_id == current_meeting_id,
                                                AgentAction.status == "pending",
                                            )
                                        )
                                        .scalars()
                                        .all()
                                    )
                                result = await controller.summarize(
                                    chunk_text,
                                    existing_actions=pending_rows,
                                    team_prompts=team_prompts,
                                )
                                scrum = result.get("scrum_master", {})
                                summary_text = scrum.get("text", "IGNORE")
                                if (
                                    summary_text
                                    and summary_text.strip().upper() != "IGNORE"
                                    and ws_manager
                                ):
                                    await ws_manager.broadcast(
                                        current_meeting_id,
                                        {
                                            "event": "insight",
                                            "data": {
                                                "role": "scrum_master",
                                                "text": summary_text,
                                            },
                                        },
                                    )

                                proposal_data = scrum.get("proposal")
                                if proposal_data and ws_manager:
                                    with SessionLocal() as db2:
                                        agent_action = AgentAction(
                                            meeting_id=current_meeting_id,
                                            agent_role="scrum_master",
                                            action_type=proposal_data["type"],
                                            content=proposal_data["content"],
                                            status="pending",
                                        )
                                        db2.add(agent_action)
                                        db2.commit()
                                        db2.refresh(agent_action)
                                    await ws_manager.broadcast(
                                        current_meeting_id,
                                        {
                                            "event": "proposal",
                                            "data": {
                                                "id": agent_action.id,
                                                "type": proposal_data["type"],
                                                "content": proposal_data["content"],
                                                "status": "pending",
                                            },
                                        },
                                    )
                                    print(
                                        f"[Action Proposal] {proposal_data['type']}: {proposal_data['content']}"
                                    )
                            except Exception as exc:
                                print(
                                    f"[Vexa] Ollama analysis failed for chunk: {exc}"
                                )
                        
                        # Dispatch LLM analysis to the background so it doesn't block the next transcript fetch
                        asyncio.create_task(_process_chunk_summary(c.text, meeting_id))
        except Exception as exc:
            print(f"[Vexa] Transcript poll failed for meeting {meeting_id}: {exc}")

        await asyncio.sleep(poll_interval)

    print(f"[Vexa] Meeting {meeting_id} is terminal; stopping transcript polling")

