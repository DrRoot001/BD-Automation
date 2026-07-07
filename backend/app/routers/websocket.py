"""WebSocket endpoint for real-time dashboard updates (Module 5).

Subscribes to Redis pub/sub channels and broadcasts events to all
connected clients as JSON frames.

Connect: ws://localhost:8000/ws/updates
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Set, Dict, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.user import User, UserRole
from app.routers.auth import validate_supabase_token

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

# ── Connection manager ────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        # Map websocket -> (user_id, role)
        self._active: Dict[WebSocket, tuple[str, str]] = {}

    async def connect(self, ws: WebSocket, user_id: str, role: str) -> None:
        await ws.accept()
        self._active[ws] = (user_id, role)
        logger.info(f"[WS] Client connected (user={user_id}) — total={len(self._active)}")

    def disconnect(self, ws: WebSocket) -> None:
        self._active.pop(ws, None)
        logger.info(f"[WS] Client disconnected — total={len(self._active)}")

    async def broadcast(self, message: str, candidate_owner_id: Optional[str] = None) -> None:
        """Broadcast message to connected clients.
        
        If candidate_owner_id is provided, only sends to that user OR to admins.
        If no candidate_owner_id is provided, sends to everyone.
        """
        dead: Set[WebSocket] = set()
        for ws, (uid, role) in list(self._active.items()):
            if role == UserRole.admin.value or not candidate_owner_id or uid == candidate_owner_id:
                try:
                    await ws.send_text(message)
                except Exception:
                    dead.add(ws)
        
        # Cleanup dead connections
        for ws in dead:
            self._active.pop(ws, None)

manager = ConnectionManager()

# ── Local Candidate Owner Cache ──────────────────────────────────────────────
# Avoids a DB query for every single event broadcast
_candidate_owners: Dict[str, str] = {}

async def _get_candidate_owner(candidate_id: str) -> Optional[str]:
    if candidate_id in _candidate_owners:
        return _candidate_owners[candidate_id]
        
    from app.models.candidate import Candidate
    from app.models.user import User
    import uuid
    try:
        cand_uuid = uuid.UUID(candidate_id) if isinstance(candidate_id, str) else candidate_id
        async with AsyncSessionLocal() as session:
            query = (
                select(User.supabase_user_id)
                .join(Candidate, Candidate.user_id == User.id)
                .where(Candidate.id == cand_uuid)
            )
            result = await session.execute(query)
            owner_id = result.scalar_one_or_none()
            if owner_id:
                owner_str = str(owner_id)
                _candidate_owners[candidate_id] = owner_str
                return owner_str
    except Exception as exc:
        logger.warning(f"[WS] Failed to fetch candidate owner: {exc}")
    return None

# ── Redis subscriber background task ─────────────────────────────────────────

SUBSCRIBED_CHANNELS = [
    "events:application.created",
    "events:application.status_changed",
    "events:application.submitted",
    "events:application.failed",
    "events:email.classified",
    "events:interview.detected",
    "events:job.discovered",
    "events:pipeline.progress",
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
                            frame = json.dumps(payload)
                            
                            # Scope by candidate owner if applicable
                            candidate_id = payload.get("data", {}).get("candidate_id")
                            owner_id = None
                            if candidate_id:
                                owner_id = await _get_candidate_owner(candidate_id)
                                
                            # Fallback: if candidate owner lookup failed but the event
                            # carries a bd_user_id directly (set by dynamic_apply tasks),
                            # use it to scope the event. This prevents pipeline.progress
                            # events from leaking to other BD users when the DB lookup
                            # fails or the cache hasn't been populated yet.
                            if owner_id is None:
                                bd_user_id = payload.get("data", {}).get("bd_user_id")
                                if bd_user_id:
                                    owner_id = bd_user_id
                                    
                            await manager.broadcast(frame, candidate_owner_id=owner_id)
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
async def websocket_updates(websocket: WebSocket, token: str = Query(None)) -> None:
    """
    Real-time event stream for the dashboard.
    Clients receive JSON frames for platform events they are authorised to see.
    """
    if not token:
        await websocket.close(code=1008)
        return
        
    try:
        payload = await validate_supabase_token(f"Bearer {token}")
        user_id = payload.get("id") or payload.get("sub")
        if not user_id:
            await websocket.close(code=1008)
            return
            
        async with AsyncSessionLocal() as session:
            query = select(User).where(User.supabase_user_id == user_id)
            result = await session.execute(query)
            user = result.scalars().first()
            if not user:
                from app.config import get_settings
                settings = get_settings()
                email = payload.get("email") or ""
                admin_id = settings.supabase_admin_user_id or ""
                is_admin = bool(admin_id and user_id == admin_id)
                role_value = UserRole.admin if is_admin else UserRole.bd_user
                user = User(supabase_user_id=user_id, email=email, role=role_value)
                session.add(user)
                await session.commit()
                await session.refresh(user)
            role = user.role.value if hasattr(user.role, 'value') else user.role
            
    except Exception as exc:
        logger.warning(f"[WS] Auth failed: {exc}")
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, user_id, role)
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
