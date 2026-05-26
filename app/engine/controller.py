from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os

import httpx
from mem0 import Memory
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Meeting, TranscriptChunk
from app.engine.prompts import (
    REALTIME_PERSONA_PROMPTS,
    INITIAL_ANALYSIS_PROMPTS,
    SYNTHESIS_PROMPT,
    DISCUSSION_PERSONA_PROMPTS,
    INSTANT_CLARITY_BUSINESS,
    INSTANT_CLARITY_TECHNICAL,
    PROMPT_DEFAULTS,
    get_team_prompts,
)


class _SafeFormat(dict):
    """dict subclass that leaves unknown {placeholders} as-is instead of raising KeyError."""
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"

DEFAULT_DISCUSSION_ROUNDS = 1

DISCUSSION_ROLES = ("tech_lead", "product_manager")


# ---------------------------------------------------------------------------
# Low-level LLM client
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _init_memory() -> Memory | None:
    if not _env_bool("MEM0_ENABLED", True):
        return None

    ollama_url = os.getenv("MEM0_OLLAMA_URL", "http://ollama:11434").strip()
    llm_model = os.getenv("MEM0_LLM_MODEL", "llama3.1").strip() or "llama3.1"
    embed_model = (
        os.getenv("MEM0_EMBED_MODEL", "nomic-embed-text").strip()
        or "nomic-embed-text"
    )

    config = {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "meetingmind",
                "embedding_model_dims": 768,
            }
        },
        "llm": {
            "provider": "ollama",
            "config": {
                "model": llm_model,
                "ollama_base_url": ollama_url,
                "temperature": 0.1,
            },
        },
        "embedder": {
            "provider": "ollama",
            "config": {
                "model": embed_model,
                "ollama_base_url": ollama_url,
            },
        },
    }

    try:
        return Memory.from_config(config)
    except Exception as exc:
        print(f"[Memory Error] Failed to initialize Mem0: {exc}")
        return None


memory = _init_memory()
MEM0_SAVE_ENABLED = _env_bool("MEM0_SAVE_ENABLED", True)
MEM0_SEARCH_ENABLED = _env_bool("MEM0_SEARCH_ENABLED", True)


class OllamaClient:
    """Thin async wrapper around the Ollama /api/generate endpoint."""

    def __init__(
        self,
        url: str = "http://ollama:11434/api/generate",
        model: str = "llama3",
        timeout: float = 30.0,
    ) -> None:
        self.url = os.getenv("OLLAMA_URL", "").strip() or url
        self.model = os.getenv("OLLAMA_MODEL", "").strip() or model
        raw_timeout = _env_float("OLLAMA_TIMEOUT_SECONDS", timeout)
        self.timeout = raw_timeout if raw_timeout else 120.0

    async def generate(self, prompt: str, system_prompt: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)) as client:
            response = await client.post(self.url, json=payload)
            response.raise_for_status()
            data = response.json()

        text = str(data.get("response", "")).strip()
        if not text:
            raise RuntimeError("Ollama returned an empty response")
        return text


# ---------------------------------------------------------------------------
# Discussion engine  (Tech Lead ↔ Product Manager debate)
# ---------------------------------------------------------------------------


