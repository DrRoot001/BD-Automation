from fastapi import APIRouter, Depends, HTTPException, Header, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from httpx import AsyncClient
import httpx
import asyncio
import json
from app.database import get_db
from app.config import get_settings
from app.redis_client import redis_client
from sqlalchemy import select

from app.models.user import User, UserRole
from pydantic import EmailStr
from fastapi import Body, Path

router = APIRouter(prefix="/api/auth", tags=["auth"])

settings = get_settings()

# ── HTTP connection pool ────────────────────────────────────────────────────
# Shared across requests — avoids opening a new connection per token validation.
_http_client: AsyncClient | None = None

def get_http_client() -> AsyncClient:
    global _http_client
    if _http_client is None:
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
        _http_client = AsyncClient(limits=limits, timeout=10.0)
    return _http_client


# ── Redis-backed token cache ─────────────────────────────────────────────────
# Storing validated token payloads in Redis (TTL 120 s) so every uvicorn worker
# and Celery process shares the same cache — no per-process in-memory dict.
_TOKEN_CACHE_PREFIX = "auth:token:"
_TOKEN_CACHE_TTL = 120  # seconds


async def _get_cached_payload(token: str) -> dict | None:
    """Return cached Supabase user payload, or None on miss/error."""
    try:
        raw = await redis_client.get(f"{_TOKEN_CACHE_PREFIX}{token[:40]}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _cache_payload(token: str, payload: dict) -> None:
    """Store validated payload in Redis with a short TTL."""
    try:
        await redis_client.setex(
            f"{_TOKEN_CACHE_PREFIX}{token[:40]}",
            _TOKEN_CACHE_TTL,
            json.dumps(payload),
        )
    except Exception:
        pass  # Cache miss is acceptable — next request will re-validate


async def _evict_cached_token(token: str) -> None:
    """Remove a token from the cache (on 401 from Supabase)."""
    try:
        await redis_client.delete(f"{_TOKEN_CACHE_PREFIX}{token[:40]}")
    except Exception:
        pass


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class MergeUsersRequest(BaseModel):
    source_user_id: str
    target_user_id: str



@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest):
    if not settings.supabase_url or not settings.supabase_anon_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Supabase is not configured on the backend"
        )

    client = get_http_client()
    resp = await client.post(
        f"{settings.supabase_url}/auth/v1/token?grant_type=password",
        headers={
            "apikey": settings.supabase_anon_key,
            "Content-Type": "application/json",
        },
        json={
            "email": payload.email,
            "password": payload.password,
        },
    )

    if resp.status_code != 200:
        try:
            error_data = resp.json()
            error_msg = error_data.get("error_description") or error_data.get("msg") or "Invalid credentials"
        except Exception:
            error_msg = "Invalid credentials"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_msg
        )

    data = resp.json()
    access_token = data.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No access token returned from authentication provider"
        )

    return {"access_token": access_token, "token_type": "bearer"}


