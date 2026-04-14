from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import Meeting, TranscriptChunk
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
                    {"ok": False, "error": "Payload must include non-empty 'speaker' and 'text'."}
                )
                continue

            with SessionLocal() as db:
                meeting = db.get(Meeting, meeting_id)

                # --- AUTO-CREATE MEETING FOR MVP TESTING ---
                if meeting is None:
                    print(f"Meeting {meeting_id} not found. Auto-creating it for test...")
                    meeting = Meeting(
                        id=meeting_id,
                        vexa_meeting_id=f"vexa-mock-{meeting_id}",
                        title="Mock Agile Standup",
                        status="active",
                        created_at=datetime.now(timezone.utc)
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
                        {"ok": False, "error": "Database error while saving transcript chunk."}
                    )
                    continue

            # Pass the text to Ollama!
            try:
                summary = await controller.summarize(text)
                print(f"[Ollama Summary] {speaker}: {summary}")
            except Exception as exc:
                print(f"Ollama Error: {exc}")
                await websocket.send_json(
                    {"ok": False, "error": f"Failed to summarize text: {exc}"}
                )
                continue

            await websocket.send_json(
                {
                    "ok": True,
                    "meeting_id": meeting_id,
                    "chunk_id": chunk.id,
                    "summary": summary,
                }
            )

    except WebSocketDisconnect:
        print(f"WebSocket disconnected for meeting {meeting_id}")
        return