class DiscussionEngine:
    """Orchestrates multi-round discussions between personas."""

    def __init__(self, llm: OllamaClient) -> None:
        self._llm = llm

    async def run(
        self,
        meeting_id: int,
        initial_reports: dict[str, str],
        transcript: str,
        num_rounds: int,
        team_prompts: dict[str, str] | None = None,
    ) -> list[dict[str, str]]:
        """Execute *num_rounds* of Tech Lead ↔ PM discussion."""
        log: list[dict[str, str]] = []
        resolved_personas = {
            "tech_lead": (team_prompts or {}).get("discussion_tech_lead") or DISCUSSION_PERSONA_PROMPTS["tech_lead"],
            "product_manager": (team_prompts or {}).get("discussion_product_manager") or DISCUSSION_PERSONA_PROMPTS["product_manager"],
        }

        for round_num in range(1, num_rounds + 1):
            print(f"[Discussion] meeting={meeting_id} round {round_num}/{num_rounds}")

            context = self._build_context(
                meeting_id,
                transcript,
                initial_reports,
                log,
                round_num,
            )

            tasks = [
                self._discuss(role, prompt, context, meeting_id, round_num)
                for role, prompt in resolved_personas.items()
            ]
            results = await asyncio.gather(*tasks)

            entry: dict[str, str] = {"round": str(round_num)}
            for role, response in results:
                entry[role] = response
                preview = " ".join(response.split())[:200]
                label = role.replace("_", " ").title()
                print(
                    f"[Discussion] meeting={meeting_id} round={round_num} {label}: {preview}..."
                )

            log.append(entry)

        return log

    # -- private helpers ---------------------------------------------------

    async def _discuss(
        self,
        role: str,
        sys_prompt: str,
        context: str,
        meeting_id: int,
        round_num: int,
    ) -> tuple[str, str]:
        try:
            result = await self._llm.generate(prompt=context, system_prompt=sys_prompt)
            return role, result
        except Exception as exc:
            print(f"[Discussion] {role} failed in round {round_num}: {exc}")
            return role, f"[Error: {exc}]"

    @staticmethod
    def _build_context(
        meeting_id: int,
        transcript: str,
        initial_reports: dict[str, str],
        history: list[dict[str, str]],
        current_round: int,
    ) -> str:
        parts = [
            f"Meeting ID: {meeting_id}\n",
            "=== MEETING TRANSCRIPT ===",
            transcript,
            "",
            "=== INITIAL ANALYSES ===",
        ]
        for role, report in initial_reports.items():
            parts += [f"--- {role.replace('_', ' ').title()} (Initial) ---", report, ""]

        if history:
            parts.append("=== DISCUSSION HISTORY ===")
            for entry in history:
                parts.append(f"--- Round {entry.get('round', '?')} ---")
                for role in DISCUSSION_ROLES:
                    if role in entry:
                        parts += [
                            f"[{role.replace('_', ' ').title()}]:",
                            entry[role],
                            "",
                        ]

        parts += [
            f"\n=== YOUR TURN: Discussion Round {current_round} ===",
            "Review all the above and respond according to your role's discussion format.",
        ]
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Report builder  (prompt assembly — no LLM calls, no DB)
# ---------------------------------------------------------------------------


class ReportPromptBuilder:
    """Assembles the final Scrum Master synthesis prompt."""

    @staticmethod
    def build(
        meeting_id: int,
        initial_reports: dict[str, str],
        transcript: str,
        discussion_log: list[dict[str, str]] | None = None,
    ) -> str:
        parts = [
            f"Meeting ID: {meeting_id}\n",
            "--- Tech Lead Findings ---",
            initial_reports.get("tech_lead", "{}"),
            "",
            "--- Product Manager Findings ---",
            initial_reports.get("product_manager", "{}"),
            "",
        ]

        if discussion_log:
            parts.append("--- Cross-Functional Discussion ---")
            for entry in discussion_log:
                parts.append(f"  Round {entry.get('round', '?')}:")
                for role in DISCUSSION_ROLES:
                    if role in entry:
                        label = role.replace("_", " ").title()
                        parts.append(f"    [{label}]: {entry[role]}")
                parts.append("")

        parts += ["--- Full Transcript ---", transcript]
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Transcript loader  (DB read — no LLM, no prompts)
# ---------------------------------------------------------------------------


class TranscriptLoader:
    """Reads transcript chunks from the database."""

    @staticmethod
    def load(meeting_id: int, db: Session) -> str:
        ordering = getattr(TranscriptChunk, "start_time", TranscriptChunk.timestamp)
        chunks = (
            db.execute(
                select(TranscriptChunk)
                .where(TranscriptChunk.meeting_id == meeting_id)
                .order_by(ordering.asc())
            )
            .scalars()
            .all()
        )
        lines: list[str] = []
        for chunk in chunks:
            text = str(chunk.text or "").strip()
            if not text:
                continue
            speaker = str(chunk.speaker or "Unknown").strip() or "Unknown"
            lines.append(f"{speaker}: {text}")
        return "\n".join(lines).strip()

    @staticmethod
    def load_recent(
        meeting_id: int, db: Session, last_x_minutes: int | None = None
    ) -> str:
        ordering = getattr(TranscriptChunk, "start_time", TranscriptChunk.timestamp)
        stmt = select(TranscriptChunk).where(TranscriptChunk.meeting_id == meeting_id)

        if last_x_minutes is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=last_x_minutes)
            stmt = stmt.where(TranscriptChunk.timestamp >= cutoff)

        stmt = stmt.order_by(ordering.asc())
        chunks = db.execute(stmt).scalars().all()

        lines: list[str] = []
        for chunk in chunks:
            text = str(chunk.text or "").strip()
            if not text:
                continue
            speaker = str(chunk.speaker or "Unknown").strip() or "Unknown"
            lines.append(f"{speaker}: {text}")
        return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Controller  (orchestrator — ties everything together)
