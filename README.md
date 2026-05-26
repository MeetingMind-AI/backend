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

### Authentication

All authenticated endpoints read the `mm_session` cookie set on login/signup. Endpoints that require auth return `401` if the cookie is missing or invalid.

- `POST /api/auth/signup`
  - Body: `{ "email": "...", "name": "...", "password": "...", "confirm_password": "...", "photo_b64": null }`
  - Creates a user, opens a session, sets the `mm_session` cookie. Returns `{"user": {id, name, email, photo_url}}`.
  - `409` if email already registered. `422` if passwords don't match or are too short. `413` if photo > 500 KB.

- `POST /api/auth/login`
  - Body: `{ "email": "...", "password": "..." }`
  - Sets `mm_session` cookie. Returns `{"user": {id, name, email, photo_url}}`.
  - `401` on invalid credentials.

- `POST /api/auth/logout`
  - Deletes the session record and clears the cookie. Returns `{"ok": "logged out"}`.

- `GET /api/auth/me`
  - Returns the currently authenticated user: `{id, name, email, photo_url}`. `401` if not logged in.

- `GET /api/auth/photo/{user_id}`
  - Returns the raw profile photo (JPEG). `404` if the user has no photo.

### Teams

All team endpoints require a valid session cookie. Members-only actions return `403` if the caller is not a member; owner-only actions return `403` if the caller is not the owner.

- `GET /api/teams`
  - Returns all teams the current user belongs to.
  ```json
  { "teams": [{ "id": 1, "name": "Acme", "owner_id": 2, "is_owner": true, "member_count": 4, "created_at": "..." }] }
  ```

- `POST /api/teams`
  - Body: `{ "name": "Team name" }`
  - Creates a team and automatically adds the creator as a member and owner. Returns the new team object.

- `GET /api/teams/{team_id}`
  - Returns full team detail including members and topics list. Requires membership.
  ```json
  {
    "id": 1, "name": "Acme", "owner_id": 2, "is_owner": true,
    "invite_token": "abc123",
    "members": [{ "id": 2, "name": "Alice", "email": "alice@...", "is_owner": true }],
    "topics": [{ "id": 3, "name": "Backend", "color": "#4f8ef7" }]
  }
  ```

- `PATCH /api/teams/{team_id}`
  - Body: `{ "name": "New name" }`. Owner only. Returns `{id, name}`.

- `POST /api/teams/{team_id}/leave`
  - Removes the current user from the team. Owners cannot leave (`400`).

- `GET /api/teams/{team_id}/invite`
  - Owner only. Returns `{ "invite_url": "...", "invite_token": "..." }`.

- `POST /api/teams/join/{invite_token}`
  - Joins the team identified by the invite token. Idempotent — safe to call if already a member. Returns `{ "team_id": 1, "team_name": "Acme" }`.

- `GET /api/teams/{team_id}/members`
  - Returns all members of the team. Requires membership.
  ```json
  { "members": [{ "id": 2, "name": "Alice", "email": "alice@...", "is_owner": true }] }
  ```

- `DELETE /api/teams/{team_id}/members/{target_user_id}`
  - Owner only. Removes a member from the team. Cannot kick yourself (`400`). Returns `{"ok": true}`.

### Team Topics

- `GET /api/teams/{team_id}/topics`
  - Lists all topics for a team. Requires membership.
  ```json
  { "topics": [{ "id": 3, "name": "Backend", "color": "#4f8ef7" }] }
  ```

- `POST /api/teams/{team_id}/topics`
  - Body: `{ "name": "Backend", "color": "#4f8ef7" }`. Requires membership. Returns the created topic.

- `PATCH /api/teams/{team_id}/topics/{topic_id}`
  - Body: `{ "name": "...", "color": "..." }` (both optional). Requires membership. Returns the updated topic.

- `DELETE /api/teams/{team_id}/topics/{topic_id}`
  - Requires membership. Returns `{"ok": true}`.

### Health

- `GET /health` — Liveness probe.
  ```json
  {"status": "ok"}
  ```

### Meeting Lifecycle

- `POST /api/meetings/start`
  - Body: `{ "platform": "<platform>", "native_id": "<meeting-id>", "team_id": 1, "passcode": "" }`
  - `platform` (required): `google_meet`, `zoom`, or `teams`
  - `team_id` (optional): associates the meeting with a team
  - `passcode` (optional): required for passcode-protected Teams meetings
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

