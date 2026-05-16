# MeetingMind AI Backend

FastAPI service for meeting orchestration, transcript ingestion, and Agile-focused AI reporting.

## Responsibilities

- Start Vexa bots for meetings
- Consume Vexa REST API for fully merged, pause-ignored transcripts
- Persist transcripts to PostgreSQL
- Generate live Agile insights and final meeting markdown reports via Ollama
- Provide endpoints to control meeting lifecycle (including forcing bot leave)

## Tech Stack

- FastAPI
- SQLAlchemy + Alembic
- PostgreSQL 15
- Redis
- Ollama local inference (`llama3` by default)

## Service Endpoints

### Health

- `GET /health` — Liveness probe.
  ```json
  {"status": "ok"}
  ```

### Meeting Lifecycle

- `POST /api/meetings/start`
  - Body: `{ "platform": "<platform>", "native_id": "<meeting-id>" }`
  - Supported `platform` values: `google_meet`, `zoom`, `teams`
  - Deploys a Vexa bot to join the meeting. Upserts the meeting record (re-uses existing row if `vexa_meeting_id` already exists). Schedules background tasks to poll transcripts and monitor the meeting lifecycle until completion. Returns `{"meeting_id": ...}`.

- `POST /api/meetings/{meeting_id}/leave`
  - Instructs the Vexa bot to leave the meeting via the Vexa bot DELETE API. Returns `{"ok": True}`.

### AI Explanations

- `POST /api/meetings/{meeting_id}/explain`
  - Body: `{ "mode": "technical", "last_x_minutes": 2 }`
  - Generates an LLM-powered "instant clarity" explanation of recent transcript content. Supports `"technical"` or `"business"` personas. Filters by `last_x_minutes` if provided.
  - Response:
    ```json
    {
      "explanation": "The team discussed the API authentication refactor. Bob suggested OAuth2, but Alice raised concerns about complexity. They agreed to spike it next sprint."
    }
    ```

### Vexa Webhook

- `POST /api/vexa/webhook`
  - Receives Vexa lifecycle events (`meeting.status_change`, `meeting.completed`, etc.). Validates an optional Bearer token, updates meeting status in the local DB, and schedules a final transcript sync if the meeting reached a terminal status.

### Meeting CRUD

- `GET /api/meetings` — Lists all meetings ordered by `created_at` descending.
  ```json
  {
    "meetings": [
      {
        "id": 1,
        "title": "Sprint Planning",
        "status": "completed",
        "summary": {
          "tech_lead": "{...}",
          "product_manager": "{...}",
          "scrum_master": "{\"summary\": \"...\", \"pending_to_schedule\": [], \"parking_lot\": [], \"to_do\": []}"
        },
        "created_at": "2026-05-15T10:00:00+00:00"
      }
    ]
  }
  ```
- `GET /api/meetings/{meeting_id}` — Retrieves a single meeting by its local DB id.
  ```json
  {
    "id": 1,
    "title": "Sprint Planning",
    "status": "completed",
    "summary": {
      "tech_lead": "{...}",
      "product_manager": "{...}",
      "scrum_master": "{\"summary\": \"...\", \"pending_to_schedule\": [], \"parking_lot\": [], \"to_do\": []}"
    },
    "created_at": "2026-05-15T10:00:00+00:00"
  }
  ```
  Returns `404` if not found.
- `PATCH /api/meetings/{meeting_id}` — Renames a meeting. Body: `{ "title": "new title" }`. Returns `{"id": ..., "title": ...}`.
- `DELETE /api/meetings/{meeting_id}` — Deletes a meeting record. Returns `{"ok": True}`. Returns `404` if not found.

### Proposals (Parking Lot / Task / To Schedule)

During live ingestion, the LLM detects three types of proposals from each utterance and persists them as pending `AgentAction` rows:

| `action_type` | Trigger |

| `parking_lot` | Speaker is blocked, defers, or tables a topic (stuck, park it, later, offline) |
| `task` | A concrete action item assigned to someone |
| `to_schedule` | A follow-up meeting, discussion, or sync that needs to be scheduled |

