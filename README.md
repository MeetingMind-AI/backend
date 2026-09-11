# MeetingMind AI Backend (Brain Zone)

FastAPI service powering MeetingMind AI: meeting orchestration, transcript ingestion, multi-agent Agile report synthesis, role-based access control, and asynchronous digest dispatch.

---

## Tech Stack & Default Models

- **Framework:** FastAPI with Uvicorn ASGI server
- **Database:** PostgreSQL 15 (SQLAlchemy 2.0 ORM + Alembic migrations; persists relational data and user sessions)
- **Caching:** Redis 7 (asynchronous client with 60s TTL caching for Instant Clarity queries)
- **Semantic Memory:** Mem0 + Qdrant vector database (768-dimensional embeddings)
- **Email Delivery:** Resend HTTP API
- **AI Models (Ollama local inference):**
  - **Real-Time Insights & Instant Clarity:** `hermes3:8b` (configured via `OLLAMA_MODEL`)
  - **Final Multi-Round Debate & Synthesis:** `hermes3:8b` (configured via `OLLAMA_FINAL_MODEL`)
  - **Semantic Embeddings:** `nomic-embed-text` (configured via `MEM0_EMBED_MODEL`)

---

## Architectural Pipelines

### 1. Transcript Ingestion & Utterance Deduplication
The backend uses a REST polling strategy to fetch canonical speech segments from Vexa (`GET /transcripts/{platform}/{native_id}`) at regular intervals (default 3s).
- **Signature Mechanism:** Because Vexa returns cumulative transcripts on each poll, the engine calculates immutable utterance signatures formatted as `"{speaker}|||{text.strip()}"`. 
- **Set Difference Diffing:** By tracking `_seen_chunk_sigs[meeting_id]`, newly polled segments are isolated using `added_sigs = new_sigs - old_sigs`. Only genuinely new chunks are broadcast over WebSockets and queued for LLM analysis.
- **Producer-Consumer Batching Queue:** A background `_summary_worker` drains all queued transcript segments via `asyncio.Queue.get_nowait()`, batching them into a single consolidated prompt to prevent queue pile-up and protect Ollama from concurrency overload.

### 2. Progressive Speaker Retry
When a meeting terminates, remote conferencing providers (Google Meet, MS Teams) and Vexa asynchronously finalize attendee identities and diarization:
- `sync_speakers_from_vexa` executes progressive exponential backoff delays: **2s → 8s → 20s**.
- Halts immediately as soon as a non-empty list of attendees is retrieved, filtering out system audio entries (e.g. `'meeting audio'`).

### 3. Concurrency Protection (`_llm_semaphore`)
Local Ollama instances on consumer hardware (e.g. Apple Silicon Unified Memory or single NVIDIA GPUs) do not efficiently handle concurrent context evaluations. A global `asyncio.Semaphore(1)` gate enforces single-flight execution across all real-time summarization, Instant Clarity requests, and final report synthesis turns.

### 4. Multi-Round BOLAA Debate Pipeline
Final meeting reports are generated through a 3-stage agentic workflow:
1. **Parallel Initial Analysis:** Tech Lead and Product Manager inspect the transcript independently.
2. **Interactive Cross-Debate:** `DiscussionEngine` runs multi-round debate where personas review cumulative discussion history and challenge each other on technical constraints, delivery timelines, and architectural debt. Memories from Mem0 are grounded as reference-only background.
3. **Executive Synthesis:** Scrum Master receives the raw transcript, both initial analyses, and full debate logs, acting as neutral arbiter to output structured action items (`to_do`, `parking_lot`, `to_schedule`) and executive digest.

---

## REST & WebSocket Endpoints

### System & Diagnostics
- `GET /api/system/status`
  - Inspects real-time health across all services: Ollama (deployment type: host vs docker, VRAM allocation, loaded models, latency), PostgreSQL connectivity, Redis cache, Qdrant vector store, and STT gateway.
- `GET /health`
  - Lightweight liveness probe returning `{"status": "ok"}`.

### Authentication (`/api/auth`)
- `POST /api/auth/signup` — Register user, set session cookie `mm_session`.
- `POST /api/auth/login` — Authenticate credentials, set session cookie.
- `POST /api/auth/logout` — Destroy session record and clear cookie.
- `GET /api/auth/me` — Return current authenticated user profile.
- `PATCH /api/auth/me` — Update display name and/or avatar photo (`photo_b64`).
- `GET /api/auth/photo/{user_id}` — Stream user avatar image.