- `GET /api/meetings` — Lists all meetings ordered by `created_at` descending. Accepts optional `?team_id=` query param to filter by team.
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
        "team_id": 1,
        "created_at": "2026-05-15T10:00:00+00:00",
        "topics": [
          { "id": 3, "name": "Backend", "color": "#4f8ef7" }
        ]
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
    "team_id": 1,
    "created_at": "2026-05-15T10:00:00+00:00",
    "topics": [
      { "id": 3, "name": "Backend", "color": "#4f8ef7" }
    ]
  }
  ```
  Returns `404` if not found.
- `PATCH /api/meetings/{meeting_id}` — Renames a meeting. Body: `{ "title": "new title" }`. Returns `{"id": ..., "title": ...}`.
- `DELETE /api/meetings/{meeting_id}` — Deletes a meeting record. Returns `{"ok": True}`. Returns `404` if not found.

### Meeting Topics

Topics are team-scoped labels with a color. They can be assigned to meetings as tags.

- `POST /api/meetings/{meeting_id}/topics/{topic_id}` — Assigns a topic to a meeting. Returns `{"ok": true}`.
- `DELETE /api/meetings/{meeting_id}/topics/{topic_id}` — Removes a topic from a meeting. Returns `{"ok": true}`.

> Topic CRUD (create / list / update / delete) is managed under the Teams API: `GET|POST|PATCH|DELETE /api/teams/{team_id}/topics`.

### Proposals (Parking Lot / To Do / To Schedule)

During live ingestion, the LLM detects three types of proposals from each utterance and persists them as pending `AgentAction` rows:

| `action_type` | Trigger |

| `parking_lot` | Speaker is blocked, defers, or tables a topic (stuck, park it, later, offline) |
| `to_do` | A concrete action item assigned to someone |
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
    "to_do": {
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

- `GET /api/actions` — Lists ALL proposals across all meetings, grouped by type then status (same shape as above but with `meeting_id`, `meeting_title`, `meeting_date` included in each entry).

- `PATCH /api/meetings/{meeting_id}/actions/{action_id}` — Accept, reject, reset, or edit a proposal.
  - Body `{ "status": "accepted" }` — marks the action as accepted. Returns `{"ok": true, "id": 1, "status": "accepted", "content": "..."}`.
  - Body `{ "status": "rejected" }` — marks the action as rejected. Returns `{"ok": true, "id": 1, "status": "rejected", "content": "..."}`.
  - Body `{ "status": "pending" }` — resets the action to pending (undo). Returns `{"ok": true, "id": 1, "status": "pending", "content": "..."}`.
  - Body `{ "content": "new text" }` — updates the action text (leaves status unchanged). Returns `{"ok": true, "id": 1, "status": "...", "content": "new text"}`.

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
      "type": "to_do",
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
| `discussion_log` | `JSONB` | `NULL` | Reserved for storing a structured discussion log. |
| `team_id` | `Integer` | `NULL` | FK → `teams.id` (SET NULL on delete). Associates the meeting with a team. |
| `created_by` | `Integer` | `NULL` | FK → `users.id` (SET NULL on delete). The user who dispatched the bot. |
| `created_at` | `DateTime` | `now()` | Local timestamp of when the meeting record was created. |

#### 2. `users` Table

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | `Integer` | Primary Key | |
| `name` | `String(120)` | | Display name. |
| `email` | `String(150)` | Unique, Indexed | Login email. |
| `password_hash` | `String(128)` | | bcrypt hash. |
| `photo` | `LargeBinary` | `NULL` | Raw JPEG profile photo (max 500 KB). |
| `created_at` | `DateTime` | `now()` | |

#### 3. `sessions` Table

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | `Integer` | Primary Key | |
| `user_id` | `Integer` | Indexed | FK → `users.id` (CASCADE delete). |
| `token` | `String(64)` | Unique, Indexed | The `mm_session` cookie value. |
| `created_at` | `DateTime` | `now()` | |

#### 4. `teams` Table

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | `Integer` | Primary Key | |
| `name` | `String(120)` | | Team display name. |
| `owner_id` | `Integer` | `NULL` | FK → `users.id` (SET NULL on delete). |
| `invite_token` | `String(64)` | Unique, Indexed | Token used in invite links. |
| `created_at` | `DateTime` | `now()` | |

#### 5. `team_memberships` Table

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | `Integer` | Primary Key | |
| `user_id` | `Integer` | Indexed | FK → `users.id` (CASCADE delete). |
| `team_id` | `Integer` | Indexed | FK → `teams.id` (CASCADE delete). |
| `joined_at` | `DateTime` | `now()` | |

Unique constraint on `(user_id, team_id)`.

#### 6. `transcript_chunks` Table
Stores raw transcription snippets returned by Vexa WebSocket events and synced logs.

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | `Integer` | Primary Key | Unique ID for each speech chunk. |
| `meeting_id` | `Integer` | Indexed | FK → `meetings.id` (CASCADE delete). |
| `speaker` | `String(120)` | | Name of the person speaking. |
| `text` | `Text` | | The transcribed speech. |
| `timestamp` | `DateTime` | | The absolute start time of the speech chunk. |

#### 7. `agent_actions` Table
Stores AI-detected proposals (to-dos, parking lot items, items to schedule) extracted during live transcript ingestion.

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | `Integer` | Primary Key | |
| `meeting_id` | `Integer` | Indexed | FK → `meetings.id` (CASCADE delete). |
| `agent_role` | `String(120)` | | The AI persona that raised the action (e.g. `scrum_master`). |
| `action_type` | `String(120)` | | `to_do`, `parking_lot`, or `to_schedule`. |
| `content` | `Text` | | The action text. |
| `status` | `String(20)` | `'pending'` | `pending`, `accepted`, or `rejected`. |

#### 8. `topics` Table
Team-scoped labels that can be assigned to meetings as tags.

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | `Integer` | Primary Key | |
| `team_id` | `Integer` | Indexed | FK → `teams.id` (CASCADE delete). |
| `name` | `String(80)` | | Display name of the topic. |
| `color` | `String(7)` | `'#4f8ef7'` | Hex color used to render the tag in the UI. |
| `created_at` | `DateTime` | `now()` | |

#### 9. `meeting_topics` Table
Many-to-many association between meetings and topics.

| Column | Type | Description |
|--------|------|-------------|
| `meeting_id` | `Integer` | FK → `meetings.id` (CASCADE delete). Part of composite PK. |
| `topic_id` | `Integer` | FK → `topics.id` (CASCADE delete). Part of composite PK. |

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

### Email Notifications (SendGrid)

When a meeting ends and its summary is generated, the system can automatically email the report to every member of the meeting's team. This feature is **disabled by default** — it activates only when `SMTP_HOST` is set.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SMTP_HOST` | ✅ | — | SMTP server hostname. Set to `smtp.sendgrid.net` for SendGrid. |
| `SMTP_PORT` | — | `587` | SMTP server port. `587` works for SendGrid with STARTTLS. |
| `SMTP_USER` | ✅ | — | Must be the literal string `apikey` for SendGrid. |
| `SMTP_PASSWORD` | ✅ | — | Your SendGrid API key (starts with `SG.`). |
| `SMTP_FROM` | ✅ | `SMTP_USER` | The verified sender email address configured in SendGrid. |
| `SMTP_USE_TLS` | — | `true` | Enable STARTTLS. Keep `true` for SendGrid. |

