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


# Hosts that sit behind a bot wall (Cloudflare / PerimeterX / login gate) and
# will return 403 / a challenge / a redirect-to-login to a plain httpx GET — even
# when the listing is perfectly live. For these hosts the lightweight pre-flight
# check produces FALSE POSITIVES ("JOB_EXPIRED"), so we skip it and let the
# real Playwright session (which has stealth + cookies) decide via
# check_page_indicates_expired(). RR is the canonical case the user hit: a live
# listing URL was being killed at pre-flight because httpx got bounced.
# Built In / Glassdoor / ZipRecruiter sit behind Cloudflare or PerimeterX;
# Himalayas / Adzuna / RemoteOK / hiring.cafe are aggregators whose listing
# pages 403 or bounce plain httpx GETs the same way — all false-kill at
# pre-flight, so they belong on this skip list too.
_PREFLIGHT_SKIP_HOSTS = (
    "remoterocketship.com",
    "remote100k",
    "myworkdayjobs.com",
    "workday.com",
    "linkedin.com",
    "indeed.com",
    "dice.com",
    "talent.com",
    "icims.com",
    "smartrecruiters.com",
    "builtin.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "himalayas.app",
    "adzuna.com",
    "remoteok.com",
    "hiring.cafe",
)


# Per-platform AgentLoop step budgets. Multi-step wizard ATSes (login →
# profile → per-page questions → review) burn far more loop iterations than a
# single-page Greenhouse/Lever form, so the default 60 steps (env
# AGENT_LOOP_MAX_STEPS) is not enough — the loop dies mid-wizard with
# MAX_STEPS. Values here only ever RAISE the budget (we take the max with the
# env default), never lower it.
_PLATFORM_MAX_STEPS = {
    "dice": 90,
    "icims": 90,
    "ziprecruiter": 90,
    "workday": 90,
}


def _canonical_platform_key(platform: str, known_slugs) -> str:
    """Resolve a canonical platform slug from a value that may arrive either as
    a bare slug ("dice") or as a HOST ("www.dice.com"). Per the hints.py
    contract, non-passthrough adapters frequently store platform as a host, so a
    plain dict lookup misses. Substring-match the (normalized) value against the
    known slugs and return the first hit; fall back to the normalized value."""
    key = (platform or "").lower().strip()
    normalized = key.replace("-", "").replace("_", "").replace(".", "")
    for slug in known_slugs:
        if slug in key or slug in normalized:
            return slug
    return key


async def is_job_url_active(url: str) -> bool:
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # Skip pre-flight for bot-walled hosts — the real browser will validate.
        try:
            _host = (urlparse(url).hostname or "").lower()
            if any(h in _host for h in _PREFLIGHT_SKIP_HOSTS):
                logger.info(f"[Job Check] Skipping lightweight check for bot-walled host {_host!r}")
                return True
        except Exception:
            pass

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            resp = await client.get(url, headers=headers)

            # 4xx other than 404/410 (e.g. 403 from a bot wall, 401 behind
            # login) tells us NOTHING about whether the listing is live — the
            # full browser may load it fine. Only treat hard-not-found as
            # expired.
            if resp.status_code in (404, 410):
                logger.info(f"[Job Check] Job URL returned status {resp.status_code}: {url}")
                return False
            if resp.status_code >= 400:
                logger.info(
                    f"[Job Check] HTTP {resp.status_code} from pre-flight on {url} — "
                    "deferring decision to real browser session (could be bot wall)"
                )
                return True
                
            final_url = str(resp.url)
            
            if final_url != url:
                parsed_orig = urlparse(url)
                parsed_final = urlparse(final_url)
                
                orig_path = parsed_orig.path.strip("/")
                final_path = parsed_final.path.strip("/")
                
                orig_segments = [s for s in orig_path.split("/") if s]
                final_segments = [s for s in final_path.split("/") if s]

                def _looks_like_job_id(s: str) -> bool:
                    # A path segment that plausibly identifies THIS specific job:
                    #   • pure numeric id (12345)
                    #   • long slug / UUID (len > 8, e.g. 1a750f3c-8595-...)
                    #   • shorter alphanumeric id that mixes letters + digits
                    #     (>=5 chars, e.g. "r4x9k2", "job2024") — these were
                    #     previously missed and fell to the weaker homepage
                    #     heuristic. Pure-alpha slugs ("software-engineer") are
                    #     intentionally NOT job ids (too generic to anchor on).
                    if s.isdigit() or len(s) > 8:
                        return True
                    return len(s) >= 5 and any(c.isdigit() for c in s) and any(c.isalpha() for c in s)

                job_id_segments = [s for s in orig_segments if _looks_like_job_id(s)]
                logger.debug(f"[Job Check] redirect heuristic: orig={orig_segments} "
                             f"final={final_segments} job_id_segments={job_id_segments}")
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

            # CROSS-HOST hops (RR/RR-100k → Ashby/Greenhouse/Lever, or a
            # company-careers SPA bouncing to its ATS) are EXPECTED and not a
            # redirect-to-homepage failure. Only treat redirects as "moved away"
            # when the final host is the SAME as the original — otherwise we
            # cannot meaningfully compare paths (Ashby's slug has nothing in
            # common with RR's slug by design). Title/content patterns below
            # still catch the genuine "page filled / 404" cases.
            same_host = (
                (parsed_orig.hostname or "").lower() == (parsed_final.hostname or "").lower()
            )
            if same_host:
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
        # platform may arrive as a bare slug ("lever") or a host ("jobs.lever.co");
        # substring-match against the known slugs so the per-platform limit still
        # applies in the host case instead of silently falling back to default.
        _limit_key = _canonical_platform_key(platform, self._limits.keys())
        limit = self._limits.get(_limit_key, self._default_limit)
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

        # Strip version suffixes like _v38, -v38, _version38, etc. at the end of the base name
        # e.g., resume_v38.pdf -> resume.pdf, cover_letter_v2.pdf -> cover_letter.pdf
        import re
        base, ext = os.path.splitext(filename)
        cleaned_base = re.sub(r'[_-]v(?:ersion)?_?\d+$', '', base, flags=re.IGNORECASE)
        cleaned_base = re.sub(r'v\d+$', '', cleaned_base, flags=re.IGNORECASE)
        cleaned_base = cleaned_base.rstrip('_-')
        filename = cleaned_base + ext

        if not filename.lower().endswith(suffix.lower()):
            filename += suffix
        # Avoid collisions when different URLs share the same basename (common for signed URLs).
        import hashlib
        digest = hashlib.sha256(url_or_path.encode("utf-8")).hexdigest()[:12]
        root, ext = os.path.splitext(filename)
        filename = f"{root}-{digest}{ext}"

        # Use a project-local, stable cache dir instead of the OS temp
        # dir. Windows aggressively cleans %TEMP% (Storage Sense, tempfile
        # module context managers elsewhere in this process, and any
        # sibling cleanup call that walks tempfile.gettempdir()) — an
        # already-downloaded resume can vanish between the executor's
        # initial resolve and a later retry-attempt upload inside the
        # AgentLoop, which is exactly what we saw on Palantir: the resume
        # download succeeded on entry, but the file was gone by the time
        # the form-fill retry called set_input_files → hard "Local file
        # not found" failure at C:\Users\<u>\AppData\Local\Temp\... .
        # A per-candidate cache under backend/data/ is out of every
        # tempdir-cleanup path and survives across retries + runs.
        _here = os.path.dirname(os.path.abspath(__file__))
        _backend_root = os.path.abspath(os.path.join(_here, "..", "..", "..", ".."))
        cache_dir = os.path.join(_backend_root, "data", "upload_cache")
        os.makedirs(cache_dir, exist_ok=True)
        temp_path = os.path.join(cache_dir, filename)
        with open(temp_path, "wb") as f:
            f.write(resp.content)

        logger.info(f"Downloaded {url_or_path} → {temp_path}")
        return temp_path
    except Exception as exc:
        logger.error(f"Failed to download {url_or_path}: {exc}")
        return None


def _cleanup_temp(*paths: Optional[str]) -> None:
    # Only clean paths under the OS tempdir (legacy behavior — nothing
    # writes here anymore since _resolve_file_to_local_path was moved to
    # backend/data/upload_cache, but keep the guard for any pre-existing
    # callers). The new cache dir is intentionally NOT cleaned: leaving
    # the resume + cover letter cached across retries is a feature, not
    # a leak — same candidate applying to N jobs reuses one download.
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
        logger.info(
            f"[M4] execute() ENTRY app={package.application_id} platform={package.platform!r} "
            f"url={(package.job_url or '')[:120]!r} retry={retry_count}"
        )
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
            from .platform_review import (
                is_platform_flagged,
                get_spam_backoff_remaining,
            )
            if is_platform_flagged(package.platform):
                raise Exception(f"PLATFORM_NEEDS_REVIEW: Platform {package.platform} is flagged as needing review due to previous failures.")

            # ── Phase 5.3: SPAM_FLAGGED exponential backoff gate ──
            # A prior anti-bot rejection arms a growing cooldown for this ATS
            # (see platform_review.record_spam_backoff). While it's active we
            # short-circuit rather than re-tripping the host's spam filter.
            # get_spam_backoff_remaining canonicalizes its argument to the same
            # known-ATS slug the failure/success paths record under (via
            # platform_review._canonical_spam_key over the full hints slug set),
            # so passing the raw package.platform (bare slug OR host) here reads
            # back the exact key an armed cooldown was written under. NOTE: for
            # wrapper aggregators (RemoteRocketship) the failure path records
            # under the resolved INNER ATS, which isn't known at pre-flight, so
            # an inner-ATS cooldown still can't gate the wrapper-routed retry —
            # a documented limitation; native/direct-host cooldowns gate.
            _spam_remaining = get_spam_backoff_remaining(package.platform)
            if _spam_remaining > 0:
                logger.warning(
                    f"[M4] SPAM_BACKOFF active for {package.platform!r} — "
                    f"holding for {_spam_remaining}s after an anti-bot rejection"
                )
                _cleanup_temp(_temp_resume, _temp_cover)
                return ApplicationResult(
                    application_id=package.application_id,
                    status="RATE_LIMITED",
                    execution_time_seconds=_elapsed(),
                    retry_count=retry_count,
                    error_message=(
                        f"SPAM_BACKOFF: {package.platform} is backing off for "
                        f"{_spam_remaining}s after an anti-bot rejection"
                    ),
                )

            # ── PRE-FLIGHT: validate resume exists before launching browser ──
            if not package.resume_url:
                raise ValueError("ApplicationPackage.resume_url is empty — cannot proceed")

            _temp_resume = await _resolve_file_to_local_path(package.resume_url, ".pdf")
            if not _temp_resume:
                raise FileNotFoundError(f"Resume file could not be resolved: {package.resume_url}")

            # Backfill thin/generic candidate_profile fields (e.g. location
            # stored as just "US") from the resume PDF itself — the resume
            # header is the source of truth for the candidate's actual city
            # and state. Mutates package.candidate_profile in place so every
            # downstream consumer (AgentLoop prompt, adapters, LLM filler)
            # sees the enriched value without any test-only env overrides.
            try:
                from .resume_enricher import enrich_profile_from_resume
                enrich_profile_from_resume(package.candidate_profile, _temp_resume)
            except Exception as exc:
                logger.warning(f"[M4] Resume enrichment failed (non-fatal): {exc}")

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

            # ── PRE-FLIGHT 3: robots.txt compliance check ──
            # Operator kill-switch. When DISABLE_ROBOTS_CHECK is on, we skip the
            # robots.txt gate entirely. Reasoning: the candidate has authorized
            # this apply on their own behalf — it is a one-shot form submission,
            # NOT a crawler. Most job aggregators (RR, Ashby, Dice, Workday,
            # LinkedIn, Indeed) ship a `User-agent: * Disallow: /` style
            # robots.txt that blocks unknown bots from scraping listings. With
            # the gate on, only ATSes with permissive robots (Greenhouse boards)
            # ever reach the browser — exactly the symptom the operator saw
            # ("only Greenhouse Vercel ones start").
            from .robots_validator import is_action_allowed
            if os.getenv("DISABLE_ROBOTS_CHECK", "").lower() in ("1", "true", "yes", "on"):
                logger.info(f"[Robots.txt] DISABLE_ROBOTS_CHECK=on — skipping gate for {package.platform}")
            else:
                robots_allowed = True
                try:
                    robots_allowed = await is_action_allowed(package.job_url)
                except Exception as e:
                    logger.warning(f"Error checking robots.txt compliance: {e}")

                if not robots_allowed:
                    logger.error(
                        f"[Robots.txt] BLOCKING application — host disallows our UA. "
                        f"platform={package.platform} url={package.job_url[:120]} "
                        f"(set DISABLE_ROBOTS_CHECK=true to override)"
                    )
                    raise Exception("ROBOTS_BLOCKED: BLOCKED: Navigation disallowed by robots.txt policy")

            # ── PRE-FLIGHT 4: account-walled ATS skip ──
            # Workday/iCIMS/Dice authenticate at the submit endpoint. Without
            # configured credentials there is NO scraping bypass — the form is
            # gated behind a real account. Fail fast (before launching Chrome)
            # with a specific LOGIN_REQUIRED status so the operator sees "this
            # job needs creds" rather than "browser opened and closed". Saves
            # ~30s per job and keeps the screenshot log meaningful.
            # Load the candidate's own portal login credentials (Gmail-based
            # login email + password) from their DB profile ONCE here, so the
            # account-wall pre-flight below can accept EITHER per-candidate
            # credentials OR global env-var credentials, and so we can inject
            # them into the adapter before it navigates/logs in. Never placed on
            # candidate_profile — keeps the password out of LLM prompts and logs.
            _cand_creds = {"login_email": "", "password": "", "gmail": ""}
            try:
                from ..adapters.session_utils import load_candidate_credentials
                _cand_creds = await load_candidate_credentials(package.candidate_id)
            except Exception as _cc_exc:
                logger.debug(f"[M4] candidate-credential load skipped: {_cc_exc}")
            _has_cand_login = bool(
                _cand_creds.get("login_email") and _cand_creds.get("password")
            )

            _walled_creds = {
                "workday":      ("WORKDAY_USERNAME",    "WORKDAY_PASSWORD"),
                "icims":        ("ICIMS_USERNAME",      "ICIMS_PASSWORD"),
                "dice":         ("DICE_EMAIL",          "DICE_PASSWORD"),
                "glassdoor":    ("GLASSDOOR_EMAIL",     "GLASSDOOR_PASSWORD"),
                "ziprecruiter": ("ZIPRECRUITER_EMAIL",  "ZIPRECRUITER_PASSWORD"),
            }
            _plat_key = (package.platform or "").lower().strip()
            _creds = _walled_creds.get(_plat_key)
            if _creds:
                u_env, p_env = _creds
                _has_env = bool(
                    os.getenv(u_env, "").strip() and os.getenv(p_env, "").strip()
                )
                if not (_has_env or _has_cand_login):
                    logger.warning(
                        f"[M4] Pre-flight skip: {_plat_key} requires {u_env}/{p_env} "
                        "in .env OR a gmail+password on the candidate profile — "
                        "no scraping bypass exists for account-walled ATSes."
                    )
                    raise Exception(
                        f"LOGIN_REQUIRED: {_plat_key} requires an account; set "
                        f"{u_env}/{p_env} in .env or add gmail+password to the candidate."
                    )
                if _has_cand_login and not _has_env:
                    logger.info(
                        f"[M4] {_plat_key}: authenticating with candidate-profile "
                        "credentials (no env override set)."
                    )

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
                # Surface the candidate's Gmail (an email address — not a secret)
                # so adapters whose flow reads it back from that mailbox can use
                # it (e.g. Talent's email/OTP gate). The password is NOT added
                # here — it travels only via set_candidate_credentials below.
                if _cand_creds.get("gmail") and isinstance(package.candidate_profile, dict):
                    package.candidate_profile.setdefault("gmail", _cand_creds["gmail"])
            except Exception:
                pass
            # Inject login credentials via the secure channel (NOT candidate_profile)
            # so login-gated adapters authenticate as this candidate; the password
            # never touches LLM prompts or logs.
            try:
                adapter.set_candidate_credentials(_cand_creds)
            except Exception:
                pass
            await adapter.navigate_to_application(page, package.job_url)

            # When a wrapper aggregator (RemoteRocketship, Remote100k) navigated
            # to the underlying ATS, page.url is now on a totally different host
            # (jobs.ashbyhq.com, boards.greenhouse.io, …). Comparing that against
            # the original RR URL trips the "redirected away" heuristic on EVERY
            # successful wrapper apply and falsely kills it as JOB_EXPIRED.
            # Use the resolved inner URL as the comparison baseline when the
            # adapter exposes one.
            _expired_check_url = getattr(adapter, "_resolved_url", None) or package.job_url
            if _expired_check_url != package.job_url:
                logger.info(
                    f"[Job Check] Using resolved inner URL for expired-check "
                    f"(wrapper={package.platform}): {_expired_check_url}"
                )
            if await check_page_indicates_expired(page, _expired_check_url):
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
                    # Per-platform step budget: multi-step wizards (Dice, iCIMS,
                    # ZipRecruiter, Workday) need more than the default 60 steps.
                    # Take the MAX with the env default so operators can still
                    # raise the global budget via AGENT_LOOP_MAX_STEPS without
                    # this table silently clamping it back down.
                    try:
                        _default_steps = int(os.getenv("AGENT_LOOP_MAX_STEPS", "60"))
                    except ValueError:
                        _default_steps = 60
                    _loop_plat_key = _canonical_platform_key(
                        effective_platform, _PLATFORM_MAX_STEPS.keys()
                    )
                    _loop_max_steps = max(
                        _default_steps, _PLATFORM_MAX_STEPS.get(_loop_plat_key, 0)
                    )
                    if _loop_max_steps != _default_steps:
                        logger.info(
                            f"[M4] Raising AgentLoop step budget to {_loop_max_steps} "
                            f"for multi-step platform {_loop_plat_key!r}"
                        )
                    agent_loop = AgentLoop(
                        candidate_profile=package.candidate_profile,
                        job_context=job_ctx_for_loop,
                        resume_path=_temp_resume,
                        cover_letter_path=_temp_cover,
                        screening_answers=pre_answers,
                        candidate_id=package.candidate_id,
                        stop_before_submit=dry_run,
                        max_steps=_loop_max_steps,
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

                    elif getattr(agent_loop, "_submit_fired", False):
                        # The AgentLoop already CLICKED submit but did not reach a
                        # confirmed SUBMITTED (e.g. an email-verification wall it
                        # couldn't clear in time, or it ran out of steps on the
                        # post-submit page). The form was already sent to the ATS,
                        # so the scripted fallback below MUST NOT run — re-detecting
                        # and re-submitting would file a DUPLICATE application.
                        # Mark terminal with a clear, non-retryable reason.
                        logger.warning(
                            f"[M4] AgentLoop fired submit but ended {loop_result.status!r} "
                            "without confirmation — NOT running scripted fallback "
                            "(would double-submit). Marking EMAIL_VERIFICATION_REQUIRED."
                        )
                        raise Exception(
                            "EMAIL_VERIFICATION_REQUIRED: application was submitted but "
                            "post-submit verification did not complete "
                            f"(loop status={loop_result.status}, error={loop_result.error})"
                        )

                    elif loop_result.status == "VERIFICATION_FAILED":
                        # Email verification wall — Gmail not connected or code never arrived.
                        # Retrying opens the same wall every time. Stop immediately as BLOCKED.
                        raise Exception(
                            f"BLOCKED: Email verification required but could not retrieve code "
                            f"(Gmail not connected?). {loop_result.error or ''}"
                        )

                    else:
                        # MAX_STEPS / STUCK / LLM_UNAVAILABLE / ERROR
                        # Mutual Exclusion: We no longer fall back to the deterministic 
                        # pipeline if AgentLoop gets stuck. Running both on the same
                        # React DOM causes conflicting state and lost data.
                        logger.error(
                            f"[M4] AgentLoop non-terminal ({loop_result.status}) — "
                            "aborting application to prevent fallback conflict"
                        )
                        raise Exception(
                            f"AgentLoop failed to complete ({loop_result.status}): {loop_result.error}. "
                            "Failing application to prevent deterministic fallback conflict."
                        )

                except Exception as exc:
                    # Intentional terminal outcomes must propagate, NOT fall
                    # through to the scripted pipeline. "EMAIL_VERIFICATION_REQUIRED"
                    # means submit already fired — re-running scripted submit would
                    # double-apply. "AgentLoop aborted" is an explicit wrong-page/abort.
                    if "AgentLoop aborted" in str(exc) or "EMAIL_VERIFICATION_REQUIRED" in str(exc) or "BLOCKED: Email verification" in str(exc):
                        raise
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
                    api_base = os.getenv("M1_API_BASE_URL", "http://localhost:8002/api")
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
                    "CAPSOLVER_API_KEY" if provider == "capsolver" else
                    "TWO_CAPTCHA_API_KEY" if provider == "2captcha" else
                    "ANTI_CAPTCHA_API_KEY" if provider == "anticaptcha" else
                    "OCILAR_API_KEY",
                    "",
                )
                key_configured = bool(raw_key) and not raw_key.lower().startswith("your_")

                if form.has_captcha:
                    if dry_run:
                        logger.warning(
                            f"[M4] Captcha detected ({form.captcha_type}) — skipping solve (dry_run=True)"
                        )
                    elif not key_configured:
                        # No solver API key — stop immediately. Retrying will just hit the
                        # same captcha wall again. Mark as BLOCKED so Celery does not retry.
                        error_message = (
                            f"BLOCKED: {form.captcha_type} captcha detected on {package.platform} "
                            f"but no solver API key is configured "
                            f"(set CAPSOLVER_API_KEY, OCILAR_API_KEY, TWO_CAPTCHA_API_KEY, or ANTI_CAPTCHA_API_KEY)"
                        )
                        logger.error(f"[M4] {error_message}")
                        try:
                            screenshot_path = await capture_and_store_screenshot(page, package.application_id)
                        except Exception:
                            pass
                        _cleanup_temp(_temp_resume, _temp_cover)
                        return ApplicationResult(
                            application_id=package.application_id,
                            status="BLOCKED",
                            screenshot_url=screenshot_path,
                            error_message=error_message,
                            execution_time_seconds=_elapsed(),
                            retry_count=retry_count,
                        )
                    else:
                        captcha_svc = CaptchaService(provider=provider)
                        solution = await captcha_svc.solve(page, form.captcha_type)
                        if not solution.success:
                            status = "CAPTCHA_FAILED"
                            error_message = f"BLOCKED: Captcha solving exhausted all attempts: {form.captcha_type}"
                            screenshot_path = await capture_and_store_screenshot(page, package.application_id)
                            logger.error(f"[M4] {error_message}")
                            _cleanup_temp(_temp_resume, _temp_cover)
                            return ApplicationResult(
                                application_id=package.application_id,
                                status="BLOCKED",
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

                # DRY_RUN rows still land as SUBMITTED (state machine has no
                # separate dry-run state), so stamp a STABLE, queryable prefix on
                # confirmation_text. Downstream reporting can exclude dry runs
                # with `confirmation_text NOT LIKE 'DRY_RUN:%'` regardless of
                # which path (scripted vs AgentLoop) produced them.
                _is_dry_run = os.getenv("DRY_RUN_NO_SUBMIT", "false").lower() == "true"
                if _is_dry_run and not str(confirmation_text or "").startswith("DRY_RUN:"):
                    confirmation_text = f"DRY_RUN: {confirmation_text or 'stopped before submit'}"

                # Phase 5.3: a successful apply means this ATS is not currently
                # spam-blocking us — clear any stale SPAM backoff so the next
                # attempt isn't needlessly delayed.
                try:
                    from .platform_review import reset_spam_backoff
                    reset_spam_backoff(effective_platform if "effective_platform" in locals() else package.platform)
                except Exception as _sb_exc:
                    logger.debug(f"[M4] reset_spam_backoff skipped: {_sb_exc}")

                # Save the cover letter path (use the original package URL, not temp path)
                if package.cover_letter_url:
                    submit_extra["cover_letter_url"] = package.cover_letter_url

                # Resolve resume_id: find the tailored resume DB record for this
                # candidate+job combination so the application FK is correctly set.
                try:
                    api_base = os.getenv("M1_API_BASE_URL", "http://localhost:8002/api")
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

                db_persisted = await transition_status(
                    package.application_id, "SUBMITTED",
                    {"screenshot_url": screenshot_path, "confirmation_text": confirmation_text},
                    **submit_extra,
                )
                if not db_persisted:
                    # The application WENT OUT to the employer, but the DB PATCH
                    # failed (API down, invalid transition, network). Do NOT flip
                    # status to FAILED — a retry would re-submit to the employer.
                    # Log loudly so the row can be reconciled out-of-band, and
                    # carry the flag back to the caller (was silently swallowed).
                    logger.critical(
                        f"[M4] DB PERSIST FAILED for application {package.application_id}: "
                        f"the application was SUBMITTED to the employer but the status "
                        f"PATCH to M1 did not succeed. Reconcile this row manually — do "
                        f"NOT re-run the apply (it would double-submit)."
                    )
            else:
                status = "FORM_COMPLETED"
                db_persisted = await transition_status(package.application_id, "FORM_COMPLETED")
                if not db_persisted:
                    logger.error(
                        f"[M4] DB persist failed for application {package.application_id} "
                        f"(FORM_COMPLETED status PATCH to M1 did not succeed)."
                    )

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
                db_persisted=db_persisted,
            )

        except Exception as exc:
            error_message = str(exc)
            logger.error(f"[M4] Application {package.application_id} error: {exc}", exc_info=True)

            # Record platform-level failures only. Job-specific and infra errors
            # (JOB_EXPIRED, ROBOTS_BLOCKED, browser crash) are filtered inside
            # record_platform_failure and do NOT count toward the flag threshold.
            try:
                from .platform_review import record_platform_failure
                # When a wrapper aggregator (RemoteRocketship, Remote100k) delegates
                # to an inner ATS adapter, charge the failure to the resolved inner
                # ATS — not the wrapper. Otherwise every Greenhouse/Workday/Lever
                # hiccup behind RR gets counted against RR and trips the breaker
                # for a host that wasn't actually at fault.
                _eff_platform = package.platform
                try:
                    _inner_adapter = locals().get("adapter", None)
                    _inner = getattr(_inner_adapter, "_inner", None) if _inner_adapter else None
                    _inner_name = getattr(_inner, "platform_name", None) if _inner else None
                    if _inner_name:
                        _eff_platform = _inner_name
                except Exception:
                    pass
                record_platform_failure(_eff_platform, error_message)
            except Exception as p_exc:
                logger.warning(f"Failed to record platform failure: {p_exc}")

            # Phase 5.3: an ATS anti-bot rejection (SPAM_FLAGGED) arms an
            # exponential per-platform cooldown so the next attempt is delayed
            # instead of immediately re-tripping the host's spam filter. Keyed
            # to the resolved inner ATS (_eff_platform), same as the failure
            # record above.
            try:
                if "SPAM_FLAGGED" in error_message:
                    from .platform_review import record_spam_backoff
                    record_spam_backoff(_eff_platform)
            except Exception as sb_exc:
                logger.warning(f"Failed to record SPAM backoff: {sb_exc}")

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
            elif "ROBOTS_BLOCKED" in error_message:
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
            elif "EMAIL_VERIFICATION_REQUIRED" in error_message:
                # The form was ALREADY submitted to the ATS but the post-submit
                # email-verification code could not be completed. This is TERMINAL
                # and must NOT be retried — a retry would re-open the form and file
                # a DUPLICATE application. Return FAILED so the task layer reports
                # it without re-queuing.
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
        finally:
            _cleanup_temp(_temp_resume, _temp_cover)
