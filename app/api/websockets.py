from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

"""
WebSocket Real-Time Transcript Ingestion API Module.

Provides WebSocket endpoints for receiving incoming speaker transcript utterances,
broadcasting transcript snapshots and live updates to connected web clients,
and triggering real-time LLM summarization and action item proposals.
"""


from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.api.ws_manager import manager
from app.db.models import AgentAction, Meeting, TranscriptChunk
from app.db.session import SessionLocal
from app.engine.controller import ControllerAgent
from app.engine.prompts import get_team_prompts

router = APIRouter()


@router.websocket("/api/ws/ingest/{meeting_id}")
async def ingest_transcript(websocket: WebSocket, meeting_id: int) -> None:
    """Handle real-time transcript chunk ingestion over WebSocket.

    Maintains a persistent bi-directional WebSocket connection for a meeting. On connect,
    loads team prompt customizations, pre-meeting memory context, and sends a full transcript
    snapshot. On receiving incoming transcript chunks:
    1. Persists the transcript chunk to database.
    2. Broadcasts the chunk to all connected clients via WebSocket.
    3. Triggers `ControllerAgent.summarize()` to run real-time AI analysis.
    4. Broadcasts insights and automatically persists proposed action items.

    Args:
        websocket (WebSocket): Incoming client WebSocket instance.
        meeting_id (int): Primary key ID of the target meeting.

    Raises:
        WebSocketDisconnect: Raised automatically when client drops connection.
    """
    await manager.connect(meeting_id, websocket)
    controller = ControllerAgent()


    # Resolve team prompts once per connection (cached for the duration of the meeting)
    with SessionLocal() as db:
        _meeting = db.get(Meeting, meeting_id)
        _team_id = _meeting.team_id if _meeting else None
        team_prompts = get_team_prompts(_team_id, db)

    if _team_id is not None:
        await controller.load_pre_meeting_context(team_id=_team_id)

    try:
        # Send full transcript snapshot on connect
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
            await manager.broadcast(
                meeting_id,
                {
                    "event": "transcript_snapshot",
                    "data": {
                        "chunks": [
                            {
                                "id": c.id,
                                "speaker": c.speaker,
                                "text": c.text,
                                "timestamp": c.timestamp.isoformat(),
                            }
                            for c in chunks
                        ]
                    },
                },
            )

        while True:
            payload = await websocket.receive_json()
            speaker = str(payload.get("speaker", "")).strip()
            text = str(payload.get("text", "")).strip()

            if not speaker or not text:
                await manager.broadcast(
                    meeting_id,
                    {
                        "event": "error",
                        "data": {
                            "error": "Payload must include non-empty 'speaker' and 'text'."
                        },
                    },
                )
                continue

            with SessionLocal() as db:
                meeting = db.get(Meeting, meeting_id)

                if meeting is None:
                    logger.info(
                        f"Meeting {meeting_id} not found. Auto-creating it for test..."
                    )
                    meeting = Meeting(
                        id=meeting_id,
                        vexa_meeting_id=f"vexa-mock-{meeting_id}",
                        title="Mock Agile Standup",
                        status="active",
                        created_at=datetime.now(timezone.utc),
                    )
                    db.add(meeting)
                    db.commit()
                    db.refresh(meeting)

                chunk = TranscriptChunk(
                    meeting_id=meeting_id,
                    speaker=speaker,
                    text=text,
                    timestamp=datetime.now(timezone.utc),
                )
                db.add(chunk)
                try:
                    db.commit()
                    db.refresh(chunk)
                except SQLAlchemyError as e:
                    db.rollback()
                    logger.info(f"DB Error: {e}")
                    await manager.broadcast(
                        meeting_id,
                        {
                            "event": "error",
                            "data": {
                                "error": "Database error while saving transcript chunk."
                            },
                        },
                    )
                    continue

            chunk_data: dict[str, Any] = {
                "id": chunk.id,
                "speaker": chunk.speaker,
                "text": chunk.text,
                "timestamp": chunk.timestamp.isoformat(),
            }
            await manager.broadcast(
                meeting_id, {"event": "transcript_chunk", "data": chunk_data}
            )

            try:
                with SessionLocal() as db:
                    pending_rows = (
                        db.execute(
                            select(AgentAction.content).where(
                                AgentAction.meeting_id == meeting_id,
                                AgentAction.status == "pending",
                            )
                        )
                        .scalars()
                        .all()
                    )
                result = await controller.summarize(
                    text,
                    existing_actions=pending_rows,
                    team_prompts=team_prompts,
                )
                logger.info(f"[Ollama Result] {speaker}: {result}")
            except Exception as exc:
                logger.info(f"Ollama Error: {exc}")
                await manager.broadcast(
                    meeting_id,
                    {
                        "event": "error",
                        "data": {"error": f"Failed to summarize text: {exc}"},
                    },
                )
                continue

            scrum = result.get("scrum_master", {})
            summary_text = scrum.get("text", "IGNORE")
            if summary_text and summary_text.strip().upper() != "IGNORE":
                await manager.broadcast(
                    meeting_id,
                    {
                        "event": "insight",
                        "data": {"role": "scrum_master", "text": summary_text},
                    },
                )

            proposal_data = scrum.get("proposal")
            if proposal_data:
                with SessionLocal() as db:
                    agent_action = AgentAction(
                        meeting_id=meeting_id,
                        agent_role="scrum_master",
                        action_type=proposal_data["type"],
                        content=proposal_data["content"],
                        status="pending",
                    )
                    db.add(agent_action)
                    db.commit()
                    db.refresh(agent_action)
                await manager.broadcast(
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

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for meeting {meeting_id}")
    finally:
        manager.disconnect(meeting_id, websocket)
