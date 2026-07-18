"""Dashboard API — KPIs, applications list, interviews, analytics, activity feed."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User, UserRole
from app.routers.auth import get_current_user, require_admin

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
    candidate_id: Optional[str] = None
    resume_id: Optional[str] = None
    job_title: str
    company: str
    platform: str
    status: str
    paused: bool = False
    fit_score: Optional[float] = None
    ats_score: Optional[float] = None
    # ATS score of the base resume vs the JD (before tailoring) and of the
    # tailored resume (after). ats_score_after is only present once the app has
    # been tailored (QUEUED+); it comes from the QUEUED transition's history.
    ats_score_before: Optional[float] = None
    ats_score_after: Optional[float] = None
    # True when the application used the candidate's base resume (no tailoring,
    # e.g. ANALYZED apps below the gate); False when a tailored resume was used.
    resume_is_base: Optional[bool] = None
    submitted_at: Optional[datetime] = None
    created_at: datetime
    error_message: Optional[str] = None
    failure_reason: Optional[str] = None
    resume_url: Optional[str] = None
    cover_letter_url: Optional[str] = None
    job_url: Optional[str] = None
    candidate_name: Optional[str] = None
    bd_user_name: Optional[str] = None
    bd_user_email: Optional[str] = None

    @field_validator('job_url', mode='before', check_fields=False)
    @classmethod
    def clean_job_url(cls, v):
        if v is None:
            return None
        if not isinstance(v, str):
            return v
        cleaned = v.strip()
        if 'not found' in cleaned.lower() or cleaned == '—' or cleaned == '':
            return None
        return cleaned


class InterviewSummary(BaseModel):
    interview_id: str
    company: str
    position: str
    round: int
    type: str
    scheduled_at: Optional[datetime] = None
    meeting_url: Optional[str] = None
    application_id: Optional[str] = None
    received_at: Optional[datetime] = None
    status: Optional[str] = None


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


class QueueDepth(BaseModel):
    name: str
    depth: int


class WorkerInfo(BaseModel):
    name: str
    active_tasks: int


class OpsStats(BaseModel):
    queues: List[QueueDepth]
    workers: List[WorkerInfo]
    applications_in_flight: int
    applications_today: int
    submitted_today: int


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 1) if denominator else 0.0


async def get_dashboard_candidate_filter(db: AsyncSession, current_user: User, candidate_id: Optional[UUID]) -> tuple[str, dict]:
    """Generates the appropriate SQL filter segment and parameter mapping based on user permissions."""
    if current_user.role == UserRole.admin:
        if candidate_id:
            return "AND a.candidate_id = :cid", {"cid": str(candidate_id)}
        return "", {}
    else:
        # Scope via subquery instead of pre-fetching owned ids — saves one DB
        # round-trip per request. A candidate_id the user doesn't own yields
        # zero rows (cid must also be in the owned set), same as before.
        owned = "a.candidate_id IN (SELECT id FROM candidates WHERE user_id = :uid)"
        if candidate_id:
            return f"AND a.candidate_id = :cid AND {owned}", {"cid": str(candidate_id), "uid": current_user.id}
        return f"AND {owned}", {"uid": current_user.id}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/kpis", response_model=DashboardKPIs)
async def get_kpis(
    candidate_id: Optional[UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Summary KPI cards for the dashboard header."""
    cid_filter, params = await get_dashboard_candidate_filter(db, current_user, candidate_id)

    # Single round-trip: interviews-this-week runs as a scalar subquery whose
    # inner "a" alias shadows the outer one, so the same cid_filter applies.
    rows = await db.execute(text(f"""
        SELECT
          COUNT(*) FILTER (WHERE a.status NOT IN ('FOUND','QUEUED'))          AS total_applied,
          COUNT(*) FILTER (WHERE DATE(a.created_at) = CURRENT_DATE)           AS applied_today,
          COUNT(*) FILTER (WHERE a.status = 'QUEUED')                         AS pending_in_queue,
          COUNT(*) FILTER (WHERE a.status = 'REJECTED')                       AS total_rejected,
          COUNT(*) FILTER (WHERE a.status = 'OFFER')                          AS total_offers,
          COUNT(*) FILTER (WHERE a.status IN ('INTERVIEW_R1','INTERVIEW_R2'))  AS total_interviews,
          (SELECT COUNT(*) FROM interviews i
             JOIN applications a ON i.application_id = a.id
            WHERE i.scheduled_at >= NOW()
              AND i.scheduled_at < NOW() + INTERVAL '7 days'
              {cid_filter})                                                   AS interviews_this_week
        FROM applications a
        WHERE 1=1 {cid_filter}
    """), params)
    r = rows.fetchone()
    interviews_this_week = r.interviews_this_week or 0

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
    current_user: User = Depends(get_current_user),
):
    """Paginated list of applications with job metadata."""
    cid_filter, cid_params = await get_dashboard_candidate_filter(db, current_user, candidate_id)

    filters = ["1=1"]
    if cid_filter:
        filters.append(cid_filter.lstrip("AND "))
    if status:
        filters.append("a.status = :status")

    params = {"limit": limit, "offset": offset, "status": status, **cid_params}
    where = "WHERE " + " AND ".join(filters)

    rows = await db.execute(text(f"""
        SELECT a.id, a.job_id, a.candidate_id, a.resume_id,
               j.title, j.company, j.source AS platform,
               a.status, a.paused, a.fit_score, a.ats_score,
               a.submitted_at, a.created_at, a.error_message, a.failure_reason,
               r.file_url AS resume_url,
               r.is_base AS resume_is_base,
               a.cover_letter_url,
               COALESCE(
                 CASE WHEN j.canonical_url IS NOT NULL AND LOWER(j.canonical_url) NOT IN ('(not found)', 'link not found', '') AND LOWER(j.canonical_url) NOT LIKE '%not found%' THEN j.canonical_url END,
                 CASE WHEN j.source_url IS NOT NULL AND LOWER(j.source_url) NOT IN ('(not found)', 'link not found', '') AND LOWER(j.source_url) NOT LIKE '%not found%' THEN j.source_url END
               ) AS job_url,
               c.name AS candidate_name,
               u.full_name AS bd_user_name,
               u.email AS bd_user_email,
               -- Tailoring before/after ATS scores are recorded in the QUEUED
               -- transition's history metadata (the app row only keeps the
               -- pre-tailoring ats_score). Pull the most recent one.
               (tail.meta_data ->> 'ats_score_before')::numeric AS ats_score_before,
               (tail.meta_data ->> 'ats_score_after')::numeric  AS ats_score_after
        FROM applications a
        JOIN jobs j ON a.job_id = j.id
        JOIN candidates c ON a.candidate_id = c.id
        LEFT JOIN users u ON c.user_id = u.id
        LEFT JOIN resumes r ON a.resume_id = r.id
        LEFT JOIN LATERAL (
            SELECT ah.meta_data
            FROM application_history ah
            WHERE ah.application_id = a.id
              AND ah.meta_data ? 'ats_score_after'
            ORDER BY ah.created_at DESC
            LIMIT 1
        ) tail ON TRUE
        {where}
        ORDER BY a.created_at DESC
        LIMIT :limit OFFSET :offset
    """), params)

    return [
        ApplicationSummary(
            application_id=str(r.id),
            job_id=str(r.job_id),
            candidate_id=str(r.candidate_id) if r.candidate_id else None,
            resume_id=str(r.resume_id) if r.resume_id else None,
            job_title=r.title or "",
            company=r.company or "",
            platform=r.platform or "",
            status=r.status or "",
            paused=bool(r.paused),
            fit_score=float(r.fit_score) if r.fit_score is not None else None,
            ats_score=float(r.ats_score) if r.ats_score is not None else None,
            ats_score_before=float(r.ats_score_before) if r.ats_score_before is not None else None,
            ats_score_after=float(r.ats_score_after) if r.ats_score_after is not None else None,
            resume_is_base=r.resume_is_base,
            submitted_at=r.submitted_at,
            created_at=r.created_at,
            error_message=r.error_message,
            failure_reason=r.failure_reason,
            resume_url=r.resume_url,
            cover_letter_url=r.cover_letter_url,
            job_url=r.job_url,
            candidate_name=r.candidate_name,
            bd_user_name=r.bd_user_name or "System",
            bd_user_email=r.bd_user_email or "",
        )
        for r in rows.fetchall()
    ]