- `GET /api/meetings/{meeting_id}/actions` — Lists all proposals for a meeting, grouped by type, then by status.
  ```json
  {
    "parking_lot": {
      "pending": [
        {
          "id": 1,
          "agent_role": "scrum_master",
          "content": "New framework discussion deferred to later.",
          "status": "pending"
        }
      ],
      "accepted": [
        {
          "id": 2,
          "agent_role": "scrum_master",
          "content": "Disagrees with Bob on OAuth approach.",
          "status": "accepted"
        }
      ],
      "rejected": []
    },
    "task": {
      "pending": [
        {
          "id": 3,
          "agent_role": "scrum_master",
          "content": "Alice to update the API documentation.",
          "status": "pending"
        }
      ],
      "accepted": [],
      "rejected": []
    },
    "to_schedule": {
      "pending": [],
      "accepted": [],
      "rejected": []
    }
  }
  ```

- `PATCH /api/meetings/{meeting_id}/actions/{action_id}` — Accept or reject a proposal.
  - Body `{ "status": "accepted" }` — marks the action as accepted. Returns `{"ok": true, "id": 1, "status": "accepted"}`.
  - Body `{ "status": "rejected" }` — marks the action as rejected. Returns `{"ok": true, "id": 1, "status": "rejected"}`.

### Transcripts

- `GET /api/meetings/{meeting_id}/transcript` — Fetches all transcript chunks for a meeting, ordered by timestamp ascending.
  ```json
  {
    "meeting_id": 1,
    "status": "completed",
    "chunks": [
      {
        "id": 42,
        "speaker": "Alice",
        "text": "Let's review the API design",
        "timestamp": "2026-05-15T10:05:00+00:00"
      }
    ]
  }
  ```

### WebSocket

- `WS /api/ws/ingest/{meeting_id}` — Accepts a WebSocket connection for live transcript ingestion. Send JSON `{"speaker": "Alice", "text": "..."}`. The server persists the chunk, runs a single LLM call for both summarization and proposal detection, and responds with:
  ```json
  {
    "ok": true,
    "meeting_id": 1,
    "chunk_id": 42,
    "summary": "Alice assigned to API documentation.",
    "proposal": {
      "type": "task",
      "content": "Alice to update the API documentation."
    }
  }
  ```
  The `proposal` field is omitted when the LLM detects nothing worth flagging.

Interactive docs:

- `http://localhost:8000/docs`

## View Real-Time Transcripts

Since the backend prints live transcript summaries to the console, you can view the live events directly from the Docker container logs. Run the following command:

```bash
docker compose logs -f backend
```

## View Transcripts in the Database

You can also verify the saved transcripts directly in the PostgreSQL database using `docker exec` and `psql`:

```bash
docker exec -it meetingmind_postgres psql -U meetingmind -d meetingmind
```

Once connected, you can run a SQL query to check the chunk data for a specific meeting:

```sql
SELECT id, speaker, LEFT(text, 80) AS text_preview, timestamp 
FROM transcript_chunks 
WHERE meeting_id = meeting_id 
ORDER BY timestamp;
```
### Database Schema Documentation

#### 1. `meetings` Table
Stores high-level metadata about meetings orchestrated by Vexa.

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | `Integer` | Primary Key | Internal tracking ID. |
| `vexa_meeting_id` | `String(128)` | Unique, Indexed | The meeting ID returned from the Vexa service. |
| `title` | `String(255)` | | The fallback or true title of the meeting. |
| `status` | `String(64)` | `'pending'` | The meeting lifecycle status (e.g. `active`, `completed`). |
| `summary` | `JSONB` | `NULL` | The generated final report from Ollama: dict with `tech_lead`, `product_manager`, and `scrum_master` keys. The `scrum_master` value is a JSON string with keys `summary`, `pending_to_schedule`, `parking_lot`, and `to_do`. |
| `created_at` | `DateTime` | `now()` | Local timestamp of when the meeting record was created. |

