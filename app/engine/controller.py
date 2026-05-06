from __future__ import annotations

import os

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
    "You are an Expert Agile Scrum Master and Technical Project Manager. Given the following meeting transcript, "
    "generate a comprehensive and structured JSON report. Output ONLY valid JSON without any markdown formatting or explanation.\n"
    "The JSON must have exactly this structure:\n"
    "{\n"
    '  "summary": "Provide a clear and thorough summary of the meeting, focusing on the main topics discussed, key goals, decisions made, and overall progress.",\n'
    '  "pending_to_schedule": [\n'
    '    {"task": "Description of any item, follow-up meeting, or discussion that needs to be scheduled", "owner": "Name of the person responsible, or null if unassigned"}\n'
    "  ],\n"
    '  "parking_lot": [\n'
    '    "Description of any topic or idea raised during the meeting but deferred or parked for future discussion"\n'
    "  ],\n"
    '  "to_do": [\n'
    '    {"task": "Detailed description of an action item or task to be completed", "owner": "Name of the person responsible, or null if unassigned"}\n'
    "  ]\n"
    "}\n\n"
    "Rules:\n"
    "1. Base your response strictly on the provided transcript. Do not invent details.\n"
    "2. Do not mention missing transcript text, model limitations, or speculative issues.\n"
    "3. Ensure the summary flows naturally and covers all major talking points.\n"
    "4. If there are no items for a specific category, use an empty array []."
)


def _env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        return float(raw_value)
    except ValueError:
        return default


class ControllerAgent:
    def __init__(
        self,
        ollama_url: str = "http://ollama:11434/api/generate",
        model: str = "llama3",
        timeout: float = 30.0,
    ) -> None:
        env_url = os.getenv("OLLAMA_URL", "").strip()
        env_model = os.getenv("OLLAMA_MODEL", "").strip()
        env_timeout = _env_float("OLLAMA_TIMEOUT_SECONDS", timeout)

        self.ollama_url = env_url or ollama_url
        self.model = env_model or model
        self.timeout = env_timeout if env_timeout else 120.0

    async def _generate(self, prompt: str, system_prompt: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)) as client:
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
