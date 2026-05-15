from __future__ import annotations

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[int, set[WebSocket]] = {}

    async def connect(self, meeting_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.setdefault(meeting_id, set()).add(ws)

    def disconnect(self, meeting_id: int, ws: WebSocket) -> None:
        conns = self._connections.get(meeting_id)
        if conns:
            conns.discard(ws)
            if not conns:
                self._connections.pop(meeting_id, None)

    async def broadcast(self, meeting_id: int, message: dict) -> None:
        conns = self._connections.get(meeting_id)
        if not conns:
            return
        dead: set[WebSocket] = set()
        for ws in list(conns):
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        if dead:
            for ws in dead:
                conns.discard(ws)
            if not conns:
                self._connections.pop(meeting_id, None)


manager = ConnectionManager()
