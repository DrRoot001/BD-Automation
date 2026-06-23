from fastapi import APIRouter, Depends, HTTPException, Header, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from httpx import AsyncClient
from app.database import get_db
from app.config import get_settings
from sqlalchemy import select

from app.models.user import User
from pydantic import EmailStr
from fastapi import Body, Path

router = APIRouter(prefix="/api/auth", tags=["auth"])

settings = get_settings()


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


async def validate_supabase_token(authorization: str | None = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing auth token")
    token = authorization.split(" ", 1)[1]

    async with AsyncClient() as client:
        resp = await client.get(
            f"{settings.supabase_url}/auth/v1/user",
            headers={
                "apikey": settings.supabase_service_role_key or settings.supabase_anon_key,
                "Authorization": f"Bearer {token}",
            },
            timeout=10.0,
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Supabase token")

    return resp.json()


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
        role_value = "admin" if is_admin else "bd_user"
        user = User(supabase_user_id=supabase_user_id, email=email or "", role=role_value)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    else:
        # if env says this supabase id should be admin but DB record isn't, update it
        if is_admin and getattr(user, "role", None) != "admin":
            user.role = "admin"
            db.add(user)
            await db.commit()
            await db.refresh(user)

    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "role": current_user.role,
        "full_name": current_user.full_name,
    }


class CreateBdUserRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None


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
                "user_metadata": {"full_name": payload.full_name} if payload.full_name else {},
            },
            timeout=10.0,
        )

    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=resp.status_code, detail=f"Supabase error: {resp.text}")

    supabase_user = resp.json()

    # create local mapping
    user = User(supabase_user_id=supabase_user.get("id"), email=supabase_user.get("email") or payload.email, full_name=payload.full_name or "", role="bd_user")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return {"id": str(user.id), "supabase_user_id": user.supabase_user_id, "email": user.email, "role": user.role}


@router.get("/admin/users")
async def list_users(current_admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    query = select(User)
    result = await db.execute(query)
    users = result.scalars().all()
    return [
        {"id": str(u.id), "supabase_user_id": u.supabase_user_id, "email": u.email, "role": u.role, "full_name": u.full_name}
        for u in users
    ]


@router.patch("/admin/users/{user_id}/role")
async def update_user_role(
    user_id: int = Path(..., description="Local user ID"),
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

    user.role = payload.role
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return {"id": str(user.id), "role": user.role}