#### 2. `transcript_chunks` Table
Stores raw transcription snippets returned by Vexa WebSocket events and synced logs.

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | `Integer` | Primary Key | Unique ID for each speech chunk. |
| `meeting_id` | `Integer` | Indexed | Foreign Key linking back to `meetings(id)`. |
| `speaker` | `String(120)` | | Name of the person speaking. |
| `text` | `Text` | | The transcribed speech. |
| `timestamp` | `DateTime` | | The absolute start time of the speech chunk. |
## Running with Docker Compose

To quickly start the application and its dependencies (like PostgreSQL and Redis), you can use Docker Compose.

1. Ensure you have [Docker](https://docs.docker.com/get-docker/) installed.
2. Create a `.env` file in the root directory and populate it with the required environment variables (see the **Environment Variables** section below).
3. Build and launch the services in detached mode:
   ```bash
   docker compose up -d --build
   ```
4. View the logs to ensure everything started correctly:
   ```bash
   docker compose logs -f
   ```
5. To stop and remove the containers:
   ```bash
   docker compose down
   ```

## Environment Variables

Configure these variables via a `.env` file at the root of your project or through your docker-compose environment configuration.

### Required Authentication
- **`VEXA_API_KEY`** (Required)
  - **What it is:** The key used to authenticate with your local Vexa bot manager and WebSocket stream.
  - **How to get it:** You must generate a token from your local Vexa instance using its Admin API. 
    1. First, create a user:
       ```bash
       curl -X POST "http://localhost:8056/admin/users" \
         -H "Content-Type: application/json" \
         -H "X-Admin-API-Key: token" \
         -d '{"email": "my.assistant@example.com", "name": "AI Assistant"}'
       ```
    2. Next, generate a token for that user (assuming the new user ID is `1`):
       ```bash
       curl -X POST "http://localhost:8056/admin/users/1/tokens" \
         -H "Content-Type: application/json" \
         -H "X-Admin-API-Key: token" \
         -d '{"name": "Backend Key", "scopes": ["bot", "tx", "browser"]}'
       ```
    3. Finally, copy the generated token string from the response, set it in your environment (e.g., `VEXA_API_KEY=your_newly_copied_long_api_key_here`), and restart the backend container (`docker-compose restart backend` or `docker-compose stop backend && docker-compose up -d backend`).

### Database Configuration
You can pass the full URL directly (recommended) or pass connection parameters individually:

- **`DATABASE_URL`**
  - **What it is:** Full connection string for your PostgreSQL instance.
  - **How to get it:** Format it as `postgresql://<user>:<password>@<container_or_host>:<port>/<dbname>`. If you are running Postgres in Docker alongside this service, it will usually look like `postgresql://postgres:postgres@db:5432/postgres`.

If `DATABASE_URL` is omitted, the application will fallback to building the connection using these manually:
- **`POSTGRES_USER`** (default: `postgres`)
- **`POSTGRES_PASSWORD`** (default: `postgres`)
- **`POSTGRES_HOST`** (default: `localhost` — *Note: in Docker, you'll likely want to set this to your DB container name*)
- **`POSTGRES_PORT`** (default: `5432`)
- **`POSTGRES_DB`** (default: `postgres`)

- **`REDIS_URL`**
  - **What it is:** Full connection string for Redis.
  - **How to get it:** Format it for your local Docker Redis container (e.g., `redis://redis:6379/0`).

### Vexa URLs and Options
Because Vexa is running locally, these URLs should point to the Vexa container or your host machine's ports.

- **`VEXA_API_BASE_URL`** or **`VEXA_API_URL`**
  - **What it is:** The REST endpoint for bot control and transcript syncing.
  - **How to get it:** Leave blank to use the default `http://host.docker.internal:8056`.
- **`VEXA_WS_URL`** (Deprecated)
  - **What it is:** The WebSocket endpoint for live transcript listening. No longer used as we use REST polling.
- **`VEXA_WEBHOOK_SECRET`** (Optional)
  - **What it is:** A secret key used to validate incoming webhook payload signatures from Vexa.
  - **How to get it:** Ensure both repositories share the same secret key in their `.env` files.
- **`VEXA_MEETING_POLL_INTERVAL_SECONDS`** (Optional)
  - **What it is:** Polling cadence fallback (in seconds) in case the WebSocket disconnects.
  - **How to get it:** Defaults to `10`. No setup required.

## Transcript Ingestion Strategy (REST API Polling)

- We leverage the cleaner Vexa 0.10.6 API via `GET /transcripts/{platform}/{native_id}`.
- This bypasses raw websockets and chunk management in favor of automatically merged, pause-ignored segments directly from the Vexa database.
- `poll_transcripts_from_vexa` runs as an `asyncio` background task alongside `monitor_meeting_until_terminal` (both started via `asyncio.gather`).
- The poller periodically calls `sync_final_transcript_from_vexa` and upserts clean segments into the `TranscriptChunk` table.
- Live insight summarization (Ollama) runs on meaningful immutable text.

## Finalization Strategy

On meeting `completed`:

1. Sync canonical transcript from Vexa REST API.
2. Replace local transcript rows for that meeting with canonical ordered rows.
3. Generate final structured JSON report using the Multi-Persona Architecture:
   - **Tech Lead** and **Product Manager** agents run in parallel to extract technical debt, blockers, and feature requests.
   - **Scrum Master (Lead Synthesizer)** runs next, receiving the findings from the Tech Lead and PM along with the raw transcript.
   - The Scrum Master synthesizes the results, resolves conflicting constraints, and outputs the final master JSON report containing `summary`, `pending_to_schedule`, `parking_lot`, and `to_do`.
4. The report is saved in the `final_summary` (mapped internally to `summary`) JSONB column of the `meetings` table.
5. Emit progress logs while finalization is running.

## Local Development

### Prerequisites
1. **Docker & Docker Compose:** Required to run the API (and databases if defined in your compose file).
2. **Ollama:** The backend relies on Ollama for both real-time insights and final reports. 
   - Install Ollama on your host machine.
   - Make sure you pull the required model before running the backend:
     ```bash
     ollama pull llama3
     ```
   - Ollama must be reachable from the Docker container at `http://host.docker.internal:11434`. (You may need to set `OLLAMA_HOST=0.0.0.0` depending on your OS).

### Running the API

Run the following from the root directory of your project using Docker Compose:

1. **Build and start the backend service in the background:**
   ```bash
   docker compose up -d --build backend
   ```
2. **Follow the service logs:**
   ```bash
   docker compose logs -f backend
   ```

> Note: Ollama now runs as a Docker service (`meetingmind_ollama`) via the root `docker-compose.yml`.
> On Linux VMs with NVIDIA GPUs, uncomment the `deploy.resources` section in `docker-compose.yml` to enable GPU acceleration.

## Migrations

- Alembic config: `alembic.ini`
- Migration scripts: `alembic/versions`
- Generate a new migration after model changes:
  - `docker compose exec backend alembic revision --autogenerate -m "initial_tables"`
- Apply:
  - `docker compose exec backend alembic upgrade head`

## Debugging Checklist

- REST polling auth/subscription:
  - Ensure backend sees correct `VEXA_API_KEY`.
  - Confirm Vexa API Gateway reachable from container (`host.docker.internal:8056`).
- Missing/poor summaries:
  - Check Ollama availability at `http://ollama:11434` (Docker network) or `host.docker.internal:11434` (host).
  - Verify model exists and is loaded.
- Transcript quality issues:
  - Ensure polling background task is running (`docker compose logs -f backend`).
  - Validate final sync replaced rows on completion.
- Backend restart / duplicate meetings:
  - `start_meeting` now upserts by `vexa_meeting_id`, so restarting the backend won't create duplicate records.

## Security Notes

- Do not commit real API keys or secrets.
- Use `.env` / secret injection for deployment.
- Set `VEXA_WEBHOOK_SECRET` when webhook endpoint is exposed beyond localhost.
