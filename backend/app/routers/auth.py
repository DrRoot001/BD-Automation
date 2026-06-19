from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from datetime import datetime, timedelta
from jose import JWTError, jwt

from app.database import get_db
from app.config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

settings = get_settings()

class LoginRequest(BaseModel):
    email: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

def create_access_token(data: dict, expires_delta: timedelta | None = None):
    """Create JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.secret_key, algorithm="HS256")
    return encoded_jwt

@router.post("/login", response_model=TokenResponse)
async def login(credentials: LoginRequest, db: AsyncSession = Depends(get_db)):
    """
    Authenticate candidate and return JWT token.
    """
    # TODO: Verify password against database
    # For now, this is a placeholder implementation
    if not credentials.email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token = create_access_token(data={"sub": credentials.email})
    return {"access_token": access_token, "token_type": "bearer"}

@router.post("/google")
async def google_auth(code: str, db: AsyncSession = Depends(get_db)):
    """
    Exchange OAuth code for tokens, store refresh token for Gmail API access.
    Module 5 will use the stored tokens to poll Gmail.
    """
    # httpx call to Google token endpoint
    # Store tokens in candidates table or a separate oauth_tokens table
    # Return access_token for immediate API use
    pass