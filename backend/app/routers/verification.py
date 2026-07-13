"""Verification endpoints for the browser-extension co-pilot.

Thin HTTP wrappers over the Module 4 Gmail code-catcher so the Chrome
extension (bd-indeed-extension/) can auto-fill ATS email-verification codes
and sign in to job portals with the candidate's stored credentials. The main
/api/candidates responses strip `password`; /portal-credentials is the
deliberate, auth-scoped exception for the extension's portal-login flow.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.candidate import Candidate
from app.models.user import User, UserRole
from app.routers.auth import get_current_user

router = APIRouter(prefix="/api/verification", tags=["verification"])


async def _get_scoped_candidate(
    candidate_id: str, db: AsyncSession, current_user: User
) -> Candidate:
    try:
        cid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="candidate_id is not a valid UUID")
    candidate = (
        await db.execute(select(Candidate).where(Candidate.id == cid))
    ).scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if current_user.role != UserRole.admin and candidate.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your candidate")
    return candidate


@router.get("/code/{candidate_id}")
async def get_verification_code(
    candidate_id: str,
    after_epoch: int,
    sender_hint: str = "",
    timeout_s: float = 5.0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Newest ATS verification code from the candidate's Gmail, or null.

    Wraps browser_automation.verification.code_fetcher. `timeout_s` is capped
    so a poll can't hold an HTTP worker hostage — the extension polls this
    endpoint repeatedly rather than long-polling once.
    """
    await _get_scoped_candidate(candidate_id, db, current_user)
    from app.browser_automation.verification.code_fetcher import fetch_verification_code

    code = await fetch_verification_code(
        candidate_id,
        after_epoch,
        timeout_s=min(max(timeout_s, 1.0), 25.0),
        sender_hint=sender_hint,
    )
    return {"code": code}


@router.get("/portal-credentials/{candidate_id}")
async def get_portal_credentials(
    candidate_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Candidate's job-portal login credentials for the extension.

    Portal accounts are provisioned against the candidate's Gmail when present,
    falling back to the primary email — same precedence as
    adapters/session_utils.load_candidate_credentials.
    """
    candidate = await _get_scoped_candidate(candidate_id, db, current_user)
    gmail = (candidate.gmail or "").strip()
    email = (candidate.email or "").strip()
    return {
        "login_email": gmail or email,
        "gmail": gmail,
        "password": (candidate.password or "").strip(),
    }
