"""Dashboard API — KPIs, applications list, interviews, analytics, activity feed."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ── Response schemas ──────────────────────────────────────────────────────────

class DashboardKPIs(BaseModel):
    total_applied: int = 0
    applied_today: int = 0
    interviews_this_week: int = 0
    success_rate: float = 0.0
    pending_in_queue: int = 0
    total_rejected: int = 0
    total_offers: int = 0


class ApplicationSummary(BaseModel):
    application_id: str
    job_id: str
    job_title: str
    company: str
    platform: str
    status: str
    fit_score: Optional[float] = None
    ats_score: Optional[float] = None
    submitted_at: Optional[datetime] = None
    created_at: datetime
    error_message: Optional[str] = None
    resume_url: Optional[str] = None
    job_url: Optional[str] = None


class InterviewSummary(BaseModel):
    interview_id: str
    company: str
    position: str
    round: int
    type: str
    scheduled_at: Optional[datetime] = None
    meeting_url: Optional[str] = None
    application_id: str


class ConversionFunnel(BaseModel):
    total_applied: int = 0
    total_confirmed: int = 0
    total_r1: int = 0
    total_r2: int = 0
    total_offers: int = 0
    total_rejected: int = 0
    apply_to_r1_rate: float = 0.0
    r1_to_r2_rate: float = 0.0
    r2_to_offer_rate: float = 0.0


class PlatformStats(BaseModel):
    platform: str
    applications: int
    interviews: int
    success_rate: float


class DailyCount(BaseModel):
    date: str
    count: int


class AnalyticsData(BaseModel):
    conversion_funnel: ConversionFunnel
    per_platform_stats: List[PlatformStats]
    daily_applications: List[DailyCount]
    avg_time_to_response_hours: Optional[float] = None


class ActivityEvent(BaseModel):
    event_type: str
    timestamp: datetime
    summary: str
    application_id: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 1) if denominator else 0.0


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/kpis", response_model=DashboardKPIs)
async def get_kpis(
    candidate_id: Optional[UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Summary KPI cards for the dashboard header."""
    cid_filter = "AND a.candidate_id = :cid" if candidate_id else ""
    params = {"cid": str(candidate_id)} if candidate_id else {}

    rows = await db.execute(text(f"""
        SELECT
          COUNT(*) FILTER (WHERE a.status NOT IN ('FOUND','QUEUED'))          AS total_applied,
          COUNT(*) FILTER (WHERE DATE(a.created_at) = CURRENT_DATE)           AS applied_today,
          COUNT(*) FILTER (WHERE a.status = 'QUEUED')                         AS pending_in_queue,
          COUNT(*) FILTER (WHERE a.status = 'REJECTED')                       AS total_rejected,
          COUNT(*) FILTER (WHERE a.status = 'OFFER')                          AS total_offers,
          COUNT(*) FILTER (WHERE a.status IN ('INTERVIEW_R1','INTERVIEW_R2'))  AS total_interviews
        FROM applications a
        WHERE 1=1 {cid_filter}
    """), params)
    r = rows.fetchone()

    # Interviews this week from the interviews table
    iw_rows = await db.execute(text(f"""
        SELECT COUNT(*) FROM interviews i
        JOIN applications a ON i.application_id = a.id
        WHERE i.scheduled_at >= NOW()
          AND i.scheduled_at < NOW() + INTERVAL '7 days'
          {cid_filter.replace('AND a.', 'AND a.')}
    """), params)
    interviews_this_week = iw_rows.scalar() or 0

    total_applied = r.total_applied or 0
    total_interviews = r.total_interviews or 0
    success_rate = _rate(total_interviews, total_applied)

    return DashboardKPIs(
        total_applied=total_applied,
        applied_today=r.applied_today or 0,
        interviews_this_week=int(interviews_this_week),
        success_rate=success_rate,
        pending_in_queue=r.pending_in_queue or 0,
        total_rejected=r.total_rejected or 0,
        total_offers=r.total_offers or 0,
    )


