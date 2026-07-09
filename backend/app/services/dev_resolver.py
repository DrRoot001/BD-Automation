import subprocess
import getpass
import logging
from typing import Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

async def auto_resolve_candidate_id(session: AsyncSession) -> Optional[str]:
    """Dynamically resolve the developer's candidate ID in the local/shared database.
    
    1. Try to find candidate by git config user.email
    2. Try to find candidate owned by the user account with git user.email
    3. Try to find candidate whose name contains git config user.name
    4. Try to find candidate whose name contains the OS username
    
    Returns the resolved candidate ID string or None if not resolvable.
    """
    # ── 1 & 2. Check Git Email ───────────────────────────────────────────────
    try:
        proc = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True, check=False)
        git_email = proc.stdout.strip()
        if git_email:
            # Query candidate directly by email
            res = await session.execute(
                text("SELECT id FROM candidates WHERE email = :email LIMIT 1"),
                {"email": git_email}
            )
            row = res.fetchone()
            if row:
                cid = str(row[0])
                logger.info(f"[DevResolver] Auto-resolved Candidate ID {cid} via direct Git email match: {git_email}")
                return cid
                
            # Query candidate owned by user with this email
            res = await session.execute(
                text("""
                    SELECT candidates.id 
                    FROM candidates 
                    JOIN users ON candidates.user_id = users.id 
                    WHERE users.email = :email 
                    LIMIT 1
                """),
                {"email": git_email}
            )
            row = res.fetchone()
            if row:
                cid = str(row[0])
                logger.info(f"[DevResolver] Auto-resolved Candidate ID {cid} via User owner Git email match: {git_email}")
                return cid
    except Exception as e:
        logger.debug(f"[DevResolver] Git email check skipped/failed: {e}")

    # ── 3. Check Git Name ────────────────────────────────────────────────────
    try:
        proc = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, check=False)
        git_name = proc.stdout.strip()
        if git_name:
            # Query candidate whose name matches git config user.name
            res = await session.execute(
                text("SELECT id FROM candidates WHERE name ILIKE :name LIMIT 1"),
                {"name": f"%{git_name}%"}
            )
            row = res.fetchone()
            if row:
                cid = str(row[0])
                logger.info(f"[DevResolver] Auto-resolved Candidate ID {cid} via Git name match: {git_name}")
                return cid
    except Exception as e:
        logger.debug(f"[DevResolver] Git name check skipped/failed: {e}")

    # ── 4. Check OS Username ──────────────────────────────────────────────────
    try:
        username = getpass.getuser()
        if username:
            # Query candidate whose name contains the OS username
            res = await session.execute(
                text("SELECT id FROM candidates WHERE name ILIKE :name LIMIT 1"),
                {"name": f"%{username}%"}
            )
            row = res.fetchone()
            if row:
                cid = str(row[0])
                logger.info(f"[DevResolver] Auto-resolved Candidate ID {cid} via OS username match: {username}")
                return cid
    except Exception as e:
        logger.debug(f"[DevResolver] OS username check skipped/failed: {e}")

    logger.debug("[DevResolver] Could not auto-resolve candidate ID")
    return None