async def validate_supabase_token(authorization: str | None = Header(None)) -> dict:
    """Validate a Supabase Bearer token.

    1. Check Redis cache (TTL 120 s) — avoids a Supabase round-trip on every request.
    2. On cache miss, call Supabase /auth/v1/user with retry logic.
    3. On success, store payload in Redis for subsequent requests.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing auth token")
    token = authorization.split(" ", 1)[1]

    # ── Cache hit ────────────────────────────────────────────────────────────
    cached = await _get_cached_payload(token)
    if cached is not None:
        return cached

    # ── Cache miss — validate with Supabase ──────────────────────────────────
    client = get_http_client()
    max_retries = 2
    resp = None
    for attempt in range(max_retries):
        try:
            resp = await client.get(
                f"{settings.supabase_url}/auth/v1/user",
                headers={
                    "apikey": settings.supabase_service_role_key or settings.supabase_anon_key,
                    "Authorization": f"Bearer {token}",
                },
            )
            if resp.status_code == 200:
                break
        except (httpx.ConnectTimeout, httpx.ConnectError):
            if attempt == max_retries - 1:
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail="Connection to authentication provider timed out. Please try again.",
                )
            await asyncio.sleep(0.5)
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to communicate with authentication provider: {str(e)}",
            )

    if not resp or resp.status_code != 200:
        await _evict_cached_token(token)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Supabase token")

    payload = resp.json()
    # Store in Redis — shared across all workers
    await _cache_payload(token, payload)
    return payload


async def get_current_user(token_data: dict = Depends(validate_supabase_token), db: AsyncSession = Depends(get_db)) -> User:
    supabase_user_id = token_data.get("id")
    email = token_data.get("email")

    if not supabase_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    query = select(User).where(User.supabase_user_id == supabase_user_id)
    result = await db.execute(query)
    user = result.scalars().first()

    # auto-promote a configured supabase user id to admin
    admin_id = settings.supabase_admin_user_id or ""
    is_admin = bool(admin_id and supabase_user_id == admin_id)

    if not user:
        role_value = UserRole.admin if is_admin else UserRole.bd_user
        user = User(supabase_user_id=supabase_user_id, email=email or "", role=role_value)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    else:
        # if env says this supabase id should be admin but DB record isn't, update it
        if is_admin and getattr(user, "role", None) != UserRole.admin:
            user.role = UserRole.admin
            db.add(user)
            await db.commit()
            await db.refresh(user)

    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "role": current_user.role.value if hasattr(current_user.role, 'value') else current_user.role,
        "full_name": current_user.full_name,
    }


class CreateBdUserRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None
    name: str | None = None
    role: str | None = "bd_user"


class UpdateRoleRequest(BaseModel):
    role: str


@router.post("/admin/create_bd_user")
async def create_bd_user(
    payload: CreateBdUserRequest,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if not settings.supabase_service_role_key:
        raise HTTPException(status_code=500, detail="Supabase service role key not configured")

    user_name = payload.name or payload.full_name or ""

    # create user in Supabase via Admin API
    async with AsyncClient() as client:
        resp = await client.post(
            f"{settings.supabase_url}/auth/v1/admin/users",
            headers={
                "apikey": settings.supabase_service_role_key,
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
                "Content-Type": "application/json",
            },
            json={
                "email": payload.email,
                "password": payload.password,
                "email_confirm": True,
                "user_metadata": {"full_name": user_name} if user_name else {},
            },
            timeout=10.0,
        )

    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=resp.status_code, detail=f"Supabase error: {resp.text}")

    supabase_user = resp.json()

    # create local mapping
    user_role = UserRole.admin if payload.role == "admin" else UserRole.bd_user
    user = User(
        supabase_user_id=supabase_user.get("id"),
        email=supabase_user.get("email") or payload.email,
        full_name=user_name,
        role=user_role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return {
        "id": str(user.id),
        "supabase_user_id": user.supabase_user_id,
        "email": user.email,
        "role": user.role,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.get("/admin/users/count")
async def get_users_count(
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import func
    result = await db.execute(select(func.count(User.id)))
    return {"total_count": result.scalar()}


@router.get("/admin/users")
async def list_users(
    skip: int = 0,
    limit: int = 100,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db)
):
    query = select(User).offset(skip).limit(limit)
    result = await db.execute(query)
    users = result.scalars().all()
    return [
        {
            "id": str(u.id),
            "supabase_user_id": u.supabase_user_id,
            "email": u.email,
            "role": u.role.value if hasattr(u.role, 'value') else u.role,
            "full_name": u.full_name,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


@router.patch("/admin/users/{user_id}/role")
async def update_user_role(
    user_id: str = Path(..., description="Local user ID"),
    payload: UpdateRoleRequest = Body(...),
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(User).where(User.id == user_id)
    result = await db.execute(query)
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if payload.role not in ("admin", "bd_user"):
        raise HTTPException(status_code=400, detail="Invalid role")

    user.role = UserRole(payload.role)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return {"id": str(user.id), "role": user.role}


class UpdateUserRequest(BaseModel):
    email: EmailStr | None = None
    full_name: str | None = None
    name: str | None = None
    role: str | None = None


@router.patch("/admin/users/{user_id}")
async def update_user(
    user_id: str = Path(..., description="Local user ID (UUID)"),
    payload: UpdateUserRequest = Body(...),
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(User).where(User.id == user_id)
    result = await db.execute(query)
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    new_name = payload.name or payload.full_name

    # 1. Update in Supabase via Admin API
    update_data = {}
    if payload.email:
        update_data["email"] = payload.email
        update_data["email_confirm"] = True
    if new_name is not None:
        update_data["user_metadata"] = {"full_name": new_name}

    if update_data:
        async with AsyncClient() as client:
            resp = await client.put(
                f"{settings.supabase_url}/auth/v1/admin/users/{user.supabase_user_id}",
                headers={
                    "apikey": settings.supabase_service_role_key,
                    "Authorization": f"Bearer {settings.supabase_service_role_key}",
                    "Content-Type": "application/json",
                },
                json=update_data,
                timeout=10.0,
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail=f"Supabase update error: {resp.text}")

    # 2. Update local DB mapping
    if payload.email:
        user.email = payload.email
    if new_name is not None:
        user.full_name = new_name
    if payload.role:
        if payload.role not in ("admin", "bd_user"):
            raise HTTPException(status_code=400, detail="Invalid role")
        user.role = UserRole(payload.role)

    db.add(user)
    await db.commit()
    await db.refresh(user)

    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role.value if hasattr(user.role, 'value') else user.role,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


class UpdatePasswordRequest(BaseModel):
    password: str


@router.put("/admin/users/{user_id}/password")
async def update_user_password(
    user_id: str = Path(..., description="Local user ID (UUID)"),
    payload: UpdatePasswordRequest = Body(...),
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(User).where(User.id == user_id)
    result = await db.execute(query)
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Update password in Supabase via Admin API
    async with AsyncClient() as client:
        resp = await client.put(
            f"{settings.supabase_url}/auth/v1/admin/users/{user.supabase_user_id}",
            headers={
                "apikey": settings.supabase_service_role_key,
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
                "Content-Type": "application/json",
            },
            json={
                "password": payload.password,
            },
            timeout=10.0,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"Supabase password update error: {resp.text}")

    return {"message": "Password updated successfully"}


@router.delete("/admin/users/{user_id}")
async def delete_user(
    user_id: str = Path(..., description="Local user ID (UUID)"),
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(User).where(User.id == user_id)
    result = await db.execute(query)
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Do not allow deleting yourself!
    if user.supabase_user_id == current_admin.supabase_user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own admin account")

    # 1. Delete from Supabase via Admin API
    async with AsyncClient() as client:
        resp = await client.delete(
            f"{settings.supabase_url}/auth/v1/admin/users/{user.supabase_user_id}",
            headers={
                "apikey": settings.supabase_service_role_key,
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
            },
            timeout=10.0,
        )
        # 404 from Supabase is acceptable in case they are already deleted there
        if resp.status_code not in (200, 404):
            raise HTTPException(status_code=resp.status_code, detail=f"Supabase delete error: {resp.text}")

    # 2. Delete local mapping
    await db.delete(user)
    await db.commit()

    return {"message": "User deleted successfully"}


@router.post("/admin/users/merge")
async def merge_users(
    payload: MergeUsersRequest,
    current_admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    import uuid
    from sqlalchemy import update
    from app.models.candidate import Candidate

    source_id = payload.source_user_id
    target_id = payload.target_user_id
    
    if source_id == target_id:
        raise HTTPException(status_code=400, detail="Cannot merge a user into themselves")
        
    # Do not allow merging yourself (as source)!
    if source_id == str(current_admin.id):
        raise HTTPException(status_code=400, detail="Cannot merge and delete your own admin account")

    try:
        source_uuid = uuid.UUID(source_id) if isinstance(source_id, str) else source_id
        target_uuid = uuid.UUID(target_id) if isinstance(target_id, str) else target_id
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID format")

    # 1. Fetch source and target users
    source_user = await db.get(User, source_uuid)
    target_user = await db.get(User, target_uuid)
    
    if not source_user:
        raise HTTPException(status_code=404, detail="Source user not found")
    if not target_user:
        raise HTTPException(status_code=404, detail="Target user not found")
        
    # 2. Reassign all candidates belonging to the source user to the target user
    await db.execute(
        update(Candidate)
        .where(Candidate.user_id == source_user.id)
        .values(user_id=target_user.id)
    )
    
    # 3. Delete source user from Supabase via Admin API
    async with AsyncClient() as client:
        resp = await client.delete(
            f"{settings.supabase_url}/auth/v1/admin/users/{source_user.supabase_user_id}",
            headers={
                "apikey": settings.supabase_service_role_key,
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
            },
            timeout=10.0,
        )
        if resp.status_code not in (200, 404):
            raise HTTPException(status_code=resp.status_code, detail=f"Supabase delete error: {resp.text}")

    # 4. Delete source user local mapping
    await db.delete(source_user)
    await db.commit()
    
    return {"message": "Users merged successfully. Candidates re-assigned to target user."}
