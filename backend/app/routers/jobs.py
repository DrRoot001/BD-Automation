import json
import asyncio
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, status, BackgroundTasks
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.schemas.job import JobCreate, JobResponse

logger = logging.getLogger(__name__)
DATA_LOG_PATH = Path(__file__).resolve().parents[1] / "jobs_data.json"
router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# ── Job Discovery ────────────────────────────────────────────────────────────

_discovery_state: dict = {"running": False, "last_result": None}

ALLOWED_FIELDS = {
    "title", "company", "location", "source", "source_url", "canonical_url",
    "description", "skills", "salary_min", "salary_max", "pay_period",
    "job_type", "posted_at", "embedding",
}


async def _run_discovery(api_base: str = "http://localhost:8000/api") -> dict:
    """Run all module2 scrapers and POST results to the jobs API."""
    import httpx
    from module2.adapters import get_adapter
    from module2.normalization import normalize_batch
    from module2.run_scrape import run_all

    SCRAPER_CONFIGS = [
        ("greenhouse", {"company": "stripe",      "retries": 3}, 25),
        ("greenhouse", {"company": "anthropic",   "retries": 3}, 25),
        ("greenhouse", {"company": "airbnb",      "retries": 3}, 25),
        ("greenhouse", {"company": "datadog",     "retries": 3}, 25),
        ("greenhouse", {"company": "coinbase",    "retries": 3}, 25),
        ("greenhouse", {"company": "figma",       "retries": 3}, 25),
        ("greenhouse", {"company": "notion",      "retries": 3}, 25),
        ("greenhouse", {"company": "hubspot",     "retries": 3}, 25),
        ("greenhouse", {"company": "brex",        "retries": 3}, 25),
        ("greenhouse", {"company": "scaleai",     "retries": 3}, 25),
        ("greenhouse", {"company": "airtable",    "retries": 3}, 25),
        ("greenhouse", {"company": "vercel",      "retries": 3}, 25),
        ("greenhouse", {"company": "asana",       "retries": 3}, 25),
        ("greenhouse", {"company": "openai",      "retries": 3}, 25),
        ("greenhouse", {"company": "cloudflare",  "retries": 3}, 25),
        ("greenhouse", {"company": "gusto",       "retries": 3}, 25),
        ("greenhouse", {"company": "postman",     "retries": 3}, 25),
        ("lever",      {"company": "palantir",    "retries": 3}, 25),
        ("indeed",     {"query": "software engineer remote", "location": "Remote", "retries": 3}, 25),
        ("rss_generic", {"rss_url": "https://remoteok.com/remote-jobs.rss"}, 25),
        ("rss_generic", {"rss_url": "https://jobicy.com/?feed=job_feed"}, 25),
    ]

    total_discovered = 0
    total_saved = 0
    errors = []
    backend_url = f"{api_base}/jobs"

    # --- 1. Run Legacy Node.js Pipeline (Aggregators like Dice, RemoteRocketship) ---
    try:
        logger.info("Starting Node.js scrapers via run_all()")
        node_stats = await asyncio.to_thread(run_all)
        total_discovered += node_stats.get("scraped", 0)
        total_saved += node_stats.get("posted", 0)
        if "errors" in node_stats and node_stats["errors"]:
            errors.extend(node_stats["errors"])
    except Exception as e:
        logger.error(f"Node.js scraper pipeline failed: {e}")
        errors.append(f"node_scrapers: {str(e)}")

    # --- 2. Run Python Adapters (Greenhouse, Lever, etc.) ---
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Fetch existing URLs to avoid duplicates
        try:
            existing_resp = await client.get(backend_url, params={"limit": 5000})
            existing_urls = {j.get("source_url") for j in existing_resp.json() if isinstance(j, dict)}
        except Exception:
            existing_urls = set()

        for adapter_name, filters, cap in SCRAPER_CONFIGS:
            try:
                cls = get_adapter(adapter_name)
                if not cls:
                    continue
                adapter = cls()
                jobs = await asyncio.wait_for(adapter.discover_jobs(filters), timeout=45.0)
                total_discovered += len(jobs)
                if not jobs:
                    continue
                normalized = normalize_batch(jobs)
                payload = []
                for job in normalized:
                    d = job.to_dict()
                    # NormalizedJob uses 'url', backend expects 'source_url'
                    if 'url' in d and 'source_url' not in d:
                        d['source_url'] = d.pop('url')
                    # Canonical URL mapping if missing
                    if 'job_url' in d and 'canonical_url' not in d:
                        d['canonical_url'] = d.pop('job_url')
                    
                    filtered = {k: v for k, v in d.items() if k in ALLOWED_FIELDS}
                    # Ensure required fields
                    if 'source_url' in filtered and filtered['source_url']:
                        payload.append(filtered)

                # Filter out jobs that we already have
                filtered_payload = [p for p in payload if p.get("source_url") not in existing_urls]
                
                # Cap the unique results to limit
                filtered_payload = filtered_payload[:cap]
                
                if not filtered_payload:
                    errors.append(f"{adapter_name}: all {len(payload)} discovered jobs were already in DB")
                    continue
                    
                # Post individually for greenhouse to isolate errors
                if adapter_name == "greenhouse":
                    for jp in filtered_payload:
                        try:
                            r = await client.post(backend_url, json=[jp])
                            if r.status_code in (200, 201):
                                saved = r.json()
                                total_saved += len(saved)
                                existing_urls.add(jp.get("source_url"))
                            else:
                                errors.append(f"{adapter_name} post failed: {r.status_code} {r.text[:50]}")
                        except Exception as e:
                            errors.append(f"{adapter_name}: {e}")
                else:
                    r = await client.post(backend_url, json=filtered_payload)
                    if r.status_code in (200, 201):
                        saved = r.json()
                        total_saved += len(saved)
                        existing_urls.update(jp.get("source_url") for jp in filtered_payload)
                    else:
                        errors.append(f"{adapter_name} post failed: {r.status_code} {r.text[:100]}")
            except asyncio.TimeoutError:
                errors.append(f"{adapter_name}: timeout")
            except Exception as e:
                errors.append(f"{adapter_name}: {e}")
                logger.warning(f"[Discovery] adapter {adapter_name} failed: {e}")

    return {
        "status": "completed",
        "total_discovered": total_discovered,
        "total_saved": total_saved,
        "errors": errors,
    }


