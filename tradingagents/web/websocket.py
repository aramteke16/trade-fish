"""WebSocket connection manager for live updates."""

import asyncio
import json
import logging
from typing import List, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)

_loop: Optional[asyncio.AbstractEventLoop] = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.disconnect(conn)


manager = ConnectionManager()
connect = manager.connect
disconnect = manager.disconnect
broadcast = manager.broadcast


def broadcast_sync(message: dict) -> None:
    """Fire-and-forget broadcast from sync code (APScheduler, LangGraph callbacks).

    Uses the event loop stashed by set_loop() during FastAPI lifespan.
    No-op when no loop is set (CLI runs, tests).
    """
    if _loop is None or _loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(broadcast(message), _loop)
    except Exception:
        logger.debug("broadcast_sync failed", exc_info=True)
