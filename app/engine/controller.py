"""
Central Multi-Agent Controller and Orchestrator Module.

Implements the BOLAA (Bounded-Output LLM Agent Architecture) multi-agent pipeline used
to produce post-meeting reports from live transcript data.  The pipeline has five stages:

  1. Pre-meeting memory retrieval  — Mem0 vector search loads relevant past team
     context (prior decisions, blockers, agreed scope) into `_pre_meeting_context`
     before the first transcript chunk arrives.

  2. Parallel persona analysis  — `ControllerAgent._run_initial_analyses` fans out
     two concurrent Ollama calls: the Tech Lead and Product Manager each read the
     full transcript and produce independent structured JSON analyses.

  3. Multi-round cross-functional debate  — `DiscussionEngine.run` executes
     `num_rounds` debate iterations where both personas challenge each other's
     assumptions.  Higher `num_rounds` values produce more thorough alignment at
     the cost of additional inference latency (each round ~10-30 s on a 14 b model).

  4. Scrum Master synthesis  — `ReportPromptBuilder` assembles all inputs
     (initial analyses + debate log) into a single prompt, and the Scrum Master
     persona produces the final executive JSON digest.

  5. Persistence & distribution  — `ControllerAgent._persist` commits the report
     to PostgreSQL, backfills any missing AgentAction rows, stores findings in Mem0
     for future meetings, and optionally triggers async email delivery.

`ControllerAgent` also drives the **Instant Clarity** feature: on-demand technical
or business explanations of recent transcript segments, served with a 60-second
Redis TTL cache keyed by SHA-256(system_prompt + user_prompt) to avoid redundant
Ollama calls when multiple attendees click the button simultaneously.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import time

import httpx
from mem0 import Memory
import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

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


logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "").strip()
_redis_client: redis.Redis | None = None


class DatabaseError(Exception):
    """Custom exception raised for database transaction failures or constraint violations during report persistence."""


def _get_redis_client() -> redis.Redis | None:
    """Lazy initializer for asynchronous Redis client.

    Returns:
        redis.Redis | None: Configured Redis connection client if REDIS_URL is set, else None.
    """
    global _redis_client
    if not REDIS_URL:
        return None
    if _redis_client is None:
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


class _SafeFormat(dict):
    """Subclass of dict that leaves unformatted string placeholders intact instead of raising KeyError."""

    def __missing__(self, key: str) -> str:
        """Return the unformatted placeholder string `{key}` when key is absent.

        Args:
            key (str): Missing placeholder key name.

        Returns:
            str: Original `{key}` placeholder string representation.
        """
        return "{" + key + "}"


DEFAULT_DISCUSSION_ROUNDS = 1

DISCUSSION_ROLES = ("tech_lead", "product_manager")


# ---------------------------------------------------------------------------
# Low-level LLM client
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    """Parse boolean value from environment variable.

    Args:
        name (str): Environment variable name.
        default (bool): Fallback boolean value.

    Returns:
        bool: True if environment variable is '1', 'true', 'yes', or 'on'; False otherwise.
    """
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    """Parse floating point value from environment variable.

    Args:
        name (str): Environment variable name.
        default (float): Fallback float value.

    Returns:
        float: Parsed float or default if variable is empty or invalid.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _init_memory() -> Memory | None:
    """Instantiate Mem0 vector memory instance with Qdrant vector database and Ollama models.

    Returns:
        Memory | None: Initialized Mem0 instance or None if disabled or configuration failed.
    """
    if not _env_bool("MEM0_ENABLED", True):
        return None

    ollama_url = os.getenv("MEM0_OLLAMA_URL", "http://ollama:11434").strip()
    llm_model = os.getenv("MEM0_LLM_MODEL", "llama3.1").strip() or "llama3.1"
    embed_model = (
        os.getenv("MEM0_EMBED_MODEL", "nomic-embed-text").strip()
        or "nomic-embed-text"
    )
    qdrant_url = os.getenv("MEM0_QDRANT_URL", "").strip()

    qdrant_config = {
        "collection_name": "meetingmind",
        "embedding_model_dims": 768,
    }
    if qdrant_url:
        from urllib.parse import urlparse

        parsed = urlparse(qdrant_url)
        if parsed.hostname and parsed.port:
            qdrant_config["host"] = parsed.hostname
            qdrant_config["port"] = parsed.port
        else:
            qdrant_config["url"] = qdrant_url
            qdrant_config["api_key"] = os.getenv("MEM0_QDRANT_API_KEY", "")

    config = {
        "vector_store": {
            "provider": "qdrant",
            "config": qdrant_config,
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


_memory_instance: Memory | None = None
_memory_initialized = False


def get_memory() -> Memory | None:
    """Retrieve global Mem0 Memory instance using lazy initialization to prevent startup blocking.

    Returns:
        Memory | None: Mem0 memory instance or None if disabled.
    """
    global _memory_instance, _memory_initialized
    if not _memory_initialized:
        _memory_instance = _init_memory()
        _memory_initialized = True
    return _memory_instance



MEM0_SAVE_ENABLED = _env_bool("MEM0_SAVE_ENABLED", True)
MEM0_SEARCH_ENABLED = _env_bool("MEM0_SEARCH_ENABLED", True)

# Module-level concurrency semaphore shared across all ControllerAgent instances.
# Rationale: Local Ollama instances running on consumer hardware (e.g. Apple Silicon Unified
# Memory or single NVIDIA GPUs with 8GB-16GB VRAM) cannot efficiently handle concurrent model
# inference calls. Multiple overlapping generate requests lead to severe VRAM thrashing,
# repeated model context swaps, CPU thread contention, and HTTP client timeouts.
# A global semaphore with limit=1 serializes all Ollama invocations on the event loop, ensuring
# deterministic single-flight execution and consistent inference latency.
_llm_semaphore: asyncio.Semaphore | None = None

# Timeout for real-time summarize() LLM calls. If Ollama is loading models or
# overloaded (e.g. CPU-only on Apple Silicon), this prevents the summary worker
# from blocking indefinitely and losing queued transcript chunks.
_LLM_SUMMARIZE_TIMEOUT: float = _env_float(
    "OLLAMA_SUMMARIZE_TIMEOUT_SECONDS", 30.0
) or 30.0


def _get_llm_semaphore() -> asyncio.Semaphore:
    """Lazily initialise the shared LLM semaphore on the running event loop.

    asyncio.Semaphore must be created on the same event loop it is used on;
    using a module-level singleton created at import time fails in some ASGI
    environments. Lazy init is therefore the safest approach.

    Returns:
        asyncio.Semaphore: Shared concurrency gate (limit=1) for all Ollama calls.
    """
    global _llm_semaphore
    if _llm_semaphore is None:
        _llm_semaphore = asyncio.Semaphore(1)
    return _llm_semaphore


class OllamaClient:
    """Async client wrapper for interacting with Ollama generate API endpoints."""

    def __init__(
        self,
        url: str = "http://ollama:11434/api/generate",
        model: str = "hermes3:8b",
        final_model: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Initialize Ollama Client configuration and HTTP client.

        Args:
            url (str): Target Ollama API generation URL.
            model (str): Default LLM model identifier (e.g. 'hermes3:8b').
            final_model (str | None): Model for final synthesis (defaults to base model if not set).
            timeout (float): Request timeout in seconds.
        """
        self.url = os.getenv("OLLAMA_URL", "").strip() or url
        self.model = os.getenv("OLLAMA_MODEL", "").strip() or model
        self.final_model = (
            final_model
            or os.getenv("OLLAMA_FINAL_MODEL", "").strip()
            or self.model
        )
        raw_timeout = _env_float("OLLAMA_TIMEOUT_SECONDS", timeout)
        self.timeout = raw_timeout if raw_timeout else 120.0
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout))

    async def generate(
        self,
        prompt: str,
        system_prompt: str,
        json_mode: bool = False,
        model: str | None = None,
    ) -> str:
        """Send prompt and system prompt to Ollama LLM and return text response.

        Args:
            prompt (str): User-turn prompt containing input data context.
            system_prompt (str): System prompt defining persona and formatting constraints.
            json_mode (bool): Flag indicating whether to enforce JSON mode format.
            model (str | None): Optional override LLM model identifier.

        Returns:
            str: Generated text response from Ollama model.

        Raises:
            RuntimeError: If Ollama API returns empty or invalid payload.
            httpx.HTTPStatusError: If HTTP request fails.
        """
        payload = {
            "model": model or self.model,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
            "options": {
                "temperature": 0.0,
                "top_p": 0.1,
                "seed": 42,
            },
        }
        if json_mode:
            payload["format"] = "json"

        response = await self._client.post(self.url, json=payload)
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
    """Orchestrates multi-round discussions between Tech Lead and Product Manager AI personas."""

    def __init__(self, llm: OllamaClient) -> None:
        """Initialize DiscussionEngine with low-level LLM client instance.

        Args:
            llm (OllamaClient): Active Ollama client instance.
        """
        self._llm = llm

    async def run(
        self,
        meeting_id: int,
        initial_reports: dict[str, str],
        transcript: str,
        num_rounds: int,
        team_prompts: dict[str, str] | None = None,
        model: str | None = None,
        on_thought: Any | None = None,
    ) -> list[dict[str, str]]:
        """Execute specified number of cross-functional debate rounds between Tech Lead and PM personas.

        Multi-round BOLAA debate stages:
            1. Independent Initial Analysis: Tech Lead and PM personas evaluate the transcript
               separately to establish baseline technical constraints and business goals.
            2. Interactive Cross-Debate: In each debate round, both personas receive cumulative
               discussion history (_build_context) so they can challenge each other's assumptions,
               trade-offs, delivery risks, and scope feasibility.
            3. Grounding Rules: All persona prompts enforce strict grounding against the raw
               meeting transcript. Historical memories retrieved from Mem0 are explicitly labeled
               as background reference only, preventing past agreements from overriding decisions
               made in the current session.

        Args:
            meeting_id (int): Primary key ID of the meeting.
            initial_reports (dict[str, str]): Initial reports generated by Tech Lead and PM.
            transcript (str): Full meeting transcript.
            num_rounds (int): Number of discussion rounds to run.
            team_prompts (dict[str, str] | None): Team prompt overrides dictionary.
            model (str | None): Optional LLM model identifier override.
            on_thought (Any | None): Optional callback to emit live deliberation thoughts.

        Returns:
            list[dict[str, str]]: List of discussion round logs containing Tech Lead and PM responses.
        """
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
                self._discuss(role, prompt, context, meeting_id, round_num, model)
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

                if on_thought and response:
                    t_payload = {
                        "id": f"thought-debate-r{round_num}-{role}-{int(time.time()*1000)}",
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "agent": role,
                        "title": f"Round {round_num} {label} Deliberation",
                        "text": response.strip(),
                        "stage": "debate",
                    }
                    try:
                        if asyncio.iscoroutinefunction(on_thought):
                            await on_thought(t_payload)
                        else:
                            on_thought(t_payload)
                    except Exception as err:
                        logger.debug("Error in on_thought during debate: %s", err)

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
        model: str | None = None,
    ) -> tuple[str, str]:
        """Execute a single persona turn within a discussion round.

        Args:
            role (str): Persona role identifier ('tech_lead' or 'product_manager').
            sys_prompt (str): System prompt for persona role.
            context (str): Constructed discussion context payload.
            meeting_id (int): Meeting primary key ID.
            round_num (int): Current round number.
            model (str | None): LLM model identifier override.

        Returns:
            tuple[str, str]: Tuple of (role, generated_response).
        """
        try:
            result = await self._llm.generate(
                prompt=context,
                system_prompt=sys_prompt,
                model=model,
            )
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
        """Assemble full conversation context including transcript, initial reports, and previous round history.

        Args:
            meeting_id (int): Primary key ID of the meeting.
            transcript (str): Raw meeting transcript string.
            initial_reports (dict[str, str]): Persona initial analysis outputs.
            history (list[dict[str, str]]): Previous discussion round logs.
            current_round (int): Current round index number.

        Returns:
            str: Formatted discussion context string.
        """
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
    """Assembles the final Scrum Master synthesis prompt combining initial analyses, discussion logs, and transcript."""

    @staticmethod
    def build(
        meeting_id: int,
        initial_reports: dict[str, str],
        transcript: str,
        discussion_log: list[dict[str, str]] | None = None,
    ) -> str:
        """Construct user prompt for Scrum Master synthesis.

        Args:
            meeting_id (int): Primary key ID of the meeting.
            initial_reports (dict[str, str]): Tech Lead and Product Manager reports.
            transcript (str): Full meeting transcript.
            discussion_log (list[dict[str, str]] | None): Multi-round debate logs.

        Returns:
            str: Formatted user prompt string for Scrum Master synthesis.
        """
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
    """Reads and formats transcript chunks from database sessions."""

    @staticmethod
    def load(meeting_id: int, db: Session) -> str:
        """Load and format all transcript chunks for a meeting.

        Args:
            meeting_id (int): Primary key ID of target meeting.
            db (Session): Active database session.

        Returns:
            str: Line-delimited transcript string formatted as 'Speaker: Text'.
        """
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
        """Load transcript chunks filtered by time window.

        Args:
            meeting_id (int): Primary key ID of target meeting.
            db (Session): Active database session.
            last_x_minutes (int | None): Optional minute window cutoff.

        Returns:
            str: Line-delimited formatted transcript string.
        """
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
    """High-level orchestrator for real-time summaries, Instant Clarity, and multi-agent final report synthesis."""

    def __init__(
        self,
        ollama_url: str = "http://ollama:11434/api/generate",
        model: str = "hermes3:8b",
        final_model: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Initialize ControllerAgent, inner LLM client, semaphore concurrency limits, and discussion engine.

        Args:
            ollama_url (str): Target Ollama API generate endpoint URL.
            model (str): Default LLM model identifier for real-time tasks (default 'hermes3:8b').
            final_model (str | None): Model identifier for final synthesis and debate (defaults to base model if None).
            timeout (float): Connection and request timeout in seconds.
        """
        self._llm = OllamaClient(
            url=ollama_url,
            model=model,
            final_model=final_model,
            timeout=timeout,
        )
        self._discussion = DiscussionEngine(self._llm)
        self._pre_meeting_context = ""

    async def load_pre_meeting_context(self, team_id: int) -> None:
        """Fetch active team memory context from Mem0 for use during live transcript ingestion.

        Args:
            team_id (int): Primary key ID of the team workspace.
        """
        mem = get_memory()
        if mem is not None and MEM0_SEARCH_ENABLED:
            mem0_user = f"team_{team_id}"
            try:
                print(
                    f"[ControllerAgent] Fetching pre-meeting context for {mem0_user}..."
                )
                raw_memories = await asyncio.to_thread(
                    mem.search,
                    query=(
                        "What are the current active projects, recent technical "
                        "decisions, and ongoing blockers for this team?"
                    ),
                    filters={"user_id": mem0_user},
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
                        "--- PRE-MEETING CONTEXT (Past Knowledge - Reference Only) ---\n"
                        f"{formatted_memories}"
                    )
                    print(
                        "[ControllerAgent] Successfully loaded pre-meeting context."
                    )
            except Exception as exc:
                print(f"[Memory Error] Failed to load pre-meeting context: {exc}")

    # -- Real-time summarisation + proposal detection --------------------

    async def summarize(
        self,
        text: str,
        existing_actions: list[str] | None = None,
        team_prompts: dict[str, str] | None = None,
    ) -> dict[str, dict]:
        """Analyze a real-time transcript utterance and extract summaries and action proposals.

        Args:
            text (str): Incoming transcript chunk text utterance.
            existing_actions (list[str] | None): List of already tracked action item content strings for deduplication.
            team_prompts (dict[str, str] | None): Team prompt customization overrides.

        Returns:
            dict[str, dict]: Dictionary mapping persona roles (e.g., 'scrum_master') to summary text and proposal data.
        """
        cleaned = " ".join(text.split()).strip()
        if not cleaned:
            return {
                role: {"text": "IGNORE", "proposal": None}
                for role in REALTIME_PERSONA_PROMPTS
            }

        action_context = ""
        if existing_actions:
            action_context = (
                "\n\n<system_instructions>\n"
                "CRITICAL: The following actions are ALREADY TRACKED. Do NOT extract any action "
                "from the transcript that semantically matches these existing items:\n"
                + "\n".join(f"- {action}" for action in existing_actions)
                + "\n</system_instructions>"
            )

        user_template = (team_prompts or {}).get("realtime_user") or PROMPT_DEFAULTS["realtime_user"]
        prompt = user_template.format_map(
            _SafeFormat(
                transcript=cleaned,
                pre_meeting_context=self._pre_meeting_context + action_context,
            )
        )

        persona_prompts = {
            "scrum_master": (team_prompts or {}).get("realtime_scrum_master") or REALTIME_PERSONA_PROMPTS["scrum_master"],
        }

        async def _run(role: str, sys_prompt: str) -> tuple[str, dict]:
            """Execute LLM generation for a single persona role during real-time summarization.

            Args:
                role (str): Persona role key.
                sys_prompt (str): System prompt template string.

            Returns:
                tuple[str, dict]: Tuple of (role, summary_dict).
            """
            try:
                t0 = asyncio.get_event_loop().time()
                async with _get_llm_semaphore():
                    raw = await asyncio.wait_for(
                        self._llm.generate(
                            prompt=prompt,
                            system_prompt=sys_prompt,
                            json_mode=True,
                            model=self._llm.model,
                        ),
                        timeout=_LLM_SUMMARIZE_TIMEOUT,
                    )
                elapsed = asyncio.get_event_loop().time() - t0
                print(f"[Summary Timing] {role} completed in {elapsed:.2f}s")
                clean_raw = raw.strip()
                if clean_raw.startswith("```"):
                    clean_raw = clean_raw.split("\n", 1)[-1]
                if clean_raw.endswith("```"):
                    clean_raw = clean_raw.rsplit("\n", 1)[0]
                
                print(f"[LLM RAW - {role}] {clean_raw}")
                
                result = json.loads(clean_raw)
                summary = result.get("summary", "IGNORE")
                proposal = result.get("proposal")
                
                # Sanitize and normalize proposal
                if isinstance(proposal, dict):
                    raw_type = str(proposal.get("type", "")).strip().lower().replace("-", "_").replace(" ", "_")
                    type_aliases = {
                        "schedule": "to_schedule",
                        "to_schedule": "to_schedule",
                        "meeting": "to_schedule",
                        "todo": "to_do",
                        "to_do": "to_do",
                        "task": "to_do",
                        "parking": "parking_lot",
                        "parking_lot": "parking_lot",
                        "tabled": "parking_lot",
                        "blocker": "blocker",
                        "impediment": "blocker",
                    }
                    normalized_type = type_aliases.get(raw_type)
                    content = str(proposal.get("content", "")).strip()
                    if normalized_type and content:
                        proposal = {"type": normalized_type, "content": content}
                    else:
                        proposal = None
                else:
                    proposal = None

                return role, {"text": summary, "proposal": proposal}
            except asyncio.TimeoutError:
                elapsed = asyncio.get_event_loop().time() - t0
                print(
                    f"[Summary Timing] {role} TIMED OUT after {elapsed:.2f}s "
                    f"(limit={_LLM_SUMMARIZE_TIMEOUT}s) — skipping analysis"
                )
                return role, {"text": "IGNORE", "proposal": None}
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
        """Generate immediate technical or business clarification for recent meeting transcript context.

        Args:
            meeting_id (int): Primary key ID of target meeting.
            db_session (Session): Active database session.
            mode (str): Mode string ('technical' or 'business').
            last_x_minutes (int | None): Minute window cutoff for recent context.
            team_id (int | None): Team ID for custom prompt loading.

        Returns:
            str: Generated explanation text (or cached result if present in Redis).
        """
        transcript_context = await asyncio.to_thread(
            TranscriptLoader.load_recent, meeting_id, db_session, last_x_minutes
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
        cache_key = ""
        cache_client = _get_redis_client()

        # Deterministic SHA-256 caching with 60s TTL:
        # Avoids repeated redundant LLM inference when multiple attendees request clarity on the
        # same recent context window, or when an attendee repeatedly toggles personas.
        # A 60-second TTL balances instant responses with keeping pace with ongoing conversation.
        if cache_client is not None:
            cache_payload = f"{system_prompt}\n{prompt}"
            hashed_prompt = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()
            cache_key = f"instant_clarity:{hashed_prompt}"
            # Fail-open cache read: If Redis is unavailable or times out, catch the exception,
            # log a warning, and fall through cleanly to live LLM generation.
            try:
                cached = await cache_client.get(cache_key)
            except Exception as exc:
                cached = None
                print(f"[ControllerAgent] Redis cache read failed: {exc}")
            if cached:
                return cached

        # Generate explanation via LLM with error recovery
        try:
            explanation = await self._llm.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                model=self._llm.model,
            )
        except Exception as exc:
            # Catch LLM connection or timeout failures and return a user-facing explanation fallback
            print(f"[ControllerAgent] Instant Clarity failed: {exc}")
            return "Failed to generate instant clarity due to an internal error."

        # Fail-open cache write: Save successful explanation for 60 seconds
        if cache_client is not None and cache_key:
            try:
                await cache_client.setex(cache_key, 60, explanation)
            except Exception as exc:
                print(f"[ControllerAgent] Redis cache write failed: {exc}")

        return explanation

    # -- Final report ------------------------------------------------------

    async def generate_final_report(
        self,
        meeting_id: int,
        db_session: Session,
        num_rounds: int | None = None,
        team_id: int | None = None,
        on_thought: Any | None = None,
    ) -> str:
        """Orchestrate complete post-meeting pipeline: initial persona analyses, cross-functional debate, final synthesis, DB persistence, Mem0 storage, and email notification.

        Args:
            meeting_id (int): Primary key ID of target meeting.
            db_session (Session): Active database session.
            num_rounds (int | None): Number of debate rounds between Tech Lead and PM.
            team_id (int | None): Team ID for prompt config and email notification dispatch.
            on_thought (Any | None): Optional live thought streaming callback.

        Returns:
            str: JSON string containing complete final report JSON object.
        """
        # num_rounds controls the depth of the Tech Lead ↔ PM cross-functional debate.
        # DEFAULT_DISCUSSION_ROUNDS = 1 is sufficient for most meetings; 0 skips the
        # debate entirely (useful for very short check-ins).  Each additional round
        # adds one full-model inference cycle per persona (~10-30 s).
        if num_rounds is None:
            num_rounds = DEFAULT_DISCUSSION_ROUNDS
        num_rounds = max(num_rounds, 0)

        prompts = get_team_prompts(team_id, db_session)

        # 1. Load transcript
        transcript = await asyncio.to_thread(
            TranscriptLoader.load, meeting_id, db_session
        )

        # Check transcript threshold to prevent hallucinations on trivial / empty meetings
        MIN_REPORT_WORD_COUNT = 25
        word_count = len(transcript.split()) if transcript else 0

        if on_thought:
            t_payload = {
                "id": f"thought-ingest-{int(time.time()*1000)}",
                "time": datetime.now().strftime("%H:%M:%S"),
                "agent": "scrum_master",
                "title": "Context Ingestion & Vector Search",
                "text": f"Ingested {word_count} words from transcript timeline. Searching vector database for team memories & prior context.",
                "stage": "transcript",
            }
            try:
                if asyncio.iscoroutinefunction(on_thought):
                    await on_thought(t_payload)
                else:
                    on_thought(t_payload)
            except Exception as err:
                logger.debug("Error emitting ingest thought: %s", err)

        if not transcript or word_count < MIN_REPORT_WORD_COUNT:
            summary_msg = (
                "No transcript content available."
                if not transcript
                else f"Meeting was too brief ({word_count} words) to generate an in-depth synthesis: \"{transcript}\""
            )
            print(
                f"[Final Report] meeting={meeting_id} (word_count={word_count}) - insufficient content for multi-agent synthesis. Using fallback report."
            )
            fallback_report = {
                "tech_lead": json.dumps({"technical_decisions": [], "architecture": [], "engineering_blockers": []}),
                "product_manager": json.dumps({"feature_requests": [], "ux_topics": [], "roadmap_alignment": []}),
                "scrum_master": json.dumps({
                    "title": "Brief Check-in" if transcript else "Empty Meeting",
                    "summary": summary_msg,
                    "pending_to_schedule": [],
                    "parking_lot": [],
                    "to_do": [],
                }),
            }
            try:
                self._persist(db_session, meeting_id, fallback_report, [])
            except Exception as exc:
                print(f"[Final Report] Failed to persist fallback report: {exc}")
            return json.dumps(fallback_report)

        # Determine the Mem0 partition string
        mem0_user = f"team_{team_id}" if team_id else "global_team"

        # Retrieve past context from the memory layer based on the current transcript
        query_text = transcript[:1000] if transcript else "General agile meeting"
        past_memories = ""
        mem = get_memory()
        if mem is not None and MEM0_SEARCH_ENABLED:
            try:
                past_memories = await asyncio.to_thread(
                    mem.search, query=query_text, filters={"user_id": mem0_user}
                )
            except Exception as exc:
                print(f"[Memory Error] Failed to search memories: {exc}")

        # 2. Initial analysis: Tech Lead + Product Manager (parallel)
        base_prompt = (
            f"Meeting ID: {meeting_id}\n\n"
            f"--- RELEVANT PAST MEMORIES & BACKGROUND (Reference Only) ---\n{past_memories}\n\n"
            f"--- CURRENT TRANSCRIPT (Primary Source) ---\n{transcript}"
        )
        initial_reports = await self._run_initial_analyses(base_prompt, prompts, on_thought=on_thought)

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
                model=self._llm.final_model,
                on_thought=on_thought,
            )

        # 4. Scrum Master synthesis
        if on_thought:
            t_payload = {
                "id": f"thought-synthesis-start-{int(time.time()*1000)}",
                "time": datetime.now().strftime("%H:%M:%S"),
                "agent": "scrum_master",
                "title": "Master Synthesis in Progress",
                "text": "Scrum Master synthesizing Tech Lead constraints, Product Manager scope, and cross-functional agreements into final action items and executive digest.",
                "stage": "synthesis",
            }
            try:
                if asyncio.iscoroutinefunction(on_thought):
                    await on_thought(t_payload)
                else:
                    on_thought(t_payload)
            except Exception as err:
                logger.debug("Error emitting synthesis start thought: %s", err)

        synthesis_prompt = ReportPromptBuilder.build(
            meeting_id,
            initial_reports,
            transcript,
            discussion_log,
        )
        scrum_master_result = await self._llm.generate(
            prompt=synthesis_prompt,
            system_prompt=prompts["synthesis"],
            model=self._llm.final_model,
        )

        if on_thought:
            sm_summary = ""
            try:
                parsed_sm = json.loads(scrum_master_result.strip().replace('```json', '').replace('```', ''))
                if isinstance(parsed_sm, dict) and parsed_sm.get('summary'):
                    sm_summary = parsed_sm['summary']
            except Exception:
                sm_summary = scrum_master_result[:300]

            t_payload = {
                "id": f"thought-synthesis-done-{int(time.time()*1000)}",
                "time": datetime.now().strftime("%H:%M:%S"),
                "agent": "scrum_master",
                "title": "Consensus & Executive Synthesis Finalized",
                "text": sm_summary or "Executive digest and action items compiled successfully.",
                "stage": "synthesis",
            }
            try:
                if asyncio.iscoroutinefunction(on_thought):
                    await on_thought(t_payload)
                else:
                    on_thought(t_payload)
            except Exception as err:
                logger.debug("Error emitting synthesis done thought: %s", err)

        # 5. Assemble and persist
        report = {**initial_reports, "scrum_master": scrum_master_result}
        await asyncio.to_thread(
            self._persist, db_session, meeting_id, report, discussion_log
        )

        print(
            f"[Final Report] meeting={meeting_id}\n"
            f"Tech Lead: {report['tech_lead']}\n"
            f"Product Manager: {report['product_manager']}\n"
            f"Scrum Master: {report['scrum_master']}\n"
        )

        # Save today's findings into long-term memory
        if mem is not None and MEM0_SAVE_ENABLED:
            try:
                combined_report = (
                    f"Tech Lead findings: {report.get('tech_lead', '')}\n"
                    f"Product Manager findings: {report.get('product_manager', '')}\n"
                    f"Scrum Master synthesis: {report.get('scrum_master', '')}"
                )
                await asyncio.to_thread(
                    mem.add,
                    combined_report,
                    user_id=mem0_user,
                )
            except Exception as e:
                print(f"[Memory Error] Failed to save memories to Mem0: {e}")

        return json.dumps(report)

    # -- private helpers ---------------------------------------------------

    async def _run_initial_analyses(
        self,
        prompt: str,
        team_prompts: dict[str, str] | None = None,
        on_thought: Any | None = None,
    ) -> dict[str, str]:
        """Run parallel initial independent analyses for Tech Lead and Product Manager personas.

        Args:
            prompt (str): Context prompt string containing meeting ID, memories, and transcript.
            team_prompts (dict[str, str] | None): Team prompt overrides dictionary.
            on_thought (Any | None): Optional thought streaming callback.

        Returns:
            dict[str, str]: Map of persona role name to initial analysis JSON text.
        """
        resolved = {
            "tech_lead": (team_prompts or {}).get("final_tech_lead") or INITIAL_ANALYSIS_PROMPTS["tech_lead"],
            "product_manager": (team_prompts or {}).get("final_product_manager") or INITIAL_ANALYSIS_PROMPTS["product_manager"],
        }

        async def _fetch(role: str, sys_prompt: str) -> tuple[str, str]:
            """Fetch initial analysis report for a single persona role.

            Args:
                role (str): Persona role name.
                sys_prompt (str): System prompt string.

            Returns:
                tuple[str, str]: Tuple of (role, analysis_text).
            """
            try:
                result_text = await self._llm.generate(
                    prompt=prompt,
                    system_prompt=sys_prompt,
                    model=self._llm.final_model,
                )

                if on_thought and result_text:
                    label = "Tech Lead" if role == "tech_lead" else "Product Manager"
                    title = "Tech Lead Architectural Assessment" if role == "tech_lead" else "Product Manager Scope Evaluation"
                    summary_text = result_text.strip()
                    try:
                        clean_json = result_text.strip()
                        if clean_json.startswith('```'):
                            clean_json = clean_json.replace('```json', '').replace('```', '').strip()
                        parsed = json.loads(clean_json)
                        if isinstance(parsed, dict):
                            parts = []
                            for k, v in parsed.items():
                                if isinstance(v, list) and v:
                                    items = [str(x.get('decision', x.get('feature', x.get('blocker', str(x))))) if isinstance(x, dict) else str(x) for x in v[:3]]
                                    parts.append(f"**{k.replace('_', ' ').title()}**: " + ", ".join(items))
                            if parts:
                                summary_text = "\n".join(parts)
                    except Exception:
                        pass

                    t_payload = {
                        "id": f"thought-initial-{role}-{int(time.time()*1000)}",
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "agent": role,
                        "title": title,
                        "text": summary_text,
                        "stage": "personas",
                    }
                    try:
                        if asyncio.iscoroutinefunction(on_thought):
                            await on_thought(t_payload)
                        else:
                            on_thought(t_payload)
                    except Exception as err:
                        logger.debug("Error emitting initial thought: %s", err)

                return role, result_text
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
        """Persist final report summary dict, updated title, and discussion log into database.

        Args:
            db (Session): Active database session.
            meeting_id (int): Primary key ID of meeting.
            report (dict[str, str]): Assembled persona reports dictionary.
            discussion_log (list[dict[str, str]]): Debate history log list.

        Raises:
            DatabaseError: If database commit fails due to constraint error or loss of connection.
        """
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            return

        meeting.summary = report

        # Extract the AI-generated title and overwrite the raw meeting ID
        try:
            sm_raw = report.get("scrum_master", "{}").strip()
            if sm_raw.startswith("```"):
                import re
                sm_raw = re.sub(r"^```[a-z]*\n", "", sm_raw)
                sm_raw = re.sub(r"\n```$", "", sm_raw).strip()
            sm_data = json.loads(sm_raw)
            new_title = sm_data.get("title")
            if new_title and isinstance(new_title, str):
                meeting.title = new_title.strip()
        except Exception as exc:
            print(f"[_persist] Could not parse title from Scrum Master report: {exc}")
            sm_data = {}

        if discussion_log and hasattr(meeting, "discussion_log"):
            meeting.discussion_log = discussion_log

        # Backfill action items from the final summary if the live meeting didn't generate any
        try:
            from app.db.models import AgentAction
            from sqlalchemy import select
            existing_count = db.execute(
                select(AgentAction).where(AgentAction.meeting_id == meeting_id)
            ).scalars().first()

            if not existing_count:
                for t in sm_data.get("to_do", []):
                    task_text = t.get("task", "") if isinstance(t, dict) else str(t)
                    if task_text:
                        db.add(AgentAction(meeting_id=meeting_id, agent_role="scrum_master", action_type="to_do", content=task_text, status="accepted"))
                for p in sm_data.get("parking_lot", []):
                    task_text = p.get("task", "") if isinstance(p, dict) else str(p)
                    if task_text:
                        db.add(AgentAction(meeting_id=meeting_id, agent_role="scrum_master", action_type="parking_lot", content=task_text, status="accepted"))
                for s in sm_data.get("pending_to_schedule", []):
                    task_text = s.get("task", "") if isinstance(s, dict) else str(s)
                    if task_text:
                        db.add(AgentAction(meeting_id=meeting_id, agent_role="scrum_master", action_type="to_schedule", content=task_text, status="accepted"))
        except Exception as exc:
            print(f"[_persist] Failed to backfill action items: {exc}")

        try:
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            logger.exception(
                "Failed to persist final report for meeting %s", meeting_id
            )
            raise DatabaseError(
                f"Database constraint or connection failure: {exc}"
            ) from exc

