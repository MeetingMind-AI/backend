from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import AgentAction, Meeting, TranscriptChunk
from app.db.session import SessionLocal
from app.engine.controller import ControllerAgent

router = APIRouter()


@router.websocket("/api/ws/ingest/{meeting_id}")
async def ingest_transcript(websocket: WebSocket, meeting_id: int) -> None:
    await websocket.accept()
    controller = ControllerAgent()

    try:
        while True:
            payload = await websocket.receive_json()
            speaker = str(payload.get("speaker", "")).strip()
            text = str(payload.get("text", "")).strip()

            if not speaker or not text:
                await websocket.send_json(
                    {
                        "ok": False,
                        "error": "Payload must include non-empty 'speaker' and 'text'.",
                    }
                )
                continue

            with SessionLocal() as db:
                meeting = db.get(Meeting, meeting_id)

                # --- AUTO-CREATE MEETING FOR MVP TESTING ---
                if meeting is None:
                    print(
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
                # -------------------------------------------

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
                    print(f"DB Error: {e}")
                    await websocket.send_json(
                        {
                            "ok": False,
                            "error": "Database error while saving transcript chunk.",
                        }
                    )
                    continue

            # Pass the text to Ollama (summary + proposal detection in one call)
            try:
                result = await controller.summarize(text)
                print(f"[Ollama Result] {speaker}: {result}")
            except Exception as exc:
                print(f"Ollama Error: {exc}")
                await websocket.send_json(
                    {"ok": False, "error": f"Failed to summarize text: {exc}"}
                )
                continue

            scrum = result.get("scrum_master", {})
            response_payload: dict[str, Any] = {
                "ok": True,
                "meeting_id": meeting_id,
                "chunk_id": chunk.id,
                "summary": scrum.get("text", "IGNORE"),
            }

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
                response_payload["proposal"] = {
                    "id": agent_action.id,
                    "type": proposal_data["type"],
                    "content": proposal_data["content"],
                    "status": "pending",
                }
                print(
                    f"[Action Proposal] {proposal_data['type']}: {proposal_data['content']}"
                )

            await websocket.send_json(response_payload)

    except WebSocketDisconnect:
        print(f"WebSocket disconnected for meeting {meeting_id}")
        return