#### Setup steps

1. **Create a SendGrid account** at [sendgrid.com](https://sendgrid.com/) if you don't have one.

2. **Verify a sender identity** — SendGrid requires a verified "From" address before it will deliver email:
   - Go to **Settings → Sender Authentication** in the SendGrid dashboard.
   - Either verify a **Single Sender** (quickest for development) or authenticate your entire domain (recommended for production).
   - Note the verified email address — you'll use it as `SMTP_FROM`.

3. **Generate an API key:**
   - Go to **Settings → API Keys → Create API Key**.
   - Choose **Restricted Access** and enable only **Mail Send → Full Access**.
   - Copy the generated key (it starts with `SG.` and is only shown once).

4. **Add these variables to your `.env` file:**
   ```env
   SMTP_HOST=smtp.sendgrid.net
   SMTP_PORT=587
   SMTP_USER=apikey
   SMTP_PASSWORD=SG.xxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   SMTP_FROM=your-verified-sender@yourdomain.com
   ```

5. **Restart the backend** to pick up the new configuration:
   ```bash
   docker compose restart backend
   ```

6. **Verify** that email is working by checking the logs after a meeting ends:
   ```bash
   docker compose logs -f backend | grep "\[Email\]"
   ```
   - ✅ `[Email] Summary for meeting 5 sent to 3 recipient(s): alice@..., bob@..., carol@...`
   - ⚠️ `[Email] SMTP not configured — skipping email notifications.` — means `SMTP_HOST` is not set.
   - ❌ `[Email] Failed to send summary for meeting 5: ...` — check your API key and sender verification.

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
