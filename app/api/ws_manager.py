"""
WebSocket Connection Manager Module.

Manages active client WebSocket connections per meeting ID, allowing real-time
broadcast of transcript updates, summaries, and agent actions to subscribed clients.
"""

from __future__ import annotations

from fastapi import WebSocket


class ConnectionManager:
    """Manages active WebSocket connections grouped by meeting ID.

    Attributes:
        _connections (dict[int, set[WebSocket]]): Mapping from meeting ID to connected websockets.
    """

    def __init__(self) -> None:
        """Initialize empty WebSocket connections store."""
        self._connections: dict[int, set[WebSocket]] = {}

    async def connect(self, meeting_id: int, ws: WebSocket) -> None:
        """Accept an incoming WebSocket connection and register it to a meeting ID.

        Args:
            meeting_id (int): Primary key ID of the target meeting.
            ws (WebSocket): Incoming FastAPI WebSocket instance.
        """
        await ws.accept()
        self._connections.setdefault(meeting_id, set()).add(ws)

    def disconnect(self, meeting_id: int, ws: WebSocket) -> None:
        """Remove a disconnected WebSocket from the registered set.

        Args:
            meeting_id (int): Primary key ID of the meeting.
            ws (WebSocket): Disconnected WebSocket instance.
        """
        conns = self._connections.get(meeting_id)
        if conns:
            conns.discard(ws)
            if not conns:
                self._connections.pop(meeting_id, None)

    async def broadcast(self, meeting_id: int, message: dict) -> None:
        """Broadcast a JSON message payload to all active WebSocket clients for a meeting.

        Silently handles disconnected or failing sockets by purging dead connections.

        Args:
            meeting_id (int): Target meeting ID.
            message (dict): JSON-serializable payload dictionary.
        """
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


# Global singleton instance for app-wide WebSocket connection management
manager = ConnectionManager()