def _run_discovery_thread():
    """Run job discovery in a background thread with its own event loop."""
    import os
    _discovery_state["running"] = True
    try:
        api_base = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api")
        result = asyncio.run(_run_discovery(api_base))
        _discovery_state["last_result"] = result
        logger.info(f"[Discovery] Completed: {result}")
    except Exception as e:
        logger.error(f"[Discovery] Failed: {e}", exc_info=True)
        _discovery_state["last_result"] = {"status": "error", "error": str(e)}
    finally:
        _discovery_state["running"] = False


@router.post("/discover", status_code=202)
async def trigger_job_discovery():
    """Trigger job discovery across all scrapers (Greenhouse, Lever, Indeed, RSS, Node Fetchfox).
    
    Runs in the background and returns immediately. Check /api/jobs/discover/status
    for progress. Discovers fresh jobs and saves them to the database.
    """
    if _discovery_state["running"]:
        return {
            "status": "already_running",
            "message": "Job discovery is already in progress. Check /api/jobs/discover/status."
        }

    import threading
    threading.Thread(target=_run_discovery_thread, daemon=True).start()

    return {
        "status": "started",
        "message": "Job discovery started across all adapters (including Node.js FetchFox for Dice/RemoteRocketship). This may take 15-30 minutes.",
    }


@router.get("/discover/status")
async def get_discovery_status():
    """Check the status of the last or running job discovery."""
    return {
        "running": _discovery_state["running"],
        "last_result": _discovery_state["last_result"],
    }


# ── Job CRUD ──────────────────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED, response_model=List[JobResponse])
async def create_jobs(
    jobs: List[JobCreate],
    db: AsyncSession = Depends(get_db)
):
    created = []
    source_urls = [job.source_url for job in jobs if job.source_url]
    existing_urls = set()
    skipped_duplicates = 0

    if source_urls:
        result = await db.execute(select(Job.source_url).where(Job.source_url.in_(source_urls)))
        existing_urls = {row[0] for row in result.fetchall()}

    for job_data in jobs:
        job_dict = job_data.model_dump(exclude_unset=True)
        if job_dict.get('embedding') is None:
            job_dict.pop('embedding', None)

        source = job_dict.get('source')
        source_url = job_dict.get('source_url')
        company = job_dict.get('company')
        title = job_dict.get('title', '')

        # Check for test/demo companies (case-insensitive)
        INVALID_COMPANIES = {"test company", "testco", "test", "demo", "sample", "example"}
        if company and company.lower().strip() in INVALID_COMPANIES:
            logger.warning(f"Skipping test job: {title} at {company}")
            continue

        # Check for invalid fields
        if not source_url or source == "manual":
            logger.warning(f"Skipping invalid job — no source_url or manual platform")
            continue

        if job_dict.get('source_url') in existing_urls:
            skipped_duplicates += 1
            continue

        job = Job(**job_dict)
        db.add(job)
        created.append(job)

    if not created:
        from fastapi.responses import Response
        import json as _json
        body = _json.dumps([]).encode()
        return Response(
            content=body,
            media_type="application/json",
            status_code=201,
            headers={"X-Skipped-Duplicates": str(skipped_duplicates)}
        )

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return []

    for job in created:
        await db.refresh(job)
        if not job.embedding:
            from app.tasks.embedding_generation import generate_job_embedding
            generate_job_embedding.delay(str(job.id))

    await _append_jobs_to_json_log(created)

    from fastapi.responses import Response as _Resp
    import json as _json
    from app.schemas.job import JobResponse as _JobResp
    body = _json.dumps([_JobResp.model_validate(j).model_dump(mode="json") for j in created]).encode()
    return _Resp(
        content=body,
        media_type="application/json",
        status_code=201,
        headers={"X-Skipped-Duplicates": str(skipped_duplicates)}
    )