@router.get("/applications", response_model=List[ApplicationSummary])
async def get_applications(
    candidate_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    """Paginated list of applications with job metadata."""
    filters = []
    params: dict = {"limit": limit, "offset": offset}
    if candidate_id:
        filters.append("a.candidate_id = :cid")
        params["cid"] = str(candidate_id)
    if status:
        filters.append("a.status = :status")
        params["status"] = status
    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    rows = await db.execute(text(f"""
        SELECT a.id, a.job_id, j.title, j.company, j.source AS platform,
               a.status, a.fit_score, a.ats_score,
               a.submitted_at, a.created_at, a.error_message,
               r.file_url AS resume_url,
               COALESCE(j.canonical_url, j.source_url) AS job_url
        FROM applications a
        JOIN jobs j ON a.job_id = j.id
        LEFT JOIN resumes r ON a.resume_id = r.id
        {where}
        ORDER BY a.created_at DESC
        LIMIT :limit OFFSET :offset
    """), params)

    return [
        ApplicationSummary(
            application_id=str(r.id),
            job_id=str(r.job_id),
            job_title=r.title or "",
            company=r.company or "",
            platform=r.platform or "",
            status=r.status or "",
            fit_score=float(r.fit_score) if r.fit_score is not None else None,
            ats_score=float(r.ats_score) if r.ats_score is not None else None,
            submitted_at=r.submitted_at,
            created_at=r.created_at,
            error_message=r.error_message,
            resume_url=r.resume_url,
            job_url=r.job_url,
        )
        for r in rows.fetchall()
    ]


@router.get("/interviews", response_model=List[InterviewSummary])
async def get_interviews(
    candidate_id: Optional[UUID] = Query(None),
    upcoming_only: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    """List of upcoming (or all) interview records."""
    filters = ["1=1"]
    params: dict = {}
    if candidate_id:
        filters.append("a.candidate_id = :cid")
        params["cid"] = str(candidate_id)
    if upcoming_only:
        filters.append("(i.scheduled_at IS NULL OR i.scheduled_at >= NOW())")

    rows = await db.execute(text(f"""
        SELECT i.id, j.company, j.title AS position, i.round, i.type,
               i.scheduled_at, i.meeting_url, a.id AS application_id
        FROM interviews i
        JOIN applications a ON i.application_id = a.id
        JOIN jobs j ON a.job_id = j.id
        WHERE {' AND '.join(filters)}
        ORDER BY i.scheduled_at ASC NULLS LAST
        LIMIT 50
    """), params)

    return [
        InterviewSummary(
            interview_id=str(r.id),
            company=r.company or "",
            position=r.position or "",
            round=r.round or 1,
            type=r.type or "phone",
            scheduled_at=r.scheduled_at,
            meeting_url=r.meeting_url,
            application_id=str(r.application_id),
        )
        for r in rows.fetchall()
    ]


@router.get("/analytics", response_model=AnalyticsData)
async def get_analytics(
    candidate_id: Optional[UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Conversion funnel, per-platform stats, and daily application counts."""
    cid_filter = "AND a.candidate_id = :cid" if candidate_id else ""
    params = {"cid": str(candidate_id)} if candidate_id else {}

    # Conversion funnel
    funnel_rows = await db.execute(text(f"""
        SELECT
          COUNT(*) FILTER (WHERE a.status NOT IN ('FOUND','QUEUED'))           AS total_applied,
          COUNT(*) FILTER (WHERE a.status = 'CONFIRMED')                       AS total_confirmed,
          COUNT(*) FILTER (WHERE a.status IN ('INTERVIEW_R1','ASSESSMENT'))     AS total_r1,
          COUNT(*) FILTER (WHERE a.status = 'INTERVIEW_R2')                    AS total_r2,
          COUNT(*) FILTER (WHERE a.status = 'OFFER')                           AS total_offers,
          COUNT(*) FILTER (WHERE a.status = 'REJECTED')                        AS total_rejected
        FROM applications a WHERE 1=1 {cid_filter}
    """), params)
    f = funnel_rows.fetchone()
    total_applied = f.total_applied or 0
    total_r1 = f.total_r1 or 0
    total_r2 = f.total_r2 or 0

    funnel = ConversionFunnel(
        total_applied=total_applied,
        total_confirmed=f.total_confirmed or 0,
        total_r1=total_r1,
        total_r2=total_r2,
        total_offers=f.total_offers or 0,
        total_rejected=f.total_rejected or 0,
        apply_to_r1_rate=_rate(total_r1, total_applied),
        r1_to_r2_rate=_rate(total_r2, total_r1),
        r2_to_offer_rate=_rate(f.total_offers or 0, total_r2),
    )

    # Per-platform stats
    platform_rows = await db.execute(text(f"""
        SELECT j.source AS platform,
               COUNT(a.id) AS applications,
               COUNT(i.id) AS interviews
        FROM applications a
        JOIN jobs j ON a.job_id = j.id
        LEFT JOIN interviews i ON i.application_id = a.id
        WHERE 1=1 {cid_filter}
        GROUP BY j.source
        ORDER BY applications DESC
        LIMIT 10
    """), params)
    per_platform = [
        PlatformStats(
            platform=r.platform or "unknown",
            applications=r.applications or 0,
            interviews=r.interviews or 0,
            success_rate=_rate(r.interviews or 0, r.applications or 0),
        )
        for r in platform_rows.fetchall()
    ]

    # Daily applications — last 30 days
    daily_rows = await db.execute(text(f"""
        SELECT DATE(a.created_at) AS day, COUNT(*) AS cnt
        FROM applications a
        WHERE a.created_at >= NOW() - INTERVAL '30 days'
          {cid_filter}
        GROUP BY day ORDER BY day ASC
    """), params)
    daily = [DailyCount(date=str(r.day), count=r.cnt) for r in daily_rows.fetchall()]

    # Avg time to response
    avg_row = await db.execute(text(f"""
        SELECT AVG(EXTRACT(EPOCH FROM (e.received_at - a.submitted_at)) / 3600)
        FROM emails e
        JOIN applications a ON e.application_id = a.id
        WHERE e.classification != 'UNKNOWN'
          AND a.submitted_at IS NOT NULL
          AND e.received_at > a.submitted_at
          {cid_filter.replace('AND a.', 'AND a.')}
    """), params)
    avg_hours = avg_row.scalar()

    return AnalyticsData(
        conversion_funnel=funnel,
        per_platform_stats=per_platform,
        daily_applications=daily,
        avg_time_to_response_hours=float(avg_hours) if avg_hours else None,
    )


@router.get("/activity-feed", response_model=List[ActivityEvent])
async def get_activity_feed(
    candidate_id: Optional[UUID] = Query(None),
    limit: int = Query(20, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Recent events — status changes, emails classified, interviews detected."""
    cid_filter = "AND a.candidate_id = :cid" if candidate_id else ""
    params: dict = {"limit": limit}
    if candidate_id:
        params["cid"] = str(candidate_id)

    rows = await db.execute(text(f"""
        SELECT 'application.status_changed' AS event_type,
               ah.created_at AS ts,
               CONCAT(j.company, ' — ', ah.to_status) AS summary,
               a.id::text AS application_id
        FROM application_history ah
        JOIN applications a ON ah.application_id = a.id
        JOIN jobs j ON a.job_id = j.id
        WHERE 1=1 {cid_filter}
        UNION ALL
        SELECT 'email.classified' AS event_type,
               e.processed_at AS ts,
               CONCAT(e.from_addr, ': ', e.classification) AS summary,
               e.application_id::text
        FROM emails e
        JOIN applications a ON e.application_id = a.id
        WHERE 1=1 {cid_filter}
        ORDER BY ts DESC
        LIMIT :limit
    """), params)

    return [
        ActivityEvent(
            event_type=r.event_type,
            timestamp=r.ts or datetime.utcnow(),
            summary=r.summary or "",
            application_id=r.application_id,
        )
        for r in rows.fetchall()
    ]
