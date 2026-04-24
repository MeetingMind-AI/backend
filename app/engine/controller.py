from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Meeting, TranscriptChunk

REALTIME_SYSTEM_PROMPT = (
    "You are an Agile assistant extracting live insights. If the text does not contain "
    "meaningful action items, blockers, or agile updates, output exactly the word 'IGNORE'. "
    "Do not apologize or explain."
)

FINAL_REPORT_SYSTEM_PROMPT = (
    "You are an Expert Agile Scrum Master. Given the following meeting transcript, generate a "
    "structured markdown report with exactly these sections and format:\n"
    "## Summary\n"
    "Write exactly 2 concise sentences focused on goals, decisions, and progress.\n\n"
    "## Action Items\n"
    "Use bullets. Each bullet must start with '- [ ]'. Include owner if named as '(Owner: <name>)'. "
    "If no clear action items exist, output exactly '- [ ] None identified.'.\n\n"
    "## Blockers\n"
    "Use bullets. Include only explicit blockers/risks mentioned in transcript. "
    "If none, output exactly '- None identified.'.\n\n"
    "Rules: Do not mention missing transcript text, model limitations, or speculative issues. "
    "Do not add sections beyond the three required headings."
)


class ControllerAgent:
    def __init__(
        self,
        ollama_url: str = "http://host.docker.internal:11434/api/generate",
        model: str = "llama3",
        timeout: float = 30.0,
    ) -> None:
        self.ollama_url = ollama_url
        self.model = model
        self.timeout = timeout

    async def _generate(self, prompt: str, system_prompt: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.ollama_url, json=payload)
            response.raise_for_status()
            data = response.json()

        raw_response = str(data.get("response", "")).strip()
        if not raw_response:
            raise RuntimeError("Ollama returned an empty response")
        return raw_response

    async def summarize(self, text: str) -> str:
        cleaned_text = " ".join(text.split()).strip()
        if not cleaned_text:
            return "IGNORE"

        prompt = (
            "Transcript:\n"
            f"{cleaned_text}\n\n"
            "If this contains meaningful agile information, return one concise sentence that mentions "
            "action item, decision, blocker, or status update if present. "
            "Otherwise return IGNORE."
        )

        summary = await self._generate(prompt=prompt, system_prompt=REALTIME_SYSTEM_PROMPT)
        normalized_summary = " ".join(summary.split())
        if normalized_summary.upper() == "IGNORE":
            return "IGNORE"
        return normalized_summary

    async def generate_final_report(self, meeting_id: int, db_session: Session) -> str:
        ordering_column = getattr(TranscriptChunk, "start_time", TranscriptChunk.timestamp)
        chunks = (
            db_session.execute(
                select(TranscriptChunk)
                .where(TranscriptChunk.meeting_id == meeting_id)
                .order_by(ordering_column.asc())
            )
            .scalars()
            .all()
        )

        transcript_lines: list[str] = []
        for chunk in chunks:
            text = str(chunk.text or "").strip()
            if not text:
                continue
            speaker = str(chunk.speaker or "Unknown").strip() or "Unknown"
            transcript_lines.append(f"{speaker}: {text}")

        full_transcript = "\n".join(transcript_lines).strip()
        if not full_transcript:
            empty_report = "## Summary\nNo transcript content available."
            print(f"[Vexa Final Report] meeting={meeting_id}\n{empty_report}\n")
            return empty_report

        prompt = f"Meeting ID: {meeting_id}\n\nTranscript:\n{full_transcript}"
        report = await self._generate(prompt=prompt, system_prompt=FINAL_REPORT_SYSTEM_PROMPT)

        print(f"[Vexa Final Report] meeting={meeting_id}\n{report}\n")

        meeting = db_session.get(Meeting, meeting_id)
        if meeting is not None:
            report_field = None
            for candidate in ("final_summary", "summary", "final_report"):
                if hasattr(meeting, candidate):
                    report_field = candidate
                    break

            if report_field is not None:
                setattr(meeting, report_field, report)
                try:
                    db_session.commit()
                except Exception:  # noqa: BLE001
                    db_session.rollback()

        return report
