from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

"""
Vexa API Integration and Transcript Sync Engine Module.

Coordinates meeting lifecycle monitoring, remote Vexa bot status polling,
transcript segment synchronization, speaker list extraction, real-time broadcast,
and automatic final report generation upon meeting completion.
"""


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
from app.engine.summary_tasks import (
    active_summary_thoughts,
    finalizing_meetings,
    summary_starts,
    summary_tasks,
)

TERMINAL_MEETING_STATUSES = {"completed", "failed"}
FINALIZATION_PROGRESS_INTERVAL_SECONDS = 5
SYSTEM_PARTICIPANT_NAMES = {"meeting audio"}

_seen_chunk_sigs: dict[int, set[str]] = {}
_finalizing = finalizing_meetings


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
        or "http://gateway:8000"
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
        logger.info(
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
    if meeting_id in _finalizing:
        logger.info(f"[Vexa] Finalization already in progress for meeting {meeting_id}; skipping ({source})")
        return
    _finalizing.add(meeting_id)
    summary_starts.setdefault(meeting_id, time.time())
    active_summary_thoughts[meeting_id] = []

    logger.info(f"[Vexa] Starting finalization for meeting {meeting_id} (source={source})")
    progress_done = asyncio.Event()
    progress_task = asyncio.create_task(
        _emit_finalization_progress(meeting_id, progress_done, source)
    )
    started = time.monotonic()

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

    try:
        upserted = await sync_final_transcript_from_vexa(
            meeting_id=meeting_id,
            platform=platform,
            native_id=native_id,
            api_key=api_key,
        )
        logger.info(
            f"[Vexa] Final transcript sync for meeting {meeting_id} upserted {upserted} chunks"
        )
        await sync_speakers_from_vexa(meeting_id, platform, native_id, api_key)
        await _generate_and_log_final_report(
            controller, meeting_id, on_thought=on_summary_thought
        )
    except asyncio.CancelledError:
        logger.info(f"[Vexa] Finalization for meeting {meeting_id} cancelled by user")
        raise
    finally:
        progress_done.set()
        await progress_task
        summary_tasks.pop(meeting_id, None)
        summary_starts.pop(meeting_id, None)
        _finalizing.discard(meeting_id)

    duration = round(time.monotonic() - started, 2)
    logger.info(f"[Vexa] Finalization complete for meeting {meeting_id} in {duration}s")


async def _generate_and_log_final_report(
    controller: ControllerAgent,
    meeting_id: int,
    on_thought: Any | None = None,
) -> None:
    """Trigger ControllerAgent to generate and persist final meeting summary report.

    Args:
        controller (ControllerAgent): ControllerAgent orchestrator instance.
        meeting_id (int): Target meeting primary key ID.
        on_thought (Any | None): Optional live thought streaming callback.
    """
    with SessionLocal() as db:
        try:
            meeting = db.get(Meeting, meeting_id)
            team_id = meeting.team_id if meeting else None
            await controller.generate_final_report(
                meeting_id, db, team_id=team_id, on_thought=on_thought
            )
        except asyncio.CancelledError:
            logger.info(f"[Vexa] Final report generation for meeting {meeting_id} was cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info(
                f"[Vexa] Failed to generate final report for meeting {meeting_id}: {type(exc).__name__}: {str(exc)}"
            )


def _is_local_meeting_terminal_sync(meeting_id: int) -> bool:
    """Synchronous check if local meeting status is in terminal state.

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


async def _is_local_meeting_terminal(meeting_id: int) -> bool:
    """Async check if local meeting status is in terminal state.

    Wraps the synchronous DB call in asyncio.to_thread() to avoid blocking
    the event loop — critical during real-time transcript polling where any
    event-loop stall delays audio → transcript delivery.

    Args:
        meeting_id (int): Target meeting primary key ID.

    Returns:
        bool: True if terminal or meeting record does not exist, False otherwise.
    """
    return await asyncio.to_thread(_is_local_meeting_terminal_sync, meeting_id)


def _extract_latest_remote_meeting(
    meetings_payload: Any,
    platform: str,
    native_id: str,
    vexa_remote_id: str | None = None,
) -> dict[str, Any] | None:
    """Filter Vexa meetings API JSON response to find latest matching remote meeting.

    Args:
        meetings_payload (Any): JSON response from Vexa /meetings endpoint.
        platform (str): Meeting platform name.
        native_id (str): Native platform meeting ID.
        vexa_remote_id (str | None): Optional Vexa numeric ID string.

    Returns:
        dict[str, Any] | None: Matching meeting dictionary or None.
    """
    if not isinstance(meetings_payload, dict):
        return None

    meetings = meetings_payload.get("meetings")
    if not isinstance(meetings, list):
        return None

    if vexa_remote_id:
        try:
            target_id = int(vexa_remote_id)
            for item in meetings:
                if isinstance(item, dict) and item.get("id") == target_id:
                    return item
        except (ValueError, TypeError):
            pass

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
            logger.info(f"[Vexa] Failed to update meeting status for {meeting_id}: {exc}")


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
            logger.info(f"[Vexa] Speaker sync attempt {attempt} failed for meeting {meeting_id}: {exc}")
            continue

        remote_meeting = _extract_latest_remote_meeting(payload, platform, native_id)
        if not remote_meeting:
            logger.info(f"[Vexa] Speaker sync attempt {attempt}: meeting {meeting_id} not found in Vexa yet")
            continue

        raw = remote_meeting.get("data", {}).get("participants", [])
        speakers = _filter_speakers(raw if isinstance(raw, list) else [])

        if not speakers:
            logger.info(f"[Vexa] Speaker sync attempt {attempt}: no speakers yet for meeting {meeting_id}")
            continue

        with SessionLocal() as db:
            meeting = db.get(Meeting, meeting_id)
            if meeting is not None:
                meeting.speakers = speakers
                try:
                    db.commit()
                    logger.info(f"[Vexa] Synced {len(speakers)} speakers for meeting {meeting_id}: {speakers}")
                except SQLAlchemyError as exc:
                    db.rollback()
                    logger.info(f"[Vexa] Failed to persist speakers for meeting {meeting_id}: {exc}")
        return speakers

    logger.info(f"[Vexa] Speaker sync exhausted all retries for meeting {meeting_id}")
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
        logger.info(
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
        logger.info(
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
            logger.info(
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
            logger.info(
                f"[Vexa] No canonical segments from Vexa for meeting {meeting_id}; "
                "preserving existing transcript chunks"
            )

        try:
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            logger.info(
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
    vexa_remote_id: str | None = None,
) -> None:
    """Asynchronous polling loop that monitors Vexa meeting lifecycle state until terminal state is reached.

    Args:
        meeting_id (int): Local meeting primary key ID.
        platform (str): Platform name (e.g. 'google_meet').
        native_id (str): Native meeting identifier.
        api_key (str | None): Vexa API key string.
        timeout_seconds (int): Maximum polling duration in seconds (default 7200s).
        vexa_remote_id (str | None): Optional Vexa numeric ID string.
    """
    vexa_api_key = (api_key or os.getenv("VEXA_API_KEY", "")).strip()
    if not vexa_api_key:
        logger.info(f"[Vexa] Cannot poll meeting lifecycle for {meeting_id}: missing API key")
        return

    base_url = _vexa_api_base_url()
    meetings_url = f"{base_url}/meetings"
    poll_interval = int(os.getenv("VEXA_MEETING_POLL_INTERVAL_SECONDS", "5"))
    if poll_interval < 3:
        poll_interval = 3

    deadline = time.monotonic() + max(timeout_seconds, poll_interval)
    controller = ControllerAgent()
    seen_in_vexa = False
    consecutive_not_found = 0
    while time.monotonic() < deadline:
        if await _is_local_meeting_terminal(meeting_id):
            return

        headers = {"X-API-Key": vexa_api_key}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                meetings_result = await client.get(meetings_url, headers=headers)
        except Exception as exc:
            logger.info(f"[Vexa] Poll gather failed for meeting {meeting_id}: {exc}")
            await asyncio.sleep(poll_interval)
            continue

        # Meetings endpoint check
        if not isinstance(meetings_result, httpx.Response):
            logger.info(f"[Vexa] Meeting poll failed for meeting {meeting_id}: {meetings_result}")
            await asyncio.sleep(poll_interval)
            continue

        try:
            meetings_result.raise_for_status()
            payload: Any = meetings_result.json() if meetings_result.content else {}
        except httpx.HTTPError as exc:
            logger.info(f"[Vexa] Meeting poll HTTP error for meeting {meeting_id}: {exc}")
            await asyncio.sleep(poll_interval)
            continue

        remote_meeting = _extract_latest_remote_meeting(
            payload, platform, native_id, vexa_remote_id=vexa_remote_id
        )
        if not remote_meeting:
            if seen_in_vexa:
                consecutive_not_found += 1
                logger.info(
                    f"[Vexa] Meeting {meeting_id} not found in Vexa list "
                    f"(consecutive={consecutive_not_found})"
                )
                if consecutive_not_found >= 2:
                    logger.info(
                        f"[Vexa] Meeting {meeting_id} disappeared from Vexa after "
                        f"{consecutive_not_found} polls; treating as completed"
                    )
                    update_meeting_status(meeting_id, "completed")
                    await _finalize_completed_meeting(
                        controller=controller,
                        meeting_id=meeting_id,
                        platform=platform,
                        native_id=native_id,
                        api_key=vexa_api_key,
                        source="poller-disappear",
                    )
                    return
            await asyncio.sleep(poll_interval)
            continue

        seen_in_vexa = True
        consecutive_not_found = 0
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

    logger.info(
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
        logger.info(
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

    summary_queue = asyncio.Queue()

    async def _summary_worker():
        while True:
            chunk_texts = []
            try:
                chunk_texts.append(await summary_queue.get())
            except asyncio.CancelledError:
                break
                
            while not summary_queue.empty():
                try:
                    chunk_texts.append(summary_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
                    
            combined_text = " ".join(chunk_texts).strip()
            if not combined_text:
                for _ in chunk_texts:
                    summary_queue.task_done()
                continue

            queue_depth = summary_queue.qsize()
            if queue_depth > 0:
                logger.info(
                    f"[Summary Worker] Processing batch of {len(chunk_texts)} chunks, "
                    f"{queue_depth} more queued"
                )

            t0 = asyncio.get_event_loop().time()
            try:
                with SessionLocal() as db_session:
                    pending_rows = (
                        db_session.execute(
                            select(AgentAction.content).where(
                                AgentAction.meeting_id == meeting_id,
                                AgentAction.status == "pending",
                            )
                        )
                        .scalars()
                        .all()
                    )
                result = await controller.summarize(
                    combined_text,
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
                        meeting_id,
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
                            meeting_id=meeting_id,
                            agent_role="scrum_master",
                            action_type=proposal_data["type"],
                            content=proposal_data["content"],
                            status="pending",
                        )
                        db2.add(agent_action)
                        db2.commit()
                        db2.refresh(agent_action)
                    await ws_manager.broadcast(
                        meeting_id,
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
                    logger.info(
                        f"[Action Proposal] {proposal_data['type']}: {proposal_data['content']}"
                    )
            except Exception as exc:
                logger.info(
                    f"[Vexa] Ollama analysis failed for batched chunks: {exc}"
                )
            finally:
                elapsed = asyncio.get_event_loop().time() - t0
                logger.info(
                    f"[Summary Worker] Batch of {len(chunk_texts)} chunks processed in {elapsed:.2f}s"
                )
                
            for _ in chunk_texts:
                summary_queue.task_done()

    worker_task = asyncio.create_task(_summary_worker())

    while not await _is_local_meeting_terminal(meeting_id):
        try:
            old_sigs = _seen_chunk_sigs.get(meeting_id, set())
            upserted = await sync_final_transcript_from_vexa(
                meeting_id=meeting_id,
                platform=platform,
                native_id=native_id,
                api_key=vexa_api_key,
            )
            if upserted > 0:
                logger.info(
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

                        # Dispatch LLM analysis into the background queue to batch and prevent Ollama overload
                        summary_queue.put_nowait(c.text)
        except Exception as exc:
            logger.info(f"[Vexa] Transcript poll failed for meeting {meeting_id}: {exc}")

        await asyncio.sleep(poll_interval)

    logger.info(f"[Vexa] Meeting {meeting_id} is terminal; stopping transcript polling")
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

