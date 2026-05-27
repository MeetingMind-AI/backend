import asyncio
from datetime import datetime, timezone, timedelta
import json
import uuid

from app.db.session import SessionLocal
from app.db.models import AgentAction, Meeting, TranscriptChunk
from app.engine.controller import ControllerAgent, get_memory


MOCK_TRANSCRIPT = [
    "Tech Lead: We have decided to migrate from Redis to RabbitMQ for the message broker next quarter due to scaling issues.",
    "Product Manager: Alice, can you update the architecture documentation by Friday?",
    "Alice: I already sent the updated Jira ticket to DevOps yesterday.",
    "Scrum Master: What about the Q3 budget for the new cloud instances?",
    "Product Manager: We don't have time today, let's put the Q3 budget in the parking lot and discuss it next week.",
    "Tech Lead: Let's schedule a follow-up to finalize the RabbitMQ migration plan.",
]


def _parse_transcript_line(line: str) -> tuple[str, str]:
    if ":" not in line:
        return "Unknown", line.strip()
    speaker, text = line.split(":", 1)
    return speaker.strip() or "Unknown", text.strip()


async def main() -> None:
    print("Starting Memory Integration Test...")

    with SessionLocal() as db:
        meeting = Meeting(
            title="Memory Test Meeting",
            status="completed",
            vexa_meeting_id=f"mock-vexa-{uuid.uuid4()}",
            team_id=1,
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        print(f"Created mock meeting ID: {meeting.id}")

        agent = ControllerAgent()
        print("\n--- Testing Real-Time Notifications ---")
        start_time = datetime.now(timezone.utc)
        chunk_buffer: list[TranscriptChunk] = []
        for index, line in enumerate(MOCK_TRANSCRIPT):
            speaker, text = _parse_transcript_line(line)
            if not text:
                continue
            chunk = TranscriptChunk(
                meeting_id=meeting.id,
                speaker=speaker,
                text=text,
                timestamp=start_time + timedelta(seconds=index * 10),
            )
            chunk_buffer.append(chunk)

            print(f"\n[Incoming] {speaker}: {text}")
            try:
                result = await agent.summarize(text)
            except Exception as exc:
                print(f"Summarization failed: {exc}")
                continue

            scrum = result.get("scrum_master", {})
            summary_text = str(scrum.get("text") or "").strip()
            if summary_text and summary_text.upper() != "IGNORE":
                print(f"Insight: {summary_text}")

            proposal_data = scrum.get("proposal")
            if proposal_data:
                action_type = str(proposal_data.get("type") or "").strip()
                content = str(proposal_data.get("content") or "").strip()
                if action_type and content:
                    action = AgentAction(
                        meeting_id=meeting.id,
                        agent_role="scrum_master",
                        action_type=action_type,
                        content=content,
                        status="pending",
                    )
                    db.add(action)
                    db.commit()
                    db.refresh(action)
                    print(f"Notification: [{action_type.upper()}] {content}")
                else:
                    print("Notification skipped: proposal missing type/content.")
            else:
                print("No notification generated.")

        if chunk_buffer:
            db.add_all(chunk_buffer)
            db.commit()
            print(
                f"\n[Database] Batch inserted {len(chunk_buffer)} transcript chunks."
            )

        print("\n--- Testing Final Report & Mem0 Save ---")
        print("Generating final report and triggering Mem0 save...")
        await agent.generate_final_report(meeting.id, db)

    print("\n--- Testing Mem0 Retrieval ---")
    mem = get_memory()
    if mem is None:
        print("Error: Mem0 is not initialized. Check MEM0_ENABLED environment variable.")
        return

    query = "What was the decision about the message broker?"
    print(f"Querying Mem0: '{query}'")
    results = mem.search(query=query, filters={"user_id": "team_1"})
    print("\nResults from Mem0:")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
