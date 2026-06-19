from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from uuid import UUID
from typing import Optional

from app.database import get_db

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

@router.get("/summary")
async def analytics_summary(
    candidate_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Returns KPIs for the dashboard.
    Module 5 calls this on load and after WebSocket events.
    """
    # Build queries dynamically based on candidate_id
    base_filter = "WHERE candidate_id = :cid" if candidate_id else ""
    
    queries = {
        "total_applied": f"SELECT COUNT(*) FROM applications {base_filter}",
        "interviews_this_week": f"""
            SELECT COUNT(*) FROM interviews i
            JOIN applications a ON i.application_id = a.id
            {base_filter.replace('WHERE', 'WHERE a.')}
            AND i.scheduled_at > NOW() AND i.scheduled_at < NOW() + INTERVAL '7 days'
        """,
        "pending_in_queue": f"""
            SELECT COUNT(*) FROM applications {base_filter} AND status = 'QUEUED'
        """,
        # ... etc
    }
    
    results = {}
    for key, sql in queries.items():
        result = await db.execute(text(sql), {"cid": candidate_id} if candidate_id else {})
        results[key] = result.scalar()
    
    return results