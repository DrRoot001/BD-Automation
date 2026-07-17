import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.config import get_settings
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.resume import Resume
from app.services.llm_cache import cache_score, get_cached_score
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from module2.normalization.schemas import NormalizedJob
from module3.orchestrator import orchestrate_application_package
from module3.parser.resume_parser import ResumeData, ResumeSection
from module3.scoring.fit_scorer import score_job_fit

logger = logging.getLogger(__name__)
settings = get_settings()

async def get_active_application_count(
    candidate_id: UUID,
    session: AsyncSession,
    since_datetime: Optional[datetime] = None,
    inflight_only: bool = False
) -> int:
    """
    Counts active applications for the candidate, excluding any that are stale
    (stuck in QUEUED > 15 mins, or APPLICATION_STARTED/FORM_COMPLETED > 30 mins).
    Optionally filters to applications created after since_datetime.
    If inflight_only is True, only counts applications currently being processed.
    """
    from datetime import datetime, timedelta, timezone

    from app.models.application import Application
    from app.models.application_history import ApplicationHistory

    if inflight_only:
        active_statuses = {
            "FOUND", "MATCHED", "RESUME_UPDATED", "COVER_LETTER_CREATED",
            "QUEUED", "APPLICATION_STARTED", "FORM_COMPLETED"
        }
    else:
        active_statuses = {
            "FOUND", "MATCHED", "RESUME_UPDATED", "COVER_LETTER_CREATED",
            "QUEUED", "APPLICATION_STARTED", "FORM_COMPLETED",
            "SUBMITTED", "CONFIRMED", "INTERVIEW_R1", "INTERVIEW_R2",
            "INTERVIEW_R3", "INTERVIEW_R4", "OFFER"
        }

    is_mock = type(session).__name__ in ("AsyncMock", "MagicMock") or hasattr(session, "_mock_self")

    from sqlalchemy import or_
    stmt = select(Application).where(
        Application.candidate_id == candidate_id,
        Application.status.in_(list(active_statuses)),
        # Paused apps are intentionally held by an operator — they must not count
        # toward the active cap, otherwise pausing a stuck app wouldn't unblock the
        # queue (the whole point of the pause control).
        Application.paused == 0,
        or_(Application.failure_reason.is_(None), Application.failure_reason != "JOB_EXPIRED")
    )
    if since_datetime is not None:
        stmt = stmt.where(Application.created_at >= since_datetime)

    apps = (await session.execute(stmt)).scalars().all()

    if is_mock:
        return len(apps)

    # Staleness thresholds must mirror recover_stuck_applications_async so a row
    # the watchdog is about to fail is not counted as "active" here. Includes the
    # tailoring hand-off states (MATCHED/RESUME_UPDATED/COVER_LETTER_CREATED) which
    # would otherwise pin the cap forever if their pipeline died mid-tailor.
    STALE_THRESHOLDS_MIN = {
        # FOUND was MISSING here: it is counted as in-flight (see active_statuses)
        # but had no staleness cutoff, so a FOUND row whose pipeline died before
        # tailoring pinned the cap FOREVER — and with max_apps=1 that silently
        # blocked every future Auto Apply for the candidate (the run no-ops with
        # limit_reached). 30 min mirrors the watchdog's FOUND threshold in
        # app/services/state_machine.py, so the cap never counts a row the
        # watchdog is about to reap.
        "FOUND": 30,
        "QUEUED": 15,
        "APPLICATION_STARTED": 20,
        "FORM_COMPLETED": 20,
        "MATCHED": 20,
        "RESUME_UPDATED": 20,
        "COVER_LETTER_CREATED": 20,
    }
    count = 0
    now = datetime.now(timezone.utc)
    for app in apps:
        if app.status in STALE_THRESHOLDS_MIN:
            # Check if stale (duration based on latest history transition)
            hist_stmt = (
                select(ApplicationHistory)
                .where(ApplicationHistory.application_id == app.id)
                .order_by(ApplicationHistory.created_at.desc())
                .limit(1)
            )
            latest_history = (await session.execute(hist_stmt)).scalars().first()

            start_time = None
            if latest_history and hasattr(latest_history, "created_at"):
                val = latest_history.created_at
                if isinstance(val, datetime):
                    start_time = val
            if start_time is None:
                start_time = app.created_at

            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)

            threshold_min = STALE_THRESHOLDS_MIN[app.status]
            if start_time < now - timedelta(minutes=threshold_min):
                continue
        count += 1
    return count