### Teams & Memberships (`/api/teams`)
- `GET /api/teams` — List teams for authenticated user.
- `POST /api/teams` — Create team (caller assigned as owner).
- `GET /api/teams/{team_id}` — Team details with members and topics.
- `PATCH /api/teams/{team_id}` — Rename team (owner only).
- `POST /api/teams/{team_id}/transfer-ownership` — Transfer team ownership to another active member (owner only).
- `DELETE /api/teams/{team_id}` — Permanently delete team and cascade-delete all meetings, transcripts, actions, topics, and memberships (owner only).
- `POST /api/teams/{team_id}/leave` — Leave team (owner cannot leave without transferring ownership or deleting team).
- `GET /api/teams/{team_id}/invite` — Retrieve team invite link (owner only).
- `POST /api/teams/join/{invite_token}` — Join team using invite token.
- `GET /api/teams/{team_id}/members` — List all members and roles.
- `DELETE /api/teams/{team_id}/members/{user_id}` — Remove member from team (owner only).
- `PATCH /api/teams/{team_id}/members/{user_id}` — Update team member `role` (`scrum_master`, `product_manager`, `team_member`; `'admin'` is mapped to `scrum_master`, `'member'` to `team_member`) and `notification_preferences` (tag filter array).

### Team Topics (`/api/teams/{team_id}/topics`)
- `GET /api/teams/{team_id}/topics` — List all topics for team.
- `POST /api/teams/{team_id}/topics` — Create topic (`name`, `color` hex).
- `PATCH /api/teams/{team_id}/topics/{topic_id}` — Update topic name or color.
- `DELETE /api/teams/{team_id}/topics/{topic_id}` — Delete topic.

### Team Prompt Overrides (`/api/teams/{team_id}/prompts`)
- `GET /api/teams/{team_id}/prompts` — Fetch all 13 registered prompt templates (8 customizable persona system prompts and 5 read-only user templates) with team overrides or defaults.
- `PUT /api/teams/{team_id}/prompts/{prompt_key}` — Set custom prompt override for any of the 8 customizable personas (returns 400 Bad Request for read-only user templates).
- `DELETE /api/teams/{team_id}/prompts/{prompt_key}` — Reset prompt template back to global default.

### Meeting Controls & Lifecycle (`/api/meetings`)
- `POST /api/meetings/start` — Launch Vexa bot (`platform`, `native_id`, `team_id`, optional `passcode`, optional `meeting_type`). Returns `{"meeting_id": ..., "meeting_type": ...}`.
- `POST /api/meetings/{meeting_id}/leave` — Instruct bot to leave; initiates background finalization.
- `POST /api/meetings/{meeting_id}/redispatch` — Redeploy bot to ongoing meeting if disconnected and reattach pollers.
- `POST /api/meetings/{meeting_id}/resummarize` — Re-run full final multi-agent debate and summary generation from current transcript (admin/owner only). Cancels any active task and resets state (returns HTTP 200 with status and meeting data).
- `POST /api/meetings/{meeting_id}/stop-summary` — Cancel active background summary generation task.
- `GET /api/meetings/{meeting_id}/summary-thoughts` — Retrieve live streaming thought logs generated during active synthesis.
- `GET /api/meetings` — List meetings (supports `?team_id=` filter).
- `GET /api/meetings/{meeting_id}` — Meeting metadata, summary JSON, topics, and speakers.
- `PATCH /api/meetings/{meeting_id}` — Rename meeting title and/or update `meeting_type` (returns `{"ok": True, "title": ..., "meeting_type": ...}`).
- `DELETE /api/meetings/{meeting_id}` — Delete meeting record, transcript chunks, and action items.
- `POST /api/meetings/{meeting_id}/topics/{topic_id}` — Associate topic tag to meeting.
- `DELETE /api/meetings/{meeting_id}/topics/{topic_id}` — Dissociate topic tag from meeting.

### Transcript Ingestion & Revision Audit
- `GET /api/meetings/{meeting_id}/transcript` — Retrieve chronological transcript chunks with audit metadata (`is_edited`, `original_text`, `original_speaker`, `edited_at`).
- `PATCH /api/meetings/{meeting_id}/transcript/{chunk_id}` — Edit text or speaker (admin/owner only). Freezes `original_text` and `original_speaker` on first edit, sets `is_edited=True`, `edited_at=now()`, `edited_by=user_id`.
- `POST /api/meetings/{meeting_id}/transcript/{chunk_id}/revert` — Lossless restore back to original speech-to-text capture and speaker, resetting `is_edited=False`.
- `DELETE /api/meetings/{meeting_id}/transcript/{chunk_id}` — Delete transcript chunk (admin/owner only).
- `POST /api/meetings/{meeting_id}/transcript` — Manually insert transcript chunk (admin/owner only).
- `WS /api/ws/ingest/{meeting_id}` — Bidirectional WebSocket stream broadcasting real-time transcript chunks, insights, action proposals, and multi-agent thoughts to clients; also accepts client-sent speech utterances.

