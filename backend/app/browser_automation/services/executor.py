import os
import time
import logging
import asyncio
import tempfile
import httpx
import traceback
import redis.asyncio as redis
from urllib.parse import urlparse, unquote

from typing import Optional, Dict, Literal
from playwright.async_api import async_playwright, Page, BrowserContext
from dotenv import load_dotenv

from ..browser import BrowserContextManager
from ..adapters import get_adapter, BasePlatformAdapter
from ..forms import detect_form, fill_form, fill_form_with_llm
from ..captcha import CaptchaService
from ..agent import AgentLoop, LoopResult, PageAgent, diagnose_failure, get_learned_fixes

from .models import ApplicationPackage, ApplicationResult
from .screenshot import capture_and_store_screenshot
from .state_machine import transition_status

load_dotenv()
logger = logging.getLogger(__name__)


async def is_job_url_active(url: str) -> bool:
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            resp = await client.get(url, headers=headers)
            
            if resp.status_code in (404, 410):
                logger.info(f"[Job Check] Job URL returned status {resp.status_code}: {url}")
                return False
                
            final_url = str(resp.url)
            
            if final_url != url:
                parsed_orig = urlparse(url)
                parsed_final = urlparse(final_url)
                
                orig_path = parsed_orig.path.strip("/")
                final_path = parsed_final.path.strip("/")
                
                orig_segments = [s for s in orig_path.split("/") if s]
                final_segments = [s for s in final_path.split("/") if s]
                
                job_id_segments = [s for s in orig_segments if s.isdigit() or len(s) > 8]
                if job_id_segments:
                    if not any(jid in final_url for jid in job_id_segments):
                        logger.info(f"[Job Check] Redirected away from job URL: {url} -> {final_url}")
                        return False
                else:
                    if len(final_segments) < len(orig_segments) and (not final_path or "jobs" not in final_path or final_path == "jobs" or final_path == "careers"):
                        logger.info(f"[Job Check] Redirected away to homepage or directory: {url} -> {final_url}")
                        return False
            
            text = resp.text.lower()
            expired_patterns = [
                "job no longer available",
                "position is no longer available",
                "job posting has been removed",
                "no longer accepting applications",
                "this job has expired",
                "job is no longer active",
                "position has been filled",
                "job posting is no longer active",
                "the job you are looking for has been filled",
                "this listing has expired",
            ]
            if any(p in text for p in expired_patterns):
                logger.info(f"[Job Check] Job page contains expired text patterns: {url}")
                return False
                
            return True
    except Exception as e:
        logger.warning(f"Lightweight job check failed for {url}: {e}")
        return True


async def check_page_indicates_expired(page: Page, original_url: str) -> bool:
    try:
        final_url = page.url
        if final_url != original_url:
            parsed_orig = urlparse(original_url)
            parsed_final = urlparse(final_url)
            
            orig_path = parsed_orig.path.strip("/")
            final_path = parsed_final.path.strip("/")
            
            orig_segments = [s for s in orig_path.split("/") if s]
            final_segments = [s for s in final_path.split("/") if s]
            
            job_id_segments = [s for s in orig_segments if s.isdigit() or len(s) > 8]
            
            if job_id_segments:
                if not any(jid in final_url for jid in job_id_segments):
                    logger.info(f"[Browser Check] Page redirected away: {original_url} -> {final_url}")
                    return True
            else:
                if len(final_segments) < len(orig_segments) and (not final_path or "jobs" not in final_path or final_path == "jobs" or final_path == "careers"):
                    logger.info(f"[Browser Check] Page redirected away to directory/homepage: {original_url} -> {final_url}")
                    return True

        title = (await page.title()).lower()
        if "404" in title or "page not found" in title or "job not found" in title:
            logger.info(f"[Browser Check] Page title indicates not found: {title}")
            return True
            
        content = (await page.content()).lower()
        expired_patterns = [
            "job no longer available",
            "position is no longer available",
            "job posting has been removed",
            "no longer accepting applications",
            "this job has expired",
            "job is no longer active",
            "position has been filled",
            "job posting is no longer active",
            "the job you are looking for has been filled",
            "this listing has expired",
        ]
        if any(p in content for p in expired_patterns):
            logger.info(f"[Browser Check] Page content indicates expired/removed")
            return True
            
    except Exception as e:
        logger.warning(f"Error checking if page indicates expired: {e}")
    return False


