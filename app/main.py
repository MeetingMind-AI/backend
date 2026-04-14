from fastapi import FastAPI

from app.api.websockets import router as websocket_router

app = FastAPI(title="MeetingMind AI Backend")

app.include_router(websocket_router)


@app.get("/health", tags=["health"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}
