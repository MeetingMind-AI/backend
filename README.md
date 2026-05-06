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
  - Body: `{ "platform": "<platform>", "native_id": "<meeting-id>" }`
  - Supported `platform` values: `google_meet`, `zoom`, `teams`
  - Starts a Vexa bot for a target platform/native meeting ID
- `POST /api/meetings/{meeting_id}/leave`
  - Force bot to leave meeting via Vexa bot delete API
- `POST /api/vexa/webhook`
  - Receives Vexa lifecycle events for fallback completion sync

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
| `final_summary` | `Text` | `NULL` | The generated final markdown summary report from Ollama. |
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
- **`VEXA_WS_URL`**
  - **What it is:** The WebSocket endpoint for live transcript listening.
  - **How to get it:** Leave blank to use the default `ws://host.docker.internal:8056/ws`.
- **`VEXA_WEBHOOK_SECRET`** (Optional)
  - **What it is:** A secret key used to validate incoming webhook payload signatures from Vexa.
  - **How to get it:** Ensure both repositories share the same secret key in their `.env` files.
- **`VEXA_MEETING_POLL_INTERVAL_SECONDS`** (Optional)
  - **What it is:** Polling cadence fallback (in seconds) in case the WebSocket disconnects.
  - **How to get it:** Defaults to `10`. No setup required.

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
   docker-compose up -d --build backend
   ```
2. **Follow the service logs:**
   ```bash
   docker-compose logs -f backend
   ```

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