### Action Items & Proposals (`/api/actions`)
- `GET /api/actions` — List all action items across meetings, grouped by category (`parking_lot`, `to_do`, `to_schedule`, `blocker`) and status (`pending`, `accepted`, `rejected`, `archived`). Supports `?team_id=` filter.
- `GET /api/meetings/{meeting_id}/actions` — List action items for a single meeting.
- `POST /api/meetings/{meeting_id}/actions` — Manually create an action item (`action_type`, `content`, optional `assignee_id`, `status` [defaults to `accepted`], `tags`).
- `PATCH /api/meetings/{meeting_id}/actions/{action_id}` — Review or update action item (`status`, `content`, `assignee_id`, `action_type`, `tags`).
- `DELETE /api/meetings/{meeting_id}/actions/{action_id}` — Delete action item.

### AI Explanations & Email Reports
- `POST /api/meetings/{meeting_id}/explain` — Generate instant clarity explanation (`mode`: `"technical"` or `"business"`, optional `last_x_minutes`). Uses SHA-256 hashed 60s Redis caching with fail-open fallback.
- `GET /api/meetings/{meeting_id}/email-preview` — Render responsive, email-client-compatible HTML summary digest.
- `POST /api/meetings/{meeting_id}/send-email` — Deliver email summary digest to specified `recipient_ids` via Resend API.
- `POST /api/vexa/webhook` — Ingest asynchronous bot status events from Vexa.

---

## Database Schema Tables

| Table | Key Columns | Description |
|---|---|---|
| `users` | `id`, `name`, `email`, `password_hash`, `photo`, `created_at` | Application users and credentials. |
| `sessions` | `id`, `user_id` (FK), `token`, `created_at` | Active user authentication cookie sessions. |
| `teams` | `id`, `name`, `owner_id` (FK), `invite_token`, `created_at` | Workspaces grouping users, meetings, topics, and custom prompts. |
| `team_memberships` | `id`, `user_id` (FK), `team_id` (FK), `joined_at`, `role`, `notification_preferences` | User membership, canonical Agile role (`scrum_master`, `product_manager`, `team_member`), and notification tag filter preferences. |
| `meetings` | `id`, `vexa_meeting_id`, `title`, `status`, `meeting_type`, `summary`, `discussion_log`, `speakers`, `team_id` (FK), `created_by` (FK), `created_at` | Meeting records, status, Agile meeting type, multi-persona summary JSON, multi-round debate logs, and synced speaker lists. |
| `transcript_chunks` | `id`, `meeting_id` (FK), `speaker`, `text`, `timestamp`, `is_edited`, `original_text`, `original_speaker`, `edited_at`, `edited_by` (FK) | Speech utterances with audit tracking and non-destructive revert support. |
| `agent_actions` | `id`, `meeting_id` (FK), `assignee_id` (FK), `agent_role`, `action_type`, `content`, `status`, `tags` | Action items (`parking_lot`, `to_do`, `to_schedule`, `blocker`), approval status (`pending`, `accepted`, `rejected`, `archived`), tags, and assignee. |
| `topics` | `id`, `team_id` (FK), `name`, `color`, `created_at` | Custom color-coded labels for meetings. |
| `meeting_topics` | `meeting_id` (FK), `topic_id` (FK) | Composite junction table linking meetings to topics. |
| `team_prompt_configs` | `id`, `team_id` (FK), `prompt_key`, `prompt_text`, `updated_at` | Per-team persona prompt overrides across 13 registered templates. |

---

## Running Locally

Run as part of the Docker Compose stack:
```bash
# Start backend container
docker compose up -d --build backend

# View live backend logs and real-time AI summaries
docker compose logs -f backend
```

Interactive API documentation:
- **Swagger UI:** `http://localhost:8000/docs`
- **ReDoc:** `http://localhost:8000/redoc`

### Environment Variables
- `DATABASE_URL`: PostgreSQL connection string (e.g. `postgresql://postgres:postgres@db:5432/postgres`)
- `REDIS_URL`: Redis connection string (e.g. `redis://redis:6379/0`)
- `VEXA_API_KEY`: Authentication key for Vexa meeting bots
- `VEXA_API_BASE_URL`: Base URL for Vexa API Gateway (default: `http://host.docker.internal:8056`)
- `OLLAMA_URL`: Ollama generate endpoint (default: `http://ollama:11434/api/generate`)
- `OLLAMA_MODEL`: Real-time model (default: `hermes3:8b`)
- `OLLAMA_FINAL_MODEL`: Final debate/synthesis model (default: `hermes3:8b`)
- `MEM0_EMBED_MODEL`: Vector embedding model (default: `nomic-embed-text`)
- `RESEND_API_KEY`: API key for delivering email digests
- `EMAIL_FROM`: Sender email address (default: `MeetingMind <onboarding@resend.dev>`)
