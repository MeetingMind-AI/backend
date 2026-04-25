# MeetingMind AI Backend

FastAPI service for meeting orchestration, transcript ingestion, and Agile-focused AI reporting.

## Responsibilities

- Start Vexa bots for meetings
- Consume Vexa WebSocket events (meeting status and transcript updates)
- Persist transcripts to PostgreSQL
- Generate live Agile insights and final meeting markdown reports via Ollama
- Provide endpoints to control meeting lifecycle (including forcing bot leave)

## Tech Stack

- FastAPI
- SQLAlchemy + Alembic
- PostgreSQL 15
- Redis
- WebSockets client (`websockets`)
- Ollama local inference (`llama3` by default)

## Service Endpoints

- `GET /health`
- `POST /api/meetings/start`
  - Starts a Vexa bot for a target platform/native meeting ID
- `POST /api/meetings/{meeting_id}/leave`
  - Force bot to leave meeting via Vexa bot delete API
- `POST /api/vexa/webhook`
  - Receives Vexa lifecycle events for fallback completion sync

Interactive docs:

- `http://localhost:8000/docs`

## Environment Variables

- `DATABASE_URL` (required in production)
- `REDIS_URL`
- `VEXA_API_URL` (used for bot control; defaults handled by code paths)
- `VEXA_WS_URL` (for WS listener)
- `VEXA_API_KEY` (required)
- `VEXA_WEBHOOK_SECRET` (optional; validates webhook auth)
- `VEXA_MEETING_POLL_INTERVAL_SECONDS` (optional; poll fallback cadence)

## Transcript Ingestion Strategy

- Live WebSocket feed accepts `transcript.mutable` and `transcript.immutable` events.
- Segments are keyed by `absolute_start_time` and updated using `updated_at` precedence and text quality heuristics.
- DB writes update existing chunk rows by `(meeting_id, timestamp)` to avoid duplicate fragment rows.
- Live insight summarization runs only on meaningful immutable text.

## Finalization Strategy

On meeting `completed`:

1. Sync canonical transcript from Vexa REST API.
2. Replace local transcript rows for that meeting with canonical ordered rows.
3. Generate final markdown report with sections:
   - `## Summary`
   - `## Action Items`
   - `## Blockers`
4. Emit progress logs while finalization is running.

## Local Development

From monorepo root:

- Build and run backend only:
  - `docker-compose up -d --build backend`
- Follow logs:
  - `docker-compose logs -f backend`

If running outside Docker:

1. Install dependencies:
   - `pip install -r requirements.txt`
2. Run migrations:
   - `alembic upgrade head`
3. Start API:
   - `uvicorn app.main:app --host 0.0.0.0 --port 8000`

## Migrations

- Alembic config: `alembic.ini`
- Migration scripts: `alembic/versions`
- Generate a new migration after model changes:
  - `alembic revision --autogenerate -m "describe change"`
- Apply:
  - `alembic upgrade head`

## Debugging Checklist

- WS auth/subscription:
  - Ensure backend sees correct `VEXA_API_KEY`.
  - Confirm Vexa API Gateway reachable from container (`host.docker.internal:8056`).
- Missing/poor summaries:
  - Check Ollama availability at `host.docker.internal:11434`.
  - Verify model exists and is loaded.
- Transcript quality issues:
  - Compare live logs vs post-sync canonical rows.
  - Validate final sync replaced rows on completion.

## Security Notes

- Do not commit real API keys or secrets.
- Use `.env` / secret injection for deployment.
- Set `VEXA_WEBHOOK_SECRET` when webhook endpoint is exposed beyond localhost.