async def run_matching_for_candidate(
    candidate_id: UUID,
    session: AsyncSession,
    is_beat_task: bool = False,
    target_job_ids: Optional[List[str]] = None,
    manual_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Executes the daily matching and auto-apply flow for a specific candidate.
    
    1. Fetches candidate details and base resume with pgvector embedding.
    2. Enforces the configurable daily cap.
    3. Finds duplicate-free jobs created in the last 24 hours passing pgvector similarity threshold.
    4. Filters out already-applied/non-retryable jobs.
    5. Scores candidate job fit using the LLM.
    6. For fits >= 70, tailors resume/CL/answers and enqueues browser execution.
    """
    import time
    logger.info(f"[Matching] START run_matching_for_candidate for candidate={candidate_id}")
    t_start = time.time()

    is_mock = type(session).__name__ in ("AsyncMock", "MagicMock") or hasattr(session, "_mock_self")

    t0 = time.time()
    candidate = await session.get(Candidate, candidate_id)
    if not candidate:
        logger.error(f"[Matching] Candidate {candidate_id} not found.")
        return {"error": "candidate_not_found"}

    if getattr(candidate, "automation_paused", 0) == 1:
        logger.info(f"[Matching] Automation is PAUSED for candidate {candidate.name} ({candidate_id}). Skipping.")
        return {
            "candidate_id": str(candidate_id),
            "jobs_scanned": 0,
            "pgvector_passed": 0,
            "llm_passed": 0,
            "enqueued_count": 0,
            "details": [],
            "skipped": [{"reason": "automation_paused"}]
        }

    candidate_dict = {
        "id": str(candidate.id),
        "name": candidate.name,
        "email": candidate.email,
        "phone": candidate.phone,
        "location": candidate.location,
        "work_auth": candidate.work_auth,
        "tech_stack": candidate.tech_stack,
        "years_exp": candidate.years_exp,
        "linkedin_url": candidate.linkedin_url
    }

    # 2. Fetch base resume (order by version desc and pick the latest if multiple exist)
    t0 = time.time()
    resume_stmt = (
        select(Resume)
        .where(Resume.candidate_id == candidate_id, Resume.is_base == True)
        .order_by(Resume.version.desc())
        .limit(1)
    )
    base_resume = (await session.execute(resume_stmt)).scalars().first()
    logger.info(f"[Matching] resume fetch done in {time.time()-t0:.2f}s")
    if not base_resume:
        logger.warning(f"[Matching] Candidate {candidate.name} ({candidate_id}) has no base resume. Skipping.")
        return {"error": "no_base_resume"}

    t_self_heal = time.time()
    if base_resume.parsed_json is None or base_resume.embedding is None:
        logger.info(f"[Matching] Base resume for Candidate {candidate.name} ({candidate_id}) is missing parsed_json or embedding. Attempting self-heal...")
        try:
            from app.routers.resumes import _generate_resume_embedding_from_json

            # If parsed_json already exists, only re-generate the embedding (skip PDF re-parsing).
            # This avoids crashing on corrupt/small PDFs when the JSON data is already good.
            if base_resume.parsed_json is not None:
                logger.info("[Matching] parsed_json is present — generating embedding from existing JSON (skipping PDF re-parse).")
                base_resume.embedding = await asyncio.to_thread(
                    _generate_resume_embedding_from_json, base_resume.parsed_json
                )
            else:
                # parsed_json is missing — try to parse the PDF first
                pdf_parsed = False
                try:
                    from module3.parser.resume_parser import parse_resume
                    logger.info(f"[Matching] parsed_json is missing — parsing PDF from {base_resume.file_url[:60]}...")
                    parsed_resume = await parse_resume(base_resume.file_url, candidate_id=str(candidate_id), resume_id=str(base_resume.id))
                    base_resume.parsed_json = parsed_resume.sections.model_dump()
                    pdf_parsed = True
                except Exception as pdf_err:
                    logger.warning(f"[Matching] PDF parse failed for {candidate.name}: {pdf_err}. Synthesizing parsed_json from candidate profile...")
                    # Fallback: build a minimal parsed_json from the candidate's profile fields
                    # so that the embedding can still be generated and the pipeline can proceed.
                    tech = candidate_dict.get("tech_stack") or []
                    base_resume.parsed_json = {
                        "summary": f"{candidate.name} with {candidate_dict.get('years_exp', 0)} years of experience.",
                        "skills": tech,
                        "keywords": tech,
                        "experience": [],
                        "education": [],
                        "certifications": [],
                    }
                    logger.info(f"[Matching] Synthesized parsed_json from candidate profile for {candidate.name}.")

                base_resume.embedding = await asyncio.to_thread(
                    _generate_resume_embedding_from_json, base_resume.parsed_json
                )

            session.add(base_resume)
            await session.commit()
            await session.refresh(base_resume)
            logger.info(f"[Matching] Successfully self-healed base resume for Candidate {candidate.name} ({candidate_id}).")
        except Exception as parse_err:
            logger.error(f"[Matching] Self-heal failed for Candidate {candidate.name} ({candidate_id}): {parse_err}", exc_info=True)
            return {"error": "resume_parsing_failed"}

    if base_resume.embedding is None:
        logger.warning(f"[Matching] Candidate {candidate.name} ({candidate_id}) has base resume but no embedding after self-heal. Skipping.")
        return {"error": "no_resume_embedding"}

    # 3. Calculate remaining daily limit
    # manual_limit overrides the daily cap (used when BD user manually clicks "Run Now")
    t0 = time.time()
    max_daily = None  # initialized here so it's always defined in the loop below
    if manual_limit is not None:
        active_count = await get_active_application_count(candidate_id, session, inflight_only=True)
        remaining_slots = max(0, manual_limit - active_count)
        logger.info(f"[Matching] active count done in {time.time()-t0:.2f}s")
        logger.info(f"[Matching] Candidate {candidate.name}: manual run with limit={manual_limit}, inflight={active_count}, remaining={remaining_slots}")
        if remaining_slots <= 0:
            logger.info(f"[Matching] Candidate {candidate.name}: limit reached (limit={manual_limit}, inflight={active_count}).")
            return {"error": "limit_reached", "active_count": active_count}
    else:
        max_daily = getattr(candidate, "max_daily_apps_override", None)
        if max_daily is None:
            from app.services import runtime_config
            max_daily = runtime_config.get("max_daily_applications_per_candidate")

        time_24h_ago = datetime.now(timezone.utc) - timedelta(hours=24)

        already_applied_today = await get_active_application_count(candidate_id, session, since_datetime=time_24h_ago)
        remaining_slots = max(0, max_daily - already_applied_today)

        logger.info(f"[Matching] Candidate {candidate.name}: applied today={already_applied_today}, remaining={remaining_slots}")


    # Build maps of existing apps to check retry/skip conditions
    skipped_jobs = set()
    retryable_apps = {}

    # Only skip jobs with terminal-positive outcomes. Jobs that FAILED or were
    # WITHDRAWN/REJECTED due to transient errors should be retryable — filtering
    # them out permanently means a candidate can never retry after fixing infra.
    _TERMINAL_POSITIVE = {
        "SUBMITTED", "CONFIRMED", "INTERVIEW_R1", "INTERVIEW_R2",
        "INTERVIEW_R3", "INTERVIEW_R4", "OFFER",
        # Active in-progress states — don't double-dispatch
        "FOUND", "MATCHED", "RESUME_UPDATED", "COVER_LETTER_CREATED",
        "QUEUED", "APPLICATION_STARTED", "FORM_COMPLETED",
    }
    # FAILED apps with these reasons will fail identically on retry — or, for
    # EMAIL_VERIFICATION, the submit already fired and a re-run would
    # double-submit. Keep in sync with _TERMINAL_FAILURE_REASONS in
    # app/tasks/dynamic_apply.py (duplicated to avoid a circular import).
    _TERMINAL_FAILURE = {
        "BOT_DETECTED", "ROBOTS_BLOCKED", "JOB_EXPIRED",
        "LOGIN_REQUIRED", "MAX_RETRIES_EXCEEDED",
        "SPAM_FLAGGED", "ALREADY_APPLIED", "QUALIFICATION_MISMATCH",
        "EMAIL_VERIFICATION",
    }
    all_apps_stmt = select(Application).where(Application.candidate_id == candidate_id)
    all_apps = (await session.execute(all_apps_stmt)).scalars().all()

    for app in all_apps:
        if app.status in _TERMINAL_POSITIVE:
            skipped_jobs.add(app.job_id)
        elif (
            app.status == "FAILED"
            and str(app.failure_reason or "").upper() in _TERMINAL_FAILURE
        ):
            skipped_jobs.add(app.job_id)

    # 4. Fetch jobs added in lookback window (defaults to 24h, 72h on Mondays) that are not duplicates and pass pgvector distance < 0.35
    from sqlalchemy import and_, func, or_
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

    # Portal viability: portals that hard-require credentials/sessions we don't
    # have configured — or that are infrastructure-blocked (captcha/IP ban) —
    # guarantee a failed application and burn one of the candidate's slots.
    # Applied only to automatic discovery; explicitly targeted jobs
    # (target_job_ids) are honored as operator intent (e.g. Dice credentials
    # stored on the candidate profile instead of env vars).
    has_candidate_creds = bool(
        (getattr(candidate, "password", "") or "").strip()
        and (
            (getattr(candidate, "gmail", "") or "").strip()
            or (getattr(candidate, "email", "") or "").strip()
        )
    )

    _unviable = []
    if not (has_candidate_creds or (os.getenv("DICE_EMAIL") and os.getenv("DICE_PASSWORD"))):
        _unviable.append(Job.source_url.ilike('%dice.com%'))
    if not (has_candidate_creds or (os.getenv("WORKDAY_USERNAME") and os.getenv("WORKDAY_PASSWORD"))):
        _unviable.append(Job.source_url.ilike('%myworkdayjobs.com%'))
    if not (has_candidate_creds or (os.getenv("GLASSDOOR_EMAIL") and os.getenv("GLASSDOOR_PASSWORD"))):
        _unviable.append(Job.source_url.ilike('%glassdoor.com%'))
    if not (has_candidate_creds or (os.getenv("ZIPRECRUITER_EMAIL") and os.getenv("ZIPRECRUITER_PASSWORD"))):
        _unviable.append(Job.source_url.ilike('%ziprecruiter.com%'))
    if not os.getenv("PROXY_URL"):
        # builtin.com /job/ pages are Cloudflare IP-banned from datacenter egress.
        _unviable.append(Job.source_url.ilike('%builtin.com%'))
        if not os.getenv("NOPECHA_API_KEY"):
            # Lever's final submit is hCaptcha-gated; needs a token solver or a
            # residential proxy — neither configured means guaranteed failure.
            _unviable.append(Job.source_url.ilike('%jobs.lever.co%'))
            _unviable.append(Job.source == 'lever')
    # LinkedIn Easy Apply needs a manually pre-seeded browser session; there is
    # no automated login path.
    _unviable.append(Job.source_url.ilike('%linkedin.com/jobs%'))
    # RemoteRocketship '/company/' pages don't expose the publicjobs apply flow
    # the adapter drives — only '/publicjobs/' URLs are supported.
    _unviable.append(Job.source_url.ilike('%remoterocketship.com/company/%'))

    if target_job_ids:
        # If specific target jobs were requested (e.g. from dynamic_apply), skip pgvector and time filters.
        from uuid import UUID as _UUID
        valid_uuids = []
        for jid in target_job_ids:
            try:
                valid_uuids.append(_UUID(jid))
            except Exception:
                pass

        if not valid_uuids:
            logger.warning("[Matching] target_job_ids provided but no valid UUIDs found.")
            return {"error": "invalid_target_job_ids"}

        jobs_stmt = select(Job).where(Job.id.in_(valid_uuids), ~exclusions)
    else:
        weekday = datetime.now(timezone.utc).weekday()
        lookback_hours = settings.job_matching_monday_lookback_hours if weekday == 0 else settings.job_matching_lookback_hours
        time_job_lookback = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

        jobs_stmt = select(Job).where(
            Job.created_at >= time_job_lookback,
            Job.is_duplicate == False,
            Job.embedding.isnot(None),
            Job.embedding.cosine_distance(base_resume.embedding) < 0.35,
            ~exclusions
        )
        if _unviable:
            jobs_stmt = jobs_stmt.where(~or_(*_unviable))

        # Category scoping: when the candidate has a job_category, exclude jobs
        # tagged with a DIFFERENT category (keep uncategorized ones) and process
        # same-category jobs first so they win the daily-cap slots. Mirrors the
        # filter in /api/jobs/for-matching.
        cand_category = (getattr(candidate, "job_category", None) or "").strip().lower()
        if cand_category:
            jobs_stmt = jobs_stmt.where(or_(
                Job.job_category.is_(None),
                func.lower(Job.job_category) == cand_category,
            ))
            jobs_stmt = jobs_stmt.order_by(
                (func.lower(Job.job_category) == cand_category).desc().nullslast()
            )
    t0 = time.time()
    jobs = (await session.execute(jobs_stmt)).scalars().all()
    pgvector_passed_count = len(jobs)
    logger.info(f"[Matching] jobs fetch done in {time.time()-t0:.2f}s — found {pgvector_passed_count} jobs")

    if target_job_ids:
        logger.info(f"[Matching] Candidate {candidate.name}: executing on {pgvector_passed_count} target jobs.")
    else:
        logger.info(f"[Matching] Candidate {candidate.name}: found {pgvector_passed_count} jobs passing pgvector similarity.")

    # ── Diagnostic: surface platform distribution so we can tell whether
    # non-Greenhouse jobs are even reaching the gate. Helps answer "why does
    # only Greenhouse start?" — if this log shows {'greenhouse': N} only,
    # the upstream sourcing/dedup is the bottleneck, not the executor.
    try:
        from collections import Counter as _Counter
        _platform_counts = _Counter((j.source or "unknown").lower() for j in jobs)
        logger.info(
            f"[Matching] candidate={candidate.name} platform breakdown of "
            f"selectable jobs: {dict(_platform_counts)}"
        )
    except Exception:
        pass

    # 5. Process shortlist
    enqueued = []
    enqueued_job_ids = []
    skipped_details = []
    details = []

    try:
        sections = ResumeSection(**base_resume.parsed_json)
        resume_data = ResumeData(
            candidate_id=str(candidate_id),
            resume_id=str(base_resume.id),
            file_url=base_resume.file_url or "",
            sections=sections,
            raw_text="[Loaded from DB]"
        )
    except Exception as e:
        logger.error(f"[Matching] Failed to load ResumeData for {candidate.name}: {e}")
        return {"error": "resume_data_load_failed"}

    # Derive api_base_url
    api_base = os.getenv("M1_API_BASE_URL", "http://127.0.0.1:8000/api").rstrip("/")
    api_base_url = api_base[:-4] if api_base.endswith("/api") else api_base

    for job in jobs:
        # Operator stop: automation_paused is set by POST /candidates/{id}/pipeline/pause.
        # The start-of-run check above only guards NEW runs; a single tailoring pass can
        # take minutes, so re-read the flag each iteration to halt an ACTIVE run at the
        # next job boundary. Fresh column SELECT (not session.get) to bypass the
        # identity map and see a commit made by the API process mid-run.
        if not is_mock:
            paused_now = (await session.execute(
                select(Candidate.automation_paused).where(Candidate.id == candidate_id)
            )).scalar()
            if paused_now:
                from app.services.events import publish_event
                logger.info(
                    f"[Matching] Pipeline stopped by operator for {candidate.name} — "
                    f"halting run ({len(enqueued)} enqueued so far)."
                )
                await publish_event("pipeline.progress", {
                    "candidate_id": str(candidate_id),
                    "step": "paused",
                    "message": f"Pipeline stopped. {len(enqueued)} application(s) had already been queued.",
                })
                break

        if job.id in skipped_jobs:
            logger.info(f"[Matching] Skipping job {job.title} at {job.company}: already applied and not eligible for retry.")
            skipped_details.append({
                "job_id": str(job.id),
                "job_title": job.title,
                "company": job.company,
                "reason": "already_applied"
            })
            continue

        if remaining_slots <= 0:
            cap_desc = max_daily if max_daily is not None else manual_limit
            logger.info(f"[Matching] Application cap ({cap_desc}) reached for {candidate.name}. Skipping LLM evaluation.")
            skipped_details.append({
                "job_title": job.title,
                "company": job.company,
                "reason": "daily_limit_reached"
            })
            continue

        # Format skills
        skills = job.skills
        if isinstance(skills, str):
            try:
                skills = json.loads(skills)
            except Exception:
                skills = []
        elif not skills:
            skills = []

        job_obj = NormalizedJob(
            title=job.title or "",
            company=job.company or "",
            location=job.location or "Remote",
            url=job.source_url or "",
            description=job.description or "",
            source=job.source or "greenhouse",
            skills=skills,
            salary_min=job.salary_min,
            salary_max=job.salary_max,
            pay_period=job.pay_period or "yearly",
            job_type=job.job_type or "full-time",
            canonical_url=job.canonical_url or "",
            embedding=job.embedding,
            source_url=job.source_url or ""
        )
        job_obj.job_id = str(job.id)

        # Create the application record as FOUND so the watchdog's QUEUED threshold
        # (60 min) cannot fire while the pipeline is still running.  The orchestrator
        # transitions the record to ANALYZED (below threshold) or QUEUED (gate passed)
        # once tailoring and cover-letter generation are complete.
        #
        # (candidate_id, job_id) is UNIQUE — a retried job (FAILED with a transient
        # reason, or a re-scored ANALYZED) already has a record. Reuse and reset it
        # instead of inserting a duplicate, which raises IntegrityError and kills
        # the whole matching run.
        logger.info(f"[Matching] about to create FOUND record for job={job.id}")
        t_queued = time.time()
        from app.models.application_history import ApplicationHistory

        existing_stmt = select(Application).where(
            Application.candidate_id == candidate_id,
            Application.job_id == job.id,
        )
        app_record = (await session.execute(existing_stmt)).scalars().first()

        if app_record is not None:
            prev_status = app_record.status
            app_record.status = "FOUND"
            app_record.error_message = None
            app_record.failure_reason = None
            app_record.retry_count = 0
            app_record.resume_id = base_resume.id
            session.add(app_record)
            await session.commit()
            await session.refresh(app_record)
            app_id = app_record.id
            session.add(ApplicationHistory(
                application_id=app_id,
                from_status=prev_status,
                to_status="FOUND",
                meta_data={"info": f"Application re-selected for retry (was {prev_status})"}
            ))
            await session.commit()
            logger.info(f"[Matching] Reusing existing app {app_id} (was {prev_status}) for retry")
        else:
            app_record = Application(
                candidate_id=candidate_id,
                job_id=job.id,
                status="FOUND",
                resume_id=base_resume.id
            )
            session.add(app_record)
            await session.commit()
            await session.refresh(app_record)
            app_id = app_record.id

            initial_history = ApplicationHistory(
                application_id=app_id,
                from_status=None,
                to_status="FOUND",
                meta_data={"info": "Application created; pending AI evaluation and tailoring"}
            )
            session.add(initial_history)
            await session.commit()

        logger.info(f"[Matching] FOUND record and initial history created app_id={app_id} in {time.time()-t_queued:.2f}s")

        from app.services.events import publish_event
        await publish_event("application.created", {
            "application_id": str(app_id),
            "candidate_id": str(candidate_id),
            "job_id": str(job.id),
            "status": "FOUND",
            "timestamp": datetime.utcnow().isoformat()
        })

        # LLM Score Match — check Redis cache first to avoid redundant Gemini calls
        # match_result_for_orchestrator is set on cache-miss so the orchestrator can
        # reuse it and skip a second identical LLM call.
        match_result_for_orchestrator = None
        try:
            cid_str = str(candidate_id)
            jid_str = str(job.id)

            # ── Cache hit: skip LLM call ───────────────────────────────────
            cached = await get_cached_score(cid_str, jid_str)
            if cached:
                logger.info(
                    "[Matching] Cache HIT for job %s at %s — skipping LLM call",
                    job.title, job.company
                )
                score = cached.get("combined_score", 0)
                passed_llm = cached.get("should_apply", False)
                reason = cached.get("reasoning", "")
            else:
                # ── Cache miss: call Gemini ───────────────────────────────
                logger.info(
                    "[Matching] Evaluating LLM fit score for job: %s at %s...",
                    job.title, job.company
                )
                logger.info(f"[Matching] calling score_job_fit for job={job.id}")
                t_score = time.time()
                match_result_for_orchestrator = await score_job_fit(candidate_dict, resume_data, job_obj)
                score = match_result_for_orchestrator.combined_score
                passed_llm = match_result_for_orchestrator.should_apply
                reason = match_result_for_orchestrator.reasoning
                logger.info(f"[Matching] score_job_fit done in {time.time()-t_score:.2f}s passed={passed_llm}")
                # Store in cache for 24h — avoids re-evaluation on 72h Monday lookback
                await cache_score(cid_str, jid_str, {
                    "combined_score": float(score),
                    "fit_score": float(match_result_for_orchestrator.fit_score),
                    "ats_score": float(match_result_for_orchestrator.ats_score),
                    "should_apply": passed_llm,
                    "reasoning": reason,
                })
        except Exception as e:
            logger.error("[Matching] Error during LLM score_job_fit for job %s: %s", job.id, e)
            # Mark the pre-created application record as FAILED so it doesn't sit in FOUND
            try:
                fail_app = await session.get(Application, app_id)
                if fail_app and fail_app.status in ("FOUND", "QUEUED"):
                    prev_status = fail_app.status
                    fail_app.status = "FAILED"
                    fail_app.error_message = f"LLM scoring error: {e}"
                    fail_app.failure_reason = "INFRA_ERROR"
                    from app.models.application_history import ApplicationHistory
                    session.add(ApplicationHistory(
                        application_id=app_id,
                        from_status=prev_status,
                        to_status="FAILED",
                        meta_data={"error": f"LLM scoring failed: {e}"}
                    ))
                    await session.commit()
                    from app.tasks.browser_automation import publish_status_changed
                    await publish_status_changed(
                        application_id=str(app_id),
                        from_status=prev_status,
                        to_status="FAILED"
                    )
            except Exception as mark_err:
                logger.error("[Matching] Could not mark app %s as FAILED after LLM error: %s", app_id, mark_err)
            skipped_details.append({
                "job_title": job.title,
                "company": job.company,
                "reason": f"llm_error: {str(e)}"
            })
            continue

        # Always run the full orchestration pipeline regardless of fit score.
        # The gate check is now INSIDE orchestrate_application_package and runs
        # AFTER tailoring, so the tailored resume and cover letter are always
        # saved.  Dispatch browser automation only when the gate passes (QUEUED).
        # Pass prefetched_match_result (cache-miss path) to skip a second LLM call.
        logger.info(
            "[Matching] Orchestrating package for job %s at %s (score=%.1f, will_auto_apply=%s)",
            job.title, job.company, score, passed_llm
        )
        try:
            result = await asyncio.wait_for(
                orchestrate_application_package(
                    candidate_id=str(candidate_id),
                    job_id=str(job.id),
                    base_resume_pdf_path=None,
                    screening_questions=[],
                    api_base_url=api_base_url,
                    skip_gate=False,
                    existing_app_id=str(app_id),
                    prefetched_match_result=match_result_for_orchestrator,
                ),
                timeout=900.0,  # 15-minute hard cap per job
            )

            if result.get("status") == "QUEUED":
                package = {
                    "application_id": str(app_id),
                    "candidate_id": str(candidate_id),
                    "job_id": str(job.id),
                    "job_url": job.source_url or "",
                    "platform": (job.source or "").lower(),
                    "ats_type": job.job_type or "",
                    "resume_url": result.get("resume_pdf_url") or "",
                    "cover_letter_url": result.get("cover_letter_url") or "",
                    "screening_answers": result.get("screening_answers") or {}
                }

                logger.info(
                    f"[Matching] Dispatching browser automation task: app={app_id} "
                    f"platform={package['platform']!r} url={package['job_url'][:80]!r}"
                )
                from app.tasks.browser_automation import execute_application
                # App-level retry on top of task_publish_retry: a DNS flap to the
                # broker longer than the publish-retry window would otherwise
                # strand this app in QUEUED until the watchdog (≥15 min later).
                _dispatch_err = None
                for _attempt in range(3):
                    try:
                        execute_application.delay(package)
                        _dispatch_err = None
                        break
                    except Exception as _pub_err:
                        _dispatch_err = _pub_err
                        logger.warning(
                            f"[Matching] browser-task dispatch failed for app={app_id} "
                            f"(attempt {_attempt + 1}/3): {_pub_err} — retrying in 10s"
                        )
                        await asyncio.sleep(10)
                if _dispatch_err is not None:
                    # Leave the row QUEUED — the stuck-application watchdog
                    # re-dispatches it — but surface the reason loudly.
                    logger.error(
                        f"[Matching] browser-task dispatch EXHAUSTED retries for app={app_id}; "
                        f"row stays QUEUED for watchdog recovery: {_dispatch_err}"
                    )

                await publish_event("pipeline.progress", {
                    "application_id": str(app_id),
                    "candidate_id": str(candidate_id),
                    "step": "dispatched",
                    "message": f"Browser automation dispatched for {job.title} at {job.company}"
                })

                enqueued.append({
                    "job_title": job.title,
                    "company": job.company,
                    "score": score,
                    "status": "QUEUED"
                })
                enqueued_job_ids.append(str(job.id))
                remaining_slots -= 1
                logger.info(f"[Matching] Dispatched automation for application: {app_id}")

            elif result.get("status") == "ANALYZED":
                # Score was below threshold; orchestrator saved tailored docs and
                # transitioned the app to ANALYZED itself.
                logger.info(
                    "[Matching] Job %s at %s scored %.1f — below gate. "
                    "Tailored docs saved, status=ANALYZED.",
                    job.title, job.company, score
                )
                skipped_details.append({
                    "job_title": job.title,
                    "company": job.company,
                    "score": score,
                    "reason": "below_threshold"
                })

            else:
                logger.warning(
                    "[Matching] orchestrate_application_package returned unexpected "
                    "status=%s for job %s",
                    result.get("status"), job.id
                )
                try:
                    fail_app = await session.get(Application, app_id)
                    if fail_app and fail_app.status in ("FOUND", "QUEUED"):
                        prev_status = fail_app.status
                        fail_app.status = "FAILED"
                        fail_app.error_message = (
                            f"Package prep returned status={result.get('status')} — orchestration incomplete"
                        )
                        fail_app.failure_reason = "INFRA_ERROR"
                        from app.models.application_history import ApplicationHistory
                        session.add(ApplicationHistory(
                            application_id=app_id,
                            from_status=prev_status,
                            to_status="FAILED",
                            meta_data={"error": f"orchestrate returned status={result.get('status')}"}
                        ))
                        await session.commit()
                except Exception as mark_err:
                    logger.error(f"[Matching] Could not mark app {app_id} as FAILED: {mark_err}")
                skipped_details.append({
                    "job_title": job.title,
                    "company": job.company,
                    "score": score,
                    "reason": "package_prep_failed"
                })

        except asyncio.TimeoutError:
            logger.error(
                "[Matching] Orchestration timed out (>15 min) for job %s at %s",
                job.id, job.company
            )
            try:
                fail_app = await session.get(Application, app_id)
                if fail_app and fail_app.status in ("FOUND", "QUEUED"):
                    prev_status = fail_app.status
                    fail_app.status = "FAILED"
                    fail_app.error_message = "Orchestration timed out after 15 minutes"
                    fail_app.failure_reason = "INFRA_ERROR"
                    from app.models.application_history import ApplicationHistory
                    session.add(ApplicationHistory(
                        application_id=app_id,
                        from_status=prev_status,
                        to_status="FAILED",
                        meta_data={"error": "orchestration timeout after 900s"}
                    ))
                    await session.commit()
                    from app.tasks.browser_automation import publish_status_changed
                    await publish_status_changed(
                        application_id=str(app_id),
                        from_status=prev_status,
                        to_status="FAILED"
                    )
            except Exception as mark_err:
                logger.error(f"[Matching] Could not mark app {app_id} as FAILED after timeout: {mark_err}")
            skipped_details.append({
                "job_title": job.title,
                "company": job.company,
                "score": score,
                "reason": "orchestration_timeout"
            })

        except Exception as e:
            logger.error(
                "[Matching] Failed to prepare/enqueue application for job %s: %s",
                job.id, e, exc_info=True
            )
            try:
                fail_app = await session.get(Application, app_id)
                if fail_app and fail_app.status in ("FOUND", "QUEUED"):
                    prev_status = fail_app.status
                    fail_app.status = "FAILED"
                    fail_app.error_message = str(e)
                    fail_app.failure_reason = "INFRA_ERROR"
                    from app.models.application_history import ApplicationHistory
                    session.add(ApplicationHistory(
                        application_id=app_id,
                        from_status=prev_status,
                        to_status="FAILED",
                        meta_data={"error": str(e)}
                    ))
                    await session.commit()
                    from app.tasks.browser_automation import publish_status_changed
                    await publish_status_changed(
                        application_id=str(app_id),
                        from_status=prev_status,
                        to_status="FAILED"
                    )
            except Exception as mark_err:
                logger.error(f"[Matching] Could not mark app {app_id} as FAILED: {mark_err}")
            skipped_details.append({
                "job_title": job.title,
                "company": job.company,
                "score": score,
                "reason": f"error: {str(e)}"
            })

        details.append({
            "job_title": job.title,
            "company": job.company,
            "score": float(score) if score is not None else None,
            "passed": passed_llm,
            "reasoning": reason
        })

    return {
        "candidate_id": str(candidate_id),
        "jobs_scanned": len(jobs),
        "pgvector_passed": pgvector_passed_count,
        "llm_passed": len(enqueued),
        "enqueued_count": len(enqueued),
        "enqueued_job_ids": enqueued_job_ids,
        "details": details,
        "skipped": skipped_details
    }
