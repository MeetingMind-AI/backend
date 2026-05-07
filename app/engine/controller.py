from __future__ import annotations

import os

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Meeting, TranscriptChunk

import asyncio

from app.engine.prompts import REALTIME_PERSONA_PROMPTS, FINAL_PERSONA_PROMPTS


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

    async def summarize(self, text: str) -> dict[str, str]:
        cleaned_text = " ".join(text.split()).strip()
        if not cleaned_text:
            return {role: "IGNORE" for role in REALTIME_PERSONA_PROMPTS.keys()}

        prompt = (
            "Transcript:\n"
            f"{cleaned_text}\n\n"
            "If this contains meaningful information for your role, return one concise sentence. "
            "Otherwise return IGNORE."
        )

        async def _fetch_persona(role: str, sys_prompt: str) -> tuple[str, str]:
            try:
                summary = await self._generate(prompt=prompt, system_prompt=sys_prompt)
                normalized = " ".join(summary.split())
                if normalized.upper() == "IGNORE":
                    return role, "IGNORE"
                return role, normalized
            except Exception as e:
                print(f"[ControllerAgent] Persona {role} failed: {e}")
                return role, "IGNORE"

        # Execute all three personas in parallel
        tasks = [
            _fetch_persona(role, sys_prompt) 
            for role, sys_prompt in REALTIME_PERSONA_PROMPTS.items()
        ]
        results = await asyncio.gather(*tasks)
        
        return dict(results)

    def _build_scrum_master_prompt(self, meeting_id: int, report_dict: dict[str, str], full_transcript: str) -> str:
        return (
            f"Meeting ID: {meeting_id}\n\n"
            f"--- Tech Lead Findings ---\n{report_dict.get('tech_lead', '{}')}\n\n"
            f"--- Product Manager Findings ---\n{report_dict.get('product_manager', '{}')}\n\n"
            f"--- Full Transcript ---\n{full_transcript}"
        )

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
        
        async def _fetch_persona_report(role: str, sys_prompt: str, prompt: str) -> tuple[str, str]:
            try:
                result = await self._generate(prompt=prompt, system_prompt=sys_prompt)
                return role, result
            except Exception as e:
                print(f"[Vexa Final Report] Persona {role} failed: {e}")
                return role, "{}"

        # Step 1: Execute Tech Lead and Product Manager in parallel
        preliminary_personas = {
            "tech_lead": FINAL_PERSONA_PROMPTS["tech_lead"],
            "product_manager": FINAL_PERSONA_PROMPTS["product_manager"]
        }
                
        tasks = [
            _fetch_persona_report(role, sys_prompt, prompt) 
            for role, sys_prompt in preliminary_personas.items()
        ]
        results = await asyncio.gather(*tasks)
        report_dict = dict(results)

        # Step 2: Inject their findings into the Scrum Master's prompt
        scrum_master_prompt = self._build_scrum_master_prompt(meeting_id, report_dict, full_transcript)

        # Step 3: Run the Scrum Master Synthesizer
        scrum_master_result = await self._generate(
            prompt=scrum_master_prompt, 
            system_prompt=FINAL_PERSONA_PROMPTS["scrum_master"]
        )
        report_dict["scrum_master"] = scrum_master_result

        print(f"[Vexa Final Report] meeting={meeting_id}\nTech Lead: {report_dict['tech_lead']}\nScrum Master: {report_dict['scrum_master']}\nProduct Manager: {report_dict['product_manager']}\n")

        meeting = db_session.get(Meeting, meeting_id)
        if meeting is not None:
            # Save as JSON structure since column is JSONB
            meeting.summary = report_dict

            try:
                db_session.commit()
            except Exception:
                db_session.rollback()

        import json
        return json.dumps(report_dict)