@router.get("/interviews", response_model=List[InterviewSummary])
async def get_interviews(
    candidate_id: Optional[UUID] = Query(None),
    upcoming_only: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List of interview records from the interview_tracking table."""
    from sqlalchemy import select

    from app.models.candidate import Candidate
    # Build candidate filter targeting 'it' (interview_tracking) alias
    if current_user.role == UserRole.admin:
        if candidate_id:
            cid_filter = "AND it.candidate_id = :cid"
            params = {"cid": str(candidate_id)}
        else:
            cid_filter = ""
            params = {}
    else:
        res = await db.execute(select(Candidate.id).where(Candidate.user_id == current_user.id))
        owned_ids = [str(row[0]) for row in res.fetchall()]
        if not owned_ids:
            cid_filter = "AND it.candidate_id = '00000000-0000-0000-0000-000000000000'::uuid"
            params = {}
        elif candidate_id:
            if str(candidate_id) in owned_ids:
                cid_filter = "AND it.candidate_id = :cid"
                params = {"cid": str(candidate_id)}
            else:
                cid_filter = "AND it.candidate_id = '00000000-0000-0000-0000-000000000000'::uuid"
                params = {}
        else:
            uuid_literals = ", ".join(f"'{cid}'::uuid" for cid in owned_ids)
            cid_filter = f"AND it.candidate_id IN ({uuid_literals})"
            params = {}

    rows = await db.execute(text(f"""
        SELECT it.id, 
               COALESCE(j.company, 'Unknown Company') AS company, 
               COALESCE(j.title, 'Unknown Role') AS position, 
               it.interview_type AS type,
               it.received_at, 
               it.status, 
               it.application_id,
               it.email_subject,
               it.email_from
        FROM interview_tracking it
        LEFT JOIN applications a ON it.application_id = a.id
        LEFT JOIN jobs j ON a.job_id = j.id
        WHERE 1=1 {cid_filter}
        ORDER BY it.received_at DESC
        LIMIT 50
    """), params)

    res = []
    for r in rows.fetchall():
        company = r.company
        if company == "Unknown Company" and r.email_subject:
            # Simple heuristics to extract company name from subject or sender email
            import re
            m = re.search(r"with\s+([A-Za-z0-9\s]+)", r.email_subject, re.IGNORECASE)
            if m:
                company = m.group(1).strip()
            else:
                m2 = re.search(r"at\s+([A-Za-z0-9\s]+)", r.email_subject, re.IGNORECASE)
                if m2:
                    company = m2.group(1).strip()
                elif r.email_from:
                    # e.g. recruiter@stripe.com -> Stripe
                    match = re.search(r"@([\w\-]+)\.", r.email_from)
                    if match:
                        company = match.group(1).capitalize()

        res.append(InterviewSummary(
            interview_id=str(r.id),
            company=company,
            position=r.position,
            round=1,
            type=r.type or "phone",
            scheduled_at=r.received_at, # date received / scheduled fallback
            meeting_url=None,
            application_id=str(r.application_id) if r.application_id else None,
            received_at=r.received_at,
            status=r.status or "PENDING"
        ))
    return res


@router.get("/analytics", response_model=AnalyticsData)
async def get_analytics(
    candidate_id: Optional[UUID] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Conversion funnel, per-platform stats, and daily application counts."""
    cid_filter, params = await get_dashboard_candidate_filter(db, current_user, candidate_id)

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
          {cid_filter}
    """), params)
    avg_hours = avg_row.scalar()

    return AnalyticsData(
        conversion_funnel=funnel,
        per_platform_stats=per_platform,
        daily_applications=daily,
        avg_time_to_response_hours=float(avg_hours) if avg_hours else None,
    )


_OPS_QUEUES = [
    "celery",
    "queue:job_discovery",
    "queue:job_processing",
    "queue:resume_generation",
    "queue:application_execution",
    "queue:application_execution_fixed",
    "queue:manual_apply",
    "queue:email_scan",
]


@router.get("/ops", response_model=OpsStats)
async def get_ops_stats(
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(require_admin),
):
    """Admin-only operational pipeline stats: Redis queue depths, live workers,
    and in-flight/24h application counts. Broker probes are best-effort and
    hard-capped at 4s each — a dead broker must never hang the dashboard."""
    import asyncio

    from app.celery_app import celery_app

    def _queue_depths() -> List[QueueDepth]:
        depths: List[QueueDepth] = []
        with celery_app.connection_or_acquire() as conn:
            client = conn.default_channel.client
            for q in _OPS_QUEUES:
                depths.append(QueueDepth(name=q, depth=int(client.llen(q))))
        return depths

    def _active_workers() -> List[WorkerInfo]:
        active = celery_app.control.inspect(timeout=3).active() or {}
        return [
            WorkerInfo(name=name, active_tasks=len(tasks or []))
            for name, tasks in active.items()
        ]

    try:
        queues = await asyncio.wait_for(asyncio.to_thread(_queue_depths), timeout=4.0)
    except Exception:
        queues = []
    try:
        workers = await asyncio.wait_for(asyncio.to_thread(_active_workers), timeout=4.0)
    except Exception:
        workers = []

    row = (await db.execute(text("""
        SELECT
          COUNT(*) FILTER (WHERE status IN ('QUEUED','APPLICATION_STARTED','FORM_COMPLETED'))
            AS in_flight,
          COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours')
            AS created_today,
          COUNT(*) FILTER (WHERE status IN ('SUBMITTED','CONFIRMED')
                             AND updated_at >= NOW() - INTERVAL '24 hours')
            AS submitted_today
        FROM applications
    """))).fetchone()

    return OpsStats(
        queues=queues,
        workers=workers,
        applications_in_flight=row.in_flight or 0,
        applications_today=row.created_today or 0,
        submitted_today=row.submitted_today or 0,
    )


@router.get("/activity-feed", response_model=List[ActivityEvent])
async def get_activity_feed(
    candidate_id: Optional[UUID] = Query(None),
    limit: int = Query(20, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Recent events — status changes, emails classified, interviews detected."""
    cid_filter, cid_params = await get_dashboard_candidate_filter(db, current_user, candidate_id)
    params = {"limit": limit, **cid_params}

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