async def _append_jobs_to_json_log(created_jobs: List[Job]) -> None:
    try:
        existing_data = []
        if DATA_LOG_PATH.exists():
            with DATA_LOG_PATH.open("r", encoding="utf-8") as file:
                existing_data = json.load(file)
        new_entries = [
            {
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "source": job.source,
                "source_url": job.source_url,
                "canonical_url": job.canonical_url,
                "description": job.description,
                "skills": job.skills,
                "salary_min": job.salary_min,
                "salary_max": job.salary_max,
                "pay_period": job.pay_period,
                "job_type": job.job_type,
                "posted_at": job.posted_at.isoformat() if job.posted_at else None,
                "embedding": job.embedding,
                "id": str(job.id),
                "created_at": job.created_at.isoformat() if job.created_at else None,
            }
            for job in created_jobs
        ]
        existing_data.extend(new_entries)
        DATA_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with DATA_LOG_PATH.open("w", encoding="utf-8") as file:
            json.dump(existing_data, file, indent=2, ensure_ascii=False)
    except Exception as exc:
        # Keep ingestion working even if JSON logging fails.
        print(f"Warning: failed to append jobs to JSON log: {exc}")


from app.schemas.job import JobMatchingResponse

@router.get("/for-matching", response_model=List[JobMatchingResponse])
async def get_jobs_for_matching(
    skip: int = 0,
    limit: int = 100,
    candidate_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Lightweight endpoint for the keyword matching pipeline.
    Explicitly defers 'description' and 'embedding' to reduce Pydantic serialization
    overhead and improve fetch performance (e.g. from 17s to <1s for 100 jobs).
    """
    from sqlalchemy import select, or_, and_
    from sqlalchemy.orm import defer
    from app.models.resume import Resume
    from uuid import UUID

    exclusions = or_(
        Job.source == 'manual',
        Job.source_url.is_(None),
        Job.source_url == '',
        Job.company.ilike('%test%'),
        Job.company.ilike('%testco%'),
        Job.company.ilike('%demo%'),
        Job.company.ilike('%sample%'),
        Job.company.ilike('%example%'),
        and_(Job.title.ilike('%test%'), Job.company.ilike('%test%'))
    )

    resume_embedding = None
    if candidate_id:
        try:
            cand_uuid = UUID(candidate_id)
            stmt_res = (
                select(Resume)
                .where(Resume.candidate_id == cand_uuid, Resume.is_base == True)
                .order_by(Resume.version.desc())
                .limit(1)
            )
            base_resume = (await db.execute(stmt_res)).scalars().first()
            if base_resume and base_resume.embedding is not None:
                resume_embedding = base_resume.embedding
        except Exception as e:
            logger.error(f"Error fetching resume embedding for candidate {candidate_id}: {e}")

    stmt = select(Job).options(defer(Job.embedding), defer(Job.description)).where(~exclusions)
    
    if resume_embedding is not None:
        stmt = stmt.order_by(Job.embedding.cosine_distance(resume_embedding).asc())
    else:
        stmt = stmt.order_by(Job.created_at.desc())

    result = await db.execute(
        stmt.offset(skip).limit(limit)
    )
    return result.scalars().all()


@router.get("", response_model=List[JobResponse])
async def get_jobs(
    skip: int = 0,
    limit: int = 100,
    candidate_id: Optional[str] = None,
    search: Optional[str] = None,
    source: Optional[str] = None,
    job_type: Optional[str] = None,
    time_filter: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import select, or_, and_
    from sqlalchemy.orm import defer
    from uuid import UUID
    from datetime import datetime, timezone, timedelta
    
    exclusions = or_(
        Job.source == 'manual',
        Job.source_url.is_(None),
        Job.source_url == '',
        Job.company.ilike('%test%'),
        Job.company.ilike('%testco%'),
        Job.company.ilike('%demo%'),
        Job.company.ilike('%sample%'),
        Job.company.ilike('%example%'),
        and_(Job.title.ilike('%test%'), Job.company.ilike('%test%'))
    )

    def apply_job_filters(query):
        if search:
            q = f"%{search}%"
            query = query.where(or_(Job.company.ilike(q), Job.title.ilike(q)))
        if source:
            query = query.where(Job.source.ilike(f"%{source}%"))
        if job_type:
            query = query.where(Job.job_type.ilike(f"%{job_type}%"))
        if time_filter:
            now = datetime.now(timezone.utc)
            if time_filter == '24h':
                query = query.where(Job.created_at >= now - timedelta(days=1))
            elif time_filter == '3d':
                query = query.where(Job.created_at >= now - timedelta(days=3))
            elif time_filter == '7d':
                query = query.where(Job.created_at >= now - timedelta(days=7))
            elif time_filter == '30d':
                query = query.where(Job.created_at >= now - timedelta(days=30))
        return query

    candidate_uuid = None
    if candidate_id:
        try:
            candidate_uuid = UUID(candidate_id)
        except ValueError:
            pass

    if candidate_uuid:
        from app.models.resume import Resume
        from app.models.application import Application
        
        # 1. Fetch job IDs candidate has already applied to
        applied_result = await db.execute(
            select(Application.job_id).where(Application.candidate_id == candidate_uuid)
        )
        applied_job_ids = [row[0] for row in applied_result.fetchall() if row[0] is not None]
        
        # 2. Fetch base resume
        resume_stmt = (
            select(Resume)
            .where(Resume.candidate_id == candidate_uuid, Resume.is_base == True)
            .order_by(Resume.version.desc())
            .limit(1)
        )
        base_resume = (await db.execute(resume_stmt)).scalars().first()
        
        # 3. Build job select query
        stmt = select(Job).options(defer(Job.embedding)).where(Job.is_duplicate == False, ~exclusions)
        if applied_job_ids:
            stmt = stmt.where(Job.id.notin_(applied_job_ids))
            
        stmt = apply_job_filters(stmt)
            
        if base_resume and base_resume.embedding is not None:
            stmt = stmt.order_by(Job.embedding.cosine_distance(base_resume.embedding).asc())
        else:
            # Fall back to newest-first if no embedding or resume found
            stmt = stmt.order_by(Job.created_at.desc())
            
        result = await db.execute(
            stmt.offset(skip).limit(limit)
        )
        return result.scalars().all()
    else:
        # Default behavior: all duplicate-free/any jobs, newest first
        stmt = select(Job).options(defer(Job.embedding)).where(~exclusions)
        stmt = apply_job_filters(stmt)
        result = await db.execute(
            stmt.order_by(Job.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all()

@router.get("/count")
async def get_jobs_count(
    search: Optional[str] = None,
    source: Optional[str] = None,
    job_type: Optional[str] = None,
    time_filter: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import select, func, or_, and_
    from datetime import datetime, timezone, timedelta
    exclusions = or_(
        Job.source == 'manual',
        Job.source_url.is_(None),
        Job.source_url == '',
        Job.company.ilike('%test%'),
        Job.company.ilike('%testco%'),
        Job.company.ilike('%demo%'),
        Job.company.ilike('%sample%'),
        Job.company.ilike('%example%'),
        and_(Job.title.ilike('%test%'), Job.company.ilike('%test%'))
    )

    def apply_job_filters(query):
        if search:
            q = f"%{search}%"
            query = query.where(or_(Job.company.ilike(q), Job.title.ilike(q)))
        if source:
            query = query.where(Job.source.ilike(f"%{source}%"))
        if job_type:
            query = query.where(Job.job_type.ilike(f"%{job_type}%"))
        if time_filter:
            now = datetime.now(timezone.utc)
            if time_filter == '24h':
                query = query.where(Job.created_at >= now - timedelta(days=1))
            elif time_filter == '3d':
                query = query.where(Job.created_at >= now - timedelta(days=3))
            elif time_filter == '7d':
                query = query.where(Job.created_at >= now - timedelta(days=7))
            elif time_filter == '30d':
                query = query.where(Job.created_at >= now - timedelta(days=30))
        return query

    query = select(func.count(Job.id)).where(~exclusions)
    query = apply_job_filters(query)
    result = await db.execute(query)
    return {"total_count": result.scalar()}

@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import select
    from sqlalchemy.orm import defer
    from fastapi import HTTPException
    from uuid import UUID
    try:
        job_uuid = UUID(job_id) if isinstance(job_id, str) else job_id
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job UUID")
        
    result = await db.execute(select(Job).options(defer(Job.embedding)).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job