class RateLimiter:
    def __init__(self):
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._redis = None

        # Per-platform hourly limits (conservative defaults)
        self._limits: Dict[str, int] = {
            "linkedin":   10,
            "indeed":     20,
            "greenhouse": 30,
            "lever":      30,
            "ashby":      30,
        }
        self._default_limit = 25

    async def _get_redis(self):
        if self._redis is None:
            kwargs = {}
            if "rediss://" in self.redis_url:
                kwargs["ssl_cert_reqs"] = "none"
            self._redis = redis.from_url(self.redis_url, **kwargs)
        return self._redis

    async def check_and_increment(self, platform: str, candidate_id: str) -> bool:
        r = await self._get_redis()
        key = f"rate_limit:{candidate_id}:{platform}"
        limit = self._limits.get(platform.lower(), self._default_limit)
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, 3600)
        if count > limit:
            logger.warning(f"Rate limit exceeded for {candidate_id}@{platform} (count={count}, limit={limit})")
            return False
        return True


# ─────────────────────────────────────────────────────────────────────────────
# File resolution helpers
# ─────────────────────────────────────────────────────────────────────────────

def _supabase_auth_headers() -> dict:
    """Return Supabase auth headers if credentials are available, else empty dict.

    The resume/cover-letter buckets are private (not public in Supabase Storage
    settings), so downloads from /object/public/... return 400 without auth.
    We reuse the same anon key the uploader (module3/utils/storage.py) uses.
    """
    def _read_env_file(path: str, key: str) -> str:
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith(f"{key}="):
                        return line.strip().split("=", 1)[1]
        except Exception:
            pass
        return ""

    # Prefer env var, fall back to frontend .env files (same as storage.py does)
    anon_key = (
        os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY")
        or _read_env_file("frontend/.env.local", "NEXT_PUBLIC_SUPABASE_ANON_KEY")
        or _read_env_file("frontend/.env", "NEXT_PUBLIC_SUPABASE_ANON_KEY")
    )
    if not anon_key:
        return {}
    return {
        "Authorization": f"Bearer {anon_key}",
        "apikey": anon_key,
    }


async def _resolve_file_to_local_path(url_or_path: str, suffix: str = ".pdf") -> Optional[str]:
    """Return a local filesystem path for the given URL or path.

    - If it's already a valid local path → return as-is.
    - If it's an HTTP(S) URL → download to a temp file and return the path.
      Supabase storage URLs are downloaded with auth headers because the
      resume/cover-letter buckets are private.
      The temp file is NOT cleaned up here; the caller is responsible.
    - If resolution fails → return None.
    """
    if not url_or_path:
        return None

    if not url_or_path.startswith(("http://", "https://")):
        if os.path.isfile(url_or_path):
            return url_or_path
        logger.error(f"Local file not found: {url_or_path}")
        return None

    # Add Supabase auth headers for private-bucket URLs
    headers = {}
    if "supabase.co/storage" in url_or_path:
        headers = _supabase_auth_headers()
        if not headers:
            logger.warning("[M4] Supabase URL detected but no anon key found — download may fail")

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url_or_path, headers=headers)
            resp.raise_for_status()

        filename = None
        parsed = urlparse(url_or_path)
        if parsed.path:
            filename = os.path.basename(parsed.path)
        if not filename:
            filename = os.path.basename(unquote(parsed.path))
        if not filename:
            filename = f"downloaded{suffix}"
        if not filename.lower().endswith(suffix.lower()):
            filename += suffix

        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, filename)
        with open(temp_path, "wb") as f:
            f.write(resp.content)

        logger.info(f"Downloaded {url_or_path} → {temp_path}")
        return temp_path
    except Exception as exc:
        logger.error(f"Failed to download {url_or_path}: {exc}")
        return None


def _cleanup_temp(*paths: Optional[str]) -> None:
    for p in paths:
        if p and p.startswith(tempfile.gettempdir()):
            try:
                os.remove(p)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Main executor
# ─────────────────────────────────────────────────────────────────────────────

class ApplicationExecutor:
    async def execute(self, package: ApplicationPackage, retry_count: int = 0) -> ApplicationResult:
        start_time = time.monotonic()
        screenshot_path: Optional[str] = None
        confirmation_text: Optional[str] = None
        error_message: Optional[str] = None
        status: Literal["SUBMITTED", "FORM_COMPLETED", "FAILED", "CAPTCHA_FAILED", "RATE_LIMITED", "BLOCKED"] = "FAILED"

        context_mgr: Optional[BrowserContextManager] = None
        context: Optional[BrowserContext] = None
        page: Optional[Page] = None

        # Temp paths created by this executor that must be cleaned up at the end
        _temp_resume: Optional[str] = None
        _temp_cover:  Optional[str] = None

        def _elapsed() -> float:
            return time.monotonic() - start_time

        try:
            # Check platform review status
            from .platform_review import is_platform_flagged
            if is_platform_flagged(package.platform):
                raise Exception(f"PLATFORM_NEEDS_REVIEW: Platform {package.platform} is flagged as needing review due to previous failures.")

            # ── PRE-FLIGHT: validate resume exists before launching browser ──
            if not package.resume_url:
                raise ValueError("ApplicationPackage.resume_url is empty — cannot proceed")

            _temp_resume = await _resolve_file_to_local_path(package.resume_url, ".pdf")
            if not _temp_resume:
                raise FileNotFoundError(f"Resume file could not be resolved: {package.resume_url}")

            _temp_cover: Optional[str] = None
            if package.cover_letter_url:
                _temp_cover = await _resolve_file_to_local_path(package.cover_letter_url, ".pdf")
                if not _temp_cover:
                    logger.warning(f"Cover letter could not be resolved ({package.cover_letter_url}); "
                                   "continuing without it")

            logger.info(f"[M4] Pre-flight OK — resume={_temp_resume}, "
                        f"cover_letter={_temp_cover or 'N/A'}")

            # ── PRE-FLIGHT 2: check if job exists ──
            job_url_active = True
            try:
                job_url_active = await is_job_url_active(package.job_url)
            except Exception as e:
                logger.warning(f"Error checking job URL existence: {e}")
                
            if not job_url_active:
                raise Exception("JOB_EXPIRED: Job posting no longer exists or has been removed")

            # ── STEP 1: Rate limit ──
            rate_limiter = RateLimiter()
            if not await rate_limiter.check_and_increment(package.platform, package.candidate_id):
                return ApplicationResult(
                    application_id=package.application_id,
                    status="RATE_LIMITED",
                    execution_time_seconds=_elapsed(),
                    retry_count=retry_count,
                    error_message="Rate limit exceeded",
                )

            # ── STEP 2: Get platform adapter ──
            adapter: BasePlatformAdapter = get_adapter(package.platform)

            # ── STEP 3: Browser context ──
            context_mgr = BrowserContextManager()
            context = await context_mgr.get_context(package.candidate_id, package.platform)
            page = await context.new_page()

            # Apply playwright-stealth to mask automation signals before any navigation
            # (navigator.webdriver, chrome.runtime, permissions API, WebGL, etc.)
            try:
                from playwright_stealth import Stealth
                await Stealth().apply_stealth_async(page)
                logger.info("[M4] playwright-stealth v2 applied")
            except Exception as exc:
                logger.debug(f"[M4] playwright-stealth unavailable: {exc}")

            # ── STEP 4: Transition APPLICATION_STARTED ──
            await transition_status(package.application_id, "APPLICATION_STARTED")

            # ── STEP 5: Navigate ──
            # Expose the candidate profile to the adapter for adapters that own
            # a deterministic pre-form gate (e.g. Talent.com fills the email +
            # clicks Continue to trigger the OTP before the AgentLoop takes
            # over). Duck-typed + backward-compatible — adapters that don't read
            # it simply ignore the attribute.
            try:
                adapter.candidate_profile = package.candidate_profile
            except Exception:
                pass
            await adapter.navigate_to_application(page, package.job_url)

            if await check_page_indicates_expired(page, package.job_url):
                raise Exception("JOB_EXPIRED: Job posting no longer exists or has been removed")

            # ── STEP 5.5: Vision page-agent oversight ─────────────────────
            # After navigation, ask Gemini what page state we landed on. If
            # the adapter missed an Apply button (we landed on a LISTING), the
            # agent can find it from a screenshot + DOM and click — and the
            # selector is learned for next time.
            use_agent = os.getenv("USE_PAGE_AGENT", "true").lower() == "true"
            if use_agent:
                try:
                    page_agent = PageAgent(ats=package.platform)
                    frame_loc = getattr(adapter, "_frame_locator", None) or getattr(adapter, "_frame", None)
                    state = await page_agent.classify_page(
                        page, frame=frame_loc,
                    )
                    logger.info(
                        f"[Agent] page_state={state.kind} confidence={state.confidence:.2f} "
                        f"reason={state.reason!r}"
                    )
                    if state.kind == "BLOCKED":
                        raise RuntimeError(f"BLOCKED: vision-agent flagged page state: {state.reason}")
                    if state.kind == "LISTING" and state.next_action == "click_apply":
                        # Try the agent's suggested selector first, then any learned ones
                        candidates: list = []
                        if state.suggested_selector:
                            candidates.append(state.suggested_selector)
                        candidates.extend(get_learned_fixes(package.platform).get("apply_button"))
                        clicked_via: Optional[str] = None
                        for sel in candidates:
                            try:
                                loc = page.locator(sel).first
                                if await loc.count() > 0 and await loc.is_visible():
                                    await loc.scroll_into_view_if_needed()
                                    await loc.click(timeout=6000)
                                    clicked_via = sel
                                    break
                            except Exception:
                                continue
                        if not clicked_via:
                            clicked_via = await page_agent.find_and_click(
                                page, "apply_button", candidates,
                                frame=frame_loc,
                            )
                        if clicked_via:
                            get_learned_fixes(package.platform).add("apply_button", clicked_via)
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                            except Exception:
                                pass
                except RuntimeError:
                    raise
                except Exception as exc:
                    logger.warning(f"[Agent] classify/click-apply failed (non-fatal): {exc}")

            # Always refresh the frame before starting AgentLoop because cross-origin 
            # navigations or delayed iframe loads might have detached the old Frame object.
            if hasattr(adapter, 'refresh_frame'):
                try:
                    await adapter.refresh_frame(page)
                except Exception as exc:
                    logger.warning(f"[M4] Failed to refresh frame before AgentLoop: {exc}")

            # ── STEP 5.7: AgentLoop — autonomous vision-driven fill + submit ──────────
            # Runs BEFORE the scripted pipeline. On success the scripted steps 6-10
            # are skipped entirely. On wrong-page / explicit abort the application is
            # halted immediately. On any other failure we fall through to the existing
            # deterministic pipeline so the run never blocks on the AI.
            _agent_submitted = False
            _agent_confirmation: Optional[str] = None

            use_agent_loop = os.getenv("USE_AGENT_LOOP", "true").lower() == "true"
            if use_agent_loop:
                try:
                    # Pass the FULL job package so the AgentLoop can anchor the AI
                    # on the actual role/company. job_description is truncated to keep
                    # the system prompt tight; the AI only needs enough context to
                    # answer "why this role?" type screening questions.
                    job_desc = (package.job_description or "")[:1200]
                    # Passthrough/aggregator adapters (RemoteRocketship, Indeed
                    # external-apply) resolve an INNER ATS during navigation. The
                    # real form belongs to that inner ATS, so the AgentLoop must
                    # get the inner ATS's hints (e.g. Greenhouse react-select
                    # quirks) — not the aggregator's thin cheat-sheet. Without
                    # this, RR jobs route correctly but the AI flies blind.
                    effective_platform = package.platform
                    _inner = getattr(adapter, "_inner", None)
                    if _inner is not None and getattr(_inner, "platform_name", None):
                        effective_platform = _inner.platform_name
                        logger.info(
                            f"[M4] Passthrough adapter resolved inner platform="
                            f"{effective_platform!r} — using its hints for the AgentLoop"
                        )
                    job_ctx_for_loop = {
                        "platform": effective_platform,
                        "ats_type": package.ats_type or effective_platform,
                        "job_url": package.job_url,
                        "job_title": package.job_title or "",
                        "company": package.company or "",
                        "job_description": job_desc,
                    }
                    # Screening answers pre-resolved by M3 — pass to AgentLoop so the
                    # AI uses M3's answers verbatim instead of re-inventing them.
                    pre_answers = dict(package.screening_answers or {})
                    # DRY_RUN_NO_SUBMIT must be honored in AgentLoop mode too —
                    # the AI drives the final submit itself, so without this flag
                    # a "dry run" would still file a real application. When set,
                    # the loop fills + validates the form then returns SUBMITTED
                    # with confirmation="dry_run_stopped_before_submit" WITHOUT
                    # clicking the real Submit button.
                    dry_run = os.getenv("DRY_RUN_NO_SUBMIT", "false").lower() == "true"
                    agent_loop = AgentLoop(
                        candidate_profile=package.candidate_profile,
                        job_context=job_ctx_for_loop,
                        resume_path=_temp_resume,
                        cover_letter_path=_temp_cover,
                        screening_answers=pre_answers,
                        candidate_id=package.candidate_id,
                        stop_before_submit=dry_run,
                    )
                    frame_loc = getattr(adapter, "_frame_locator", None) or getattr(adapter, "_frame", None)
                    loop_result: LoopResult = await agent_loop.run(
                        page, frame=frame_loc
                    )
                    logger.info(
                        f"[M4] AgentLoop finished: status={loop_result.status} "
                        f"steps={loop_result.steps_taken} error={loop_result.error!r}"
                    )

                    if loop_result.status == "SUBMITTED":
                        _agent_submitted = True
                        _agent_confirmation = loop_result.confirmation
                        await transition_status(package.application_id, "FORM_COMPLETED")

                    elif loop_result.status in ("WRONG_PAGE", "ABORTED"):
                        raise Exception(f"AgentLoop aborted: {loop_result.error}")

                    else:
                        # MAX_STEPS / STUCK / LLM_UNAVAILABLE / ERROR — fall back
                        logger.warning(
                            f"[M4] AgentLoop non-terminal ({loop_result.status}) — "
                            "falling back to scripted pipeline"
                        )

                except Exception as exc:
                    if "AgentLoop aborted" in str(exc):
                        raise  # propagate intentional aborts
                    logger.warning(f"[M4] AgentLoop raised (non-fatal, falling back): {exc}")

            if not _agent_submitted:
                # ── STEP 6: Detect form (done inside fill_application; get ref for screening) ──
                if hasattr(adapter, 'refresh_frame'):
                    try:
                        await adapter.refresh_frame(page)
                    except Exception as exc:
                        logger.warning(f"[M4] Failed to refresh frame before detect_form: {exc}")
                
                from ..frame_utils import get_live_frame
                frame_loc = getattr(adapter, "_frame_locator", None) or getattr(adapter, "_frame", None)
                if getattr(adapter, '_iframe_mode', False):
                    ctx = await get_live_frame(frame_loc)
                else:
                    ctx = page
                    
                form = await detect_form(ctx, container_selector=adapter.container_selector)

                # ── STEP 6.5: Call M3 only for screening answers (URLs already resolved) ──
                #
                # We already have the tailored resume and cover letter from the M3 event.
                # We call M3's prepare-package endpoint ONLY to obtain answers to any
                # screening questions found in the form that weren't pre-answered.
                # We NEVER overwrite resume_url or cover_letter_url if they are already set.
                #
                # IMPORTANT: We do NOT check should_apply here. The fit-score gate already
                # ran in M3 upstream (orchestrator). By the time we are executing in M4 the
                # application is already in QUEUED/MATCHED status — re-checking the gate
                # here would cause M4 to abandon applications that were intentionally queued.
                screening_answers: Dict[str, str] = dict(package.screening_answers or {})

                exclude_kw = {"first name", "last name", "email", "phone", "resume", "cover letter", "cv"}
                open_questions = [
                    field.label for field in form.fields
                    if field.field_type in ("text", "textarea", "select", "radio", "checkbox")
                    and not any(kw in field.label.lower() for kw in exclude_kw)
                    and field.label.lower() not in {q.lower() for q in screening_answers}
                ]
                needs_cover_letter = any(
                    "cover" in f.label.lower() for f in form.fields if f.field_type == "file"
                )

                if open_questions:
                    api_base = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api")
                    try:
                        async with httpx.AsyncClient(timeout=300.0) as client:
                            resp = await client.post(
                                f"{api_base}/applications/prepare-package",
                                json={
                                    "candidate_id": package.candidate_id,
                                    "job_id": package.job_id,
                                    "needs_cover_letter": needs_cover_letter and (_temp_cover is None),
                                    "screening_questions": open_questions,
                                },
                            )
                        if resp.status_code == 200:
                            m3 = resp.json()
                            logger.info(f"[M4] M3 prepare-package answered {len(open_questions)} question(s)")

                            # Only adopt cover letter URL from M3 if we don't already have one
                            if not _temp_cover and m3.get("cover_letter_pdf_url"):
                                _temp_cover = await _resolve_file_to_local_path(
                                    m3["cover_letter_pdf_url"], ".pdf"
                                )

                            # Merge M3 screening answers (don't overwrite already-answered ones)
                            for q, a in (m3.get("screening_answers") or {}).items():
                                if q not in screening_answers:
                                    screening_answers[q] = a

                            # NOTE: should_apply is intentionally NOT checked here.
                            # The fit-gate already ran in M3. If we are here the job
                            # was already queued — we must proceed regardless of score.
                            if not m3.get("should_apply", True):
                                logger.info(
                                    "[M4] M3 prepare-package returned should_apply=False "
                                    "(score below gate threshold) — continuing anyway since "
                                    "application was already queued by upstream M3 pipeline."
                                )
                        else:
                            logger.warning(f"[M4] M3 prepare-package returned {resp.status_code}: {resp.text[:200]}")
                    except Exception as exc:
                        logger.warning(f"[M4] M3 prepare-package call failed (non-fatal): {exc}")

                # ── STEP 7: Fill form ──
                # If USE_LLM_FILLER=true, drive the fill through Gemini first. On
                # LLM failure we fall back to the deterministic adapter path so the
                # pipeline never blocks on the AI.
                use_llm_fill = os.getenv("USE_LLM_FILLER", "true").lower() == "true"
                fill_ctx = getattr(adapter, "_frame_locator", None) or getattr(adapter, "_frame", None) if getattr(adapter, "_iframe_mode", False) else page
                fill_success = False
                if use_llm_fill:
                    try:
                        job_ctx = {
                            "platform": package.platform,
                            "ats_type": package.ats_type,
                            "job_url": package.job_url,
                            "job_title": getattr(package, "job_title", ""),
                            "job_description": getattr(package, "job_description", ""),
                        }
                        fill_success = await fill_form_with_llm(
                            fill_ctx,
                            form,
                            package.candidate_profile,
                            screening_answers=screening_answers,
                            job_context=job_ctx,
                            resume_path=_temp_resume,
                            cover_letter_path=_temp_cover,
                            candidate_id=package.candidate_id,
                        )
                        logger.info(f"[M4] LLM filler returned success={fill_success}")
                    except Exception as exc:
                        logger.warning(f"[M4] LLM filler raised — falling back to adapter: {exc}")
                        fill_success = False
                if not fill_success:
                    # Adapter path includes file uploads and re-scans (e.g. Greenhouse)
                    fill_success = await adapter.fill_application(
                        page,
                        package.candidate_profile,
                        _temp_resume,
                        _temp_cover,
                        screening_answers,
                        pre_detected_form=form,
                        candidate_id=package.candidate_id,
                    )
                # End-of-fill DOM snapshot — read all react-select rendered values.
                # Reports the actual visible state for debugging. ALSO retries any
                # dropdowns that ended up empty using the in-place commit code, so
                # this is the FINAL safety net before submission.
                try:
                    from ..frame_utils import get_live_frame
                    frame_loc = getattr(adapter, "_frame_locator", None) or getattr(adapter, "_frame", None)
                    ctx_for_final = (await get_live_frame(frame_loc)) if getattr(adapter, "_iframe_mode", False) else page
                    final_state = await ctx_for_final.evaluate(
                        """() => {
                            const out = [];
                            document.querySelectorAll('.select__control').forEach(ctrl => {
                                // Skip intl-tel-input phone-prefix widget — not a form field
                                if (ctrl.closest('.iti, .iti__country-list, .iti--container')) return;
                                const sv = ctrl.querySelector('.select__single-value');
                                const ph = ctrl.querySelector('.select__placeholder');
                                const inp = ctrl.querySelector('[role=combobox], input');
                                const lblEl = (() => {
                                    if (!inp) return null;
                                    const ll = inp.getAttribute('aria-labelledby');
                                    return ll ? document.getElementById(ll) : null;
                                })();
                                out.push({
                                    id: inp ? inp.id : '',
                                    label: lblEl ? lblEl.textContent.trim().slice(0,80) : '?',
                                    value: sv ? sv.textContent.trim() : '',
                                    placeholder: ph ? ph.textContent.trim() : '',
                                });
                            });
                            return out;
                        }"""
                    )
                    logger.info("[M4] Final dropdown state after all fill passes:")
                    for d in final_state or []:
                        display = d['value'] if d['value'] else f"<PLACEHOLDER>{d['placeholder']}"
                        logger.info(f"      {d['label'][:60]!r:65} = {display!r}")

                    # Final file-upload snapshot: Greenhouse REPLACES the
                    # <input type=file> with a filename-display element after a
                    # successful upload. We look for [class*=filename] / .file-
                    # attachment-name as evidence that the files are committed.
                    attached = await ctx_for_final.evaluate(
                        """() => {
                            const out = [];
                            document.querySelectorAll(
                                '.file-attachment-name, .attachment-name, '
                                + '[class*="filename"], [class*="uploaded-file"]'
                            ).forEach(el => {
                                if (el.offsetParent && el.textContent.trim()) {
                                    out.push(el.textContent.trim().slice(0, 80));
                                }
                            });
                            return out;
                        }"""
                    )
                    if attached:
                        logger.info(f"[M4] Files attached (visible in UI): {attached}")
                    else:
                        logger.info("[M4] No filename evidence found in UI — uploads may have silently failed")
                except Exception as exc:
                    logger.debug(f"[M4] final-state snapshot failed: {exc}")

                if not fill_success:
                    # Diagnose what went wrong before we bail. The result is written
                    # to learned_fixes/ for the next run.
                    try:
                        from pathlib import Path as _P
                        adapter_module_path = _P(__file__).resolve().parents[1] / "adapters" / f"{package.platform.lower()}.py"
                        frame_loc = getattr(adapter, "_frame_locator", None) or getattr(adapter, "_frame", None)
                        await diagnose_failure(
                            page=page,
                            ats=package.platform,
                            action="fill_form",
                            failure_reason="one or more required fields could not be filled",
                            adapter_source_path=adapter_module_path if adapter_module_path.exists() else None,
                            log_tail=traceback.format_exc(),
                            frame=frame_loc,
                        )
                    except Exception as exc:
                        logger.warning(f"[M4] diagnose_failure non-fatal exception: {exc}")
                    raise Exception("Form fill incomplete — one or more required fields could not be filled")

                # ── STEP 8: Captcha ──
                dry_run = os.getenv("DRY_RUN_NO_SUBMIT", "false").lower() == "true"
                provider = os.getenv("CAPTCHA_PROVIDER", "2captcha").lower()
                raw_key = os.getenv(
                    "TWO_CAPTCHA_API_KEY" if provider == "2captcha" else
                    "ANTI_CAPTCHA_API_KEY" if provider == "anticaptcha" else
                    "OCILAR_API_KEY",
                    "",
                )
                key_configured = bool(raw_key) and not raw_key.lower().startswith("your_")

                if form.has_captcha:
                    if dry_run or not key_configured:
                        logger.warning(
                            f"[M4] Captcha detected ({form.captcha_type}) — skipping solve "
                            f"(dry_run={dry_run}, solver_configured={key_configured})"
                        )
                    else:
                        captcha_svc = CaptchaService(provider=provider)
                        solution = await captcha_svc.solve(page, form.captcha_type)
                        if not solution.success:
                            status = "CAPTCHA_FAILED"
                            error_message = f"Captcha solving exhausted all attempts: {form.captcha_type}"
                            screenshot_path = await capture_and_store_screenshot(page, package.application_id)
                            logger.error(f"[M4] {error_message}")
                            _cleanup_temp(_temp_resume, _temp_cover)
                            return ApplicationResult(
                                application_id=package.application_id,
                                status=status,
                                screenshot_url=screenshot_path,
                                error_message=error_message,
                                execution_time_seconds=_elapsed(),
                                retry_count=retry_count,
                            )

                # ── STEP 9: FORM_COMPLETED ──
                await transition_status(package.application_id, "FORM_COMPLETED")

                # ── STEP 10: Submit ──
                if dry_run:
                    logger.info("[DRY RUN] Skipping submission click")
                    submitted = True
                    verified = True
                    confirmation_text = "DRY RUN SUCCESS (no submit)"
                else:
                    submitted = await adapter.submit(page)
                    if not submitted and use_agent:
                        # Adapter selectors missed — try the vision agent before failing.
                        try:
                            page_agent = PageAgent(ats=package.platform)
                            tried = get_learned_fixes(package.platform).get("submit")
                            frame_loc = getattr(adapter, "_frame_locator", None) or getattr(adapter, "_frame", None)
                            clicked_via = await page_agent.find_and_click(
                                page, "submit", tried,
                                frame=frame_loc,
                            )
                            if clicked_via:
                                get_learned_fixes(package.platform).add("submit", clicked_via)
                                try:
                                    await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                                except Exception:
                                    pass
                                submitted = True
                        except Exception as exc:
                            logger.warning(f"[Agent] vision submit recovery failed: {exc}")
                    if not submitted:
                        # Last-resort diagnosis before bailing
                        try:
                            from pathlib import Path as _P
                            adapter_module_path = _P(__file__).resolve().parents[1] / "adapters" / f"{package.platform.lower()}.py"
                            await diagnose_failure(
                                page=page,
                                ats=package.platform,
                                action="submit",
                                failure_reason="all submit selectors missed",
                                adapter_source_path=adapter_module_path if adapter_module_path.exists() else None,
                                log_tail=traceback.format_exc(),
                                frame=getattr(adapter, "_frame_locator", None) or getattr(adapter, "_frame", None),
                            )
                        except Exception:
                            pass
                        raise Exception("Submit click failed — no submit button found or click unsuccessful")
                    verified, confirmation_text = await adapter.verify_success(page)

            else:
                # ── Agent-submitted fast path ──────────────────────────────────────────
                # AgentLoop already filled and submitted the form. Skip straight to the
                # screenshot. We treat confirmation as verified=True since the agent
                # explicitly returned status="SUBMITTED".
                submitted = True
                verified = True
                confirmation_text = _agent_confirmation

            # ── STEP 11: Screenshot ──
            # Scroll to the form section so the screenshot captures the filled fields
            try:
                await page.evaluate("""
                    // Find the application form area and scroll it into view
                    let form = document.querySelector('form')
                        || document.querySelector('#application')
                        || document.querySelector('[class*="application"]');
                    if (form) form.scrollIntoView({behavior: 'smooth', block: 'start'});
                """)
                await asyncio.sleep(1.0)
            except Exception:
                pass
            screenshot_path = await capture_and_store_screenshot(page, package.application_id)

            # ── STEP 12: Final status transition ──
            if verified:
                status = "SUBMITTED"
                submit_extra = {}

                # Save the cover letter path (use the original package URL, not temp path)
                if package.cover_letter_url:
                    submit_extra["cover_letter_url"] = package.cover_letter_url

                # Resolve resume_id: find the tailored resume DB record for this
                # candidate+job combination so the application FK is correctly set.
                try:
                    api_base = os.getenv("M1_API_BASE_URL", "http://localhost:8000/api")
                    async with httpx.AsyncClient(timeout=10) as client:
                        rv = await client.get(f"{api_base}/resumes/{package.candidate_id}")
                    if rv.status_code == 200:
                        resumes = rv.json()
                        # Prefer the latest tailored resume for this job
                        match = next(
                            (r for r in sorted(resumes,
                                               key=lambda x: x.get("version", 0),
                                               reverse=True)
                             if not r.get("is_base")
                             and r.get("tailored_for_job_id") == package.job_id),
                            None
                        )
                        if not match:
                            # Fall back to most recent base resume
                            match = next(
                                (r for r in sorted(resumes,
                                                   key=lambda x: x.get("version", 0),
                                                   reverse=True)
                                 if r.get("is_base")),
                                None
                            )
                        if match:
                            submit_extra["resume_id"] = str(match["id"])
                            logger.info(f"[M4] Resolved resume_id={match['id']} (v{match.get('version')})")
                except Exception as exc:
                    logger.warning(f"[M4] Could not resolve resume_id (non-fatal): {exc}")

                logger.info(f"[M4] SUBMITTED extras: {submit_extra}")

                await transition_status(
                    package.application_id, "SUBMITTED",
                    {"screenshot_url": screenshot_path, "confirmation_text": confirmation_text},
                    **submit_extra,
                )
            else:
                status = "FORM_COMPLETED"
                await transition_status(package.application_id, "FORM_COMPLETED")

            # ── STEP 13: Persist session ──
            if context_mgr and context:
                await context_mgr.save_session(package.candidate_id, package.platform, context)
                await context_mgr.destroy_context(context)

            _cleanup_temp(_temp_resume, _temp_cover)

            return ApplicationResult(
                application_id=package.application_id,
                status=status,
                screenshot_url=screenshot_path,
                confirmation_text=confirmation_text,
                execution_time_seconds=_elapsed(),
                retry_count=retry_count,
            )

        except Exception as exc:
            error_message = str(exc)
            logger.error(f"[M4] Application {package.application_id} error: {exc}", exc_info=True)

            # Record platform failure
            if "PLATFORM_NEEDS_REVIEW" not in error_message:
                try:
                    from .platform_review import record_platform_failure
                    record_platform_failure(package.platform, error_message)
                except Exception as p_exc:
                    logger.warning(f"Failed to record platform failure: {p_exc}")

            if "PLATFORM_NEEDS_REVIEW" in error_message:
                status = "FAILED"
                if context_mgr and context:
                    try:
                        await context_mgr.destroy_context(context)
                    except Exception:
                        pass
                _cleanup_temp(_temp_resume, _temp_cover)
                return ApplicationResult(
                    application_id=package.application_id,
                    status="FAILED",
                    error_message=error_message,
                    execution_time_seconds=_elapsed(),
                    retry_count=retry_count,
                )
            elif "BLOCKED" in error_message:
                status = "BLOCKED"
            elif "JOB_EXPIRED" in error_message:
                status = "FAILED"
                if context_mgr and context:
                    try:
                        await context_mgr.destroy_context(context)
                    except Exception:
                        pass
                _cleanup_temp(_temp_resume, _temp_cover)
                return ApplicationResult(
                    application_id=package.application_id,
                    status="FAILED",
                    error_message=error_message.replace("JOB_EXPIRED: ", ""),
                    execution_time_seconds=_elapsed(),
                    retry_count=retry_count,
                )
            elif retry_count < 3:
                # Re-raise so Celery can schedule a retry
                if context_mgr and context:
                    try:
                        await context_mgr.destroy_context(context)
                    except Exception:
                        pass
                _cleanup_temp(_temp_resume, _temp_cover)
                raise
            else:
                status = "FAILED"

            # Best-effort screenshot on error
            if page and not page.is_closed():
                try:
                    screenshot_path = await capture_and_store_screenshot(page, package.application_id)
                except Exception:
                    pass

            if context_mgr and context:
                try:
                    await context_mgr.destroy_context(context)
                except Exception:
                    pass

            _cleanup_temp(_temp_resume, _temp_cover)

            return ApplicationResult(
                application_id=package.application_id,
                status=status,
                screenshot_url=screenshot_path,
                error_message=error_message,
                execution_time_seconds=_elapsed(),
                retry_count=retry_count,
            )
