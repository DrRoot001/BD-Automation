"""WebSocket endpoint for real-time dashboard updates (Module 5).

Subscribes to Redis pub/sub channels and broadcasts events to all
connected clients as JSON frames.

Connect: ws://localhost:8000/ws/updates
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

# ── Connection manager ────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        self._active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._active.add(ws)
        logger.info(f"[WS] Client connected — total={len(self._active)}")

    def disconnect(self, ws: WebSocket) -> None:
        self._active.discard(ws)
        logger.info(f"[WS] Client disconnected — total={len(self._active)}")

    async def broadcast(self, message: str) -> None:
        dead: Set[WebSocket] = set()
        for ws in list(self._active):
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        self._active -= dead


manager = ConnectionManager()

# ── Redis subscriber background task ─────────────────────────────────────────

SUBSCRIBED_CHANNELS = [
    "events:application.status_changed",
    "events:application.submitted",
    "events:application.failed",
    "events:email.classified",
    "events:interview.detected",
    "events:job.discovered",
]

_subscriber_task: asyncio.Task | None = None


async def _redis_subscriber() -> None:
    """Background task: read Redis pub/sub and broadcast to WebSocket clients."""
    import redis.asyncio as aioredis
    from app.config import get_settings
    import ssl as _ssl

    settings = get_settings()
    kwargs: dict = {"decode_responses": True}
    if "rediss://" in settings.redis_url:
        kwargs["ssl_cert_reqs"] = "none"

    while True:
        try:
            async with aioredis.from_url(settings.redis_url, **kwargs) as r:
                pubsub = r.pubsub()
                await pubsub.subscribe(*SUBSCRIBED_CHANNELS)
                logger.info(f"[WS] Subscribed to {len(SUBSCRIBED_CHANNELS)} Redis channels")
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        raw = message.get("data", "")
                        try:
                            payload = json.loads(raw)
                            # Normalise to { event, data, timestamp } shape
                            frame = json.dumps(payload)
                            await manager.broadcast(frame)
                        except Exception:
                            pass
        except asyncio.CancelledError:
            logger.info("[WS] Redis subscriber cancelled")
            return
        except Exception as exc:
            logger.error(f"[WS] Redis subscriber error: {exc} — retrying in 5s")
            await asyncio.sleep(5)


# ── Startup / shutdown lifecycle hooks ───────────────────────────────────────

async def start_redis_subscriber() -> None:
    global _subscriber_task
    if _subscriber_task is None or _subscriber_task.done():
        _subscriber_task = asyncio.create_task(_redis_subscriber())


async def stop_redis_subscriber() -> None:
    global _subscriber_task
    if _subscriber_task and not _subscriber_task.done():
        _subscriber_task.cancel()
        try:
            await _subscriber_task
        except asyncio.CancelledError:
            pass


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket) -> None:
    """
    Real-time event stream for the dashboard.
    Clients receive JSON frames for every platform event.
    """
    await manager.connect(websocket)
    # Send a welcome ping so the client knows it's connected
    try:
        await websocket.send_text(json.dumps({
            "event": "connected",
            "data": {"message": "BD Automator real-time feed active"},
        }))
        # Keep alive — wait for disconnect (client can send pings but we ignore them)
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # Send a keepalive ping
                await websocket.send_text(json.dumps({"event": "ping"}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug(f"[WS] Connection closed: {exc}")
    finally:
        manager.disconnect(websocket)
