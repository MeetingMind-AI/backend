# MeetingMind AI Backend (Brain Zone)

This directory contains the FastAPI backend for the MeetingMind AI application. 

It is responsible for:
- Orchestrating Vexa meeting bots via REST API
- Transcript ingestion and cleanup via PostgreSQL + Redis
- AI analysis and report generation via Ollama local models
- Exposing the core REST and WebSocket APIs for the frontend UI

## 📚 Documentation

Detailed architecture documentation is available in the root `docs/` folder:
- **[Backend Architecture & API Reference](../docs/architecture/backend.md)**
- **[Monorepo Overview & Setup](../docs/README.md)**

## 🚀 Tech Stack

- **Framework:** FastAPI
- **Database:** PostgreSQL 15 (SQLAlchemy + Alembic for migrations)
- **Caching & PubSub:** Redis
- **AI Inference:** Ollama local inference (`llama3` by default)
- **Vector DB / Memory:** Mem0 semantic memory (optional)

## 🏗 Architecture & Strategies

### Transcript Ingestion
The backend uses a REST API polling strategy to fetch canonical transcripts from Vexa (`GET /transcripts/{platform}/{native_id}`). This ensures the backend receives clean, merged, and pause-ignored speech segments. `asyncio` background tasks poll Vexa while the meeting is active and persist the data to the `transcript_chunks` table in PostgreSQL.

### Meeting Finalization & Multi-Persona AI
When a meeting concludes:
1. The canonical transcript and speaker list are synced from Vexa.
2. An Ollama-powered Multi-Persona AI pipeline runs:
   - **Tech Lead** and **Product Manager** agents run in parallel to extract technical debt, blockers, and feature requests.
   - **Scrum Master** synthesizes the findings and the raw transcript to output a structured JSON report (summary, parking lot, to-do, to-schedule).
3. The report is saved into the database and exposed to the frontend.

## ⚙️ Running Locally

This service is designed to run as part of the `docker-compose.yml` stack defined in the root directory.

```bash
# From the root directory:
docker compose up -d --build backend
```

Once running, the interactive API documentation is available at:
- **Swagger UI:** `http://localhost:8000/docs`
- **ReDoc:** `http://localhost:8000/redoc`

You can view the backend logs and real-time AI summaries by running:
```bash
docker compose logs -f backend
```

## 🔐 Environment Variables

The backend requires several environment variables, typically managed via a `.env` file at the root of the project:

- `VEXA_API_KEY`: Required to authenticate with the local Vexa bot manager.
- `DATABASE_URL`: Full connection string for PostgreSQL (e.g., `postgresql://postgres:postgres@db:5432/postgres`).
- `REDIS_URL`: Full connection string for Redis.
- `VEXA_API_BASE_URL`: The Vexa API gateway address (default: `http://host.docker.internal:8056`).
- `OLLAMA_HOST`: Ensure Ollama is reachable at `http://host.docker.internal:11434` or configure accordingly.

## 🗄️ Database Migrations

Database schemas are managed with Alembic. To create or apply migrations:

```bash
# Generate a new migration
docker compose exec backend alembic revision --autogenerate -m "description"

# Apply pending migrations
docker compose exec backend alembic upgrade head
```