# ---------------------------------------------------------------------------


class ControllerAgent:
    """High-level orchestrator for real-time summaries and final reports."""

    def __init__(
        self,
        ollama_url: str = "http://ollama:11434/api/generate",
        model: str = "llama3",
        timeout: float = 30.0,
    ) -> None:
        self._llm = OllamaClient(url=ollama_url, model=model, timeout=timeout)
        self._discussion = DiscussionEngine(self._llm)
        self._pre_meeting_context = ""

    def load_pre_meeting_context(self, team_id: str = "team_agile") -> None:
        """Fetches the team's recent history from Mem0 to use during the live meeting."""
        global memory, MEM0_SEARCH_ENABLED
        if memory is not None and MEM0_SEARCH_ENABLED:
            try:
                print(
                    f"[ControllerAgent] Fetching pre-meeting context for {team_id}..."
                )
                raw_memories = memory.search(
                    query=(
                        "What are the current active projects, recent technical "
                        "decisions, and ongoing blockers for this team?"
                    ),
                    filters={"user_id": team_id},
                )
                if (
                    raw_memories
                    and isinstance(raw_memories, dict)
                    and raw_memories.get("results")
                ):
                    memory_texts = [
                        m.get("memory", "") for m in raw_memories["results"]
                    ]
                    formatted_memories = "\n".join(
                        f"- {text}" for text in memory_texts if text
                    )
                    self._pre_meeting_context = (
                        "--- PRE-MEETING CONTEXT (Past Knowledge) ---\n"
                        f"{formatted_memories}"
                    )
                    print(
                        "[ControllerAgent] Successfully loaded pre-meeting context."
                    )
            except Exception as exc:
                print(f"[Memory Error] Failed to load pre-meeting context: {exc}")

    # -- Real-time summarisation + proposal detection --------------------

    async def summarize(self, text: str, team_prompts: dict[str, str] | None = None) -> dict[str, dict]:
        cleaned = " ".join(text.split()).strip()
        if not cleaned:
            return {
                role: {"text": "IGNORE", "proposal": None}
                for role in REALTIME_PERSONA_PROMPTS
            }

        user_template = (team_prompts or {}).get("realtime_user") or PROMPT_DEFAULTS["realtime_user"]
        prompt = user_template.format_map(
            _SafeFormat(transcript=cleaned, pre_meeting_context=self._pre_meeting_context)
        )

        persona_prompts = {
            "scrum_master": (team_prompts or {}).get("realtime_scrum_master") or REALTIME_PERSONA_PROMPTS["scrum_master"],
        }

        async def _run(role: str, sys_prompt: str) -> tuple[str, dict]:
            try:
                raw = await self._llm.generate(prompt=prompt, system_prompt=sys_prompt)
                raw = raw.strip().removeprefix("```json").removesuffix("```").strip()
                result = json.loads(raw)
                summary = result.get("summary", "IGNORE")
                proposal = result.get("proposal")
                return role, {"text": summary, "proposal": proposal}
            except Exception as exc:
                print(f"[ControllerAgent] Persona {role} failed: {exc}")
                return role, {"text": "IGNORE", "proposal": None}

        results = await asyncio.gather(
            *[_run(r, p) for r, p in persona_prompts.items()]
        )
        return dict(results)

    # -- Instant Clarity ---------------------------------------------------

    async def generate_instant_clarity(
        self,
        meeting_id: int,
        db_session: Session,
        mode: str,
        last_x_minutes: int | None = None,
        team_id: int | None = None,
    ) -> str:
        transcript_context = TranscriptLoader.load_recent(
            meeting_id, db_session, last_x_minutes
        )
        if not transcript_context:
            return "No transcript data available for this meeting to explain."

        word_count = len(transcript_context.split())
        if word_count < 10:
            return "The transcript is too short to generate a meaningful clarification."

        prompts = get_team_prompts(team_id, db_session)
        system_prompt = (
            prompts["instant_clarity_business"]
            if mode.lower() == "business"
            else prompts["instant_clarity_technical"]
        )

        prompt = prompts["instant_clarity_user"].format_map(
            _SafeFormat(transcript_context=transcript_context)
        )
        try:
            return await self._llm.generate(prompt=prompt, system_prompt=system_prompt)
        except Exception as exc:
            print(f"[ControllerAgent] Instant Clarity failed: {exc}")
            return "Failed to generate instant clarity due to an internal error."

    # -- Final report ------------------------------------------------------

    async def generate_final_report(
        self,
        meeting_id: int,
        db_session: Session,
        num_rounds: int | None = None,
        team_id: int | None = None,
    ) -> str:
        if num_rounds is None:
            num_rounds = DEFAULT_DISCUSSION_ROUNDS
        num_rounds = max(num_rounds, 0)

        prompts = get_team_prompts(team_id, db_session)

        # 1. Load transcript
        transcript = TranscriptLoader.load(meeting_id, db_session)

        # Retrieve past context from the memory layer based on the current transcript
        query_text = transcript[:1000] if transcript else "General agile meeting"
        past_memories = ""
        if memory is not None and MEM0_SEARCH_ENABLED:
            try:
                past_memories = memory.search(
                    query=query_text, filters={"user_id": "team_agile"}
                )
            except Exception as exc:
                print(f"[Memory Error] Failed to search memories: {exc}")

        if not transcript:
            msg = "## Summary\nNo transcript content available."
            print(f"[Final Report] meeting={meeting_id}\n{msg}\n")
            return msg

        # 2. Initial analysis: Tech Lead + Product Manager (parallel)
        base_prompt = (
            f"Meeting ID: {meeting_id}\n\n"
            f"--- RELEVANT PAST MEMORIES & CONTEXT ---\n{past_memories}\n\n"
            f"--- CURRENT TRANSCRIPT ---\n{transcript}"
        )
        initial_reports = await self._run_initial_analyses(base_prompt, prompts)

        # 3. Discussion rounds (Tech Lead ↔ PM)
        discussion_log: list[dict[str, str]] = []
        if num_rounds > 0:
            print(
                f"[Final Report] meeting={meeting_id} starting {num_rounds}-round discussion"
            )
            discussion_log = await self._discussion.run(
                meeting_id=meeting_id,
                initial_reports=initial_reports,
                transcript=transcript,
                num_rounds=num_rounds,
                team_prompts=prompts,
            )

        # 4. Scrum Master synthesis
        synthesis_prompt = ReportPromptBuilder.build(
            meeting_id,
            initial_reports,
            transcript,
            discussion_log,
        )
        scrum_master_result = await self._llm.generate(
            prompt=synthesis_prompt,
            system_prompt=prompts["synthesis"],
        )

        # 5. Assemble and persist
        report = {**initial_reports, "scrum_master": scrum_master_result}
        self._persist(db_session, meeting_id, report, discussion_log)

        print(
            f"[Final Report] meeting={meeting_id}\n"
            f"Tech Lead: {report['tech_lead']}\n"
            f"Product Manager: {report['product_manager']}\n"
            f"Scrum Master: {report['scrum_master']}\n"
        )

        # Save today's findings into long-term memory
        if memory is not None and MEM0_SAVE_ENABLED:
            try:
                memory.add(
                    f"Tech Lead findings: {report.get('tech_lead', '')}",
                    user_id="team_agile",
                )
                memory.add(
                    f"Product Manager findings: {report.get('product_manager', '')}",
                    user_id="team_agile",
                )
                memory.add(
                    f"Scrum Master synthesis: {report.get('scrum_master', '')}",
                    user_id="team_agile",
                )
            except Exception as e:
                print(f"[Memory Error] Failed to save memories to Mem0: {e}")
        return json.dumps(report)

    # -- private helpers ---------------------------------------------------

    async def _run_initial_analyses(self, prompt: str, team_prompts: dict[str, str] | None = None) -> dict[str, str]:
        resolved = {
            "tech_lead": (team_prompts or {}).get("final_tech_lead") or INITIAL_ANALYSIS_PROMPTS["tech_lead"],
            "product_manager": (team_prompts or {}).get("final_product_manager") or INITIAL_ANALYSIS_PROMPTS["product_manager"],
        }

        async def _fetch(role: str, sys_prompt: str) -> tuple[str, str]:
            try:
                return role, await self._llm.generate(
                    prompt=prompt, system_prompt=sys_prompt
                )
            except Exception as exc:
                print(f"[Final Report] Persona {role} failed: {exc}")
                return role, "{}"

        results = await asyncio.gather(
            *[_fetch(r, p) for r, p in resolved.items()]
        )
        return dict(results)

    @staticmethod
    def _persist(
        db: Session,
        meeting_id: int,
        report: dict[str, str],
        discussion_log: list[dict[str, str]],
    ) -> None:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            return

        meeting.summary = report
        if discussion_log and hasattr(meeting, "discussion_log"):
            meeting.discussion_log = discussion_log

        try:
            db.commit()
        except Exception:
            db.rollback()
