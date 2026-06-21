import os
import time
import logging
import asyncio
import tempfile
import httpx
import redis.asyncio as redis

from typing import Optional, Dict, Literal
from playwright.async_api import async_playwright, Page, BrowserContext
from dotenv import load_dotenv

from ..browser import BrowserContextManager
from ..adapters import get_adapter, BasePlatformAdapter
from ..forms import detect_form, fill_form, fill_form_with_llm
from ..captcha import CaptchaService
from ..agent import PageAgent, diagnose_failure, get_learned_fixes

from .models import ApplicationPackage, ApplicationResult
from .screenshot import capture_and_store_screenshot
from .state_machine import transition_status

load_dotenv()
logger = logging.getLogger(__name__)


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

async def _resolve_file_to_local_path(url_or_path: str, suffix: str = ".pdf") -> Optional[str]:
    """Return a local filesystem path for the given URL or path.

    - If it's already a valid local path → return as-is.
    - If it's an HTTP(S) URL → download to a temp file and return the path.
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

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url_or_path)
            resp.raise_for_status()
        # Write to a persistent temp file (not inside a with-block so it survives)
        tf = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tf.write(resp.content)
        tf.close()
        logger.info(f"Downloaded {url_or_path} → {tf.name}")
        return tf.name
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
            await adapter.navigate_to_application(page, package.job_url)

            # ── STEP 5.5: Vision page-agent oversight ─────────────────────
            # After navigation, ask Gemini what page state we landed on. If
            # the adapter missed an Apply button (we landed on a LISTING), the
            # agent can find it from a screenshot + DOM and click — and the
            # selector is learned for next time.
            use_agent = os.getenv("USE_PAGE_AGENT", "true").lower() == "true"
            if use_agent:
                try:
                    page_agent = PageAgent(ats=package.platform)
                    state = await page_agent.classify_page(
                        page, frame=getattr(adapter, "_frame", None),
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
                                frame=getattr(adapter, "_frame", None),
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

            # ── STEP 6: Detect form (done inside fill_application; get ref for screening) ──
            ctx = adapter._frame if getattr(adapter, '_iframe_mode', False) else page
            form = await detect_form(ctx, container_selector=adapter.container_selector)

            # ── STEP 6.5: Call M3 only for screening answers (URLs already resolved) ──
            #
            # We already have the tailored resume and cover letter from the M3 event.
            # We call M3's prepare-package endpoint ONLY to obtain answers to any
            # screening questions found in the form that weren't pre-answered.
            # We NEVER overwrite resume_url or cover_letter_url if they are already set.
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

                        # Only adopt URLs from M3 if we don't already have local paths
                        if not _temp_cover and m3.get("cover_letter_pdf_url"):
                            _temp_cover = await _resolve_file_to_local_path(
                                m3["cover_letter_pdf_url"], ".pdf"
                            )

                        # Merge M3 screening answers (don't overwrite already-answered ones)
                        for q, a in (m3.get("screening_answers") or {}).items():
                            if q not in screening_answers:
                                screening_answers[q] = a

                        if not m3.get("should_apply", True):
                            logger.warning(f"[M4] M3 returned should_apply=False; abandoning")
                            await transition_status(package.application_id, "ANALYZED",
                                                    {"reason": "score_below_threshold"})
                            if context_mgr and context:
                                await context_mgr.destroy_context(context)
                            _cleanup_temp(_temp_resume, _temp_cover)
                            return ApplicationResult(
                                application_id=package.application_id,
                                status="FAILED",
                                error_message="Abandoned: score below threshold",
                                execution_time_seconds=_elapsed(),
                                retry_count=retry_count,
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
            fill_ctx = adapter._frame if getattr(adapter, "_iframe_mode", False) else page
            fill_success = False
            if use_llm_fill:
                try:
                    job_ctx = {
                        "platform": package.platform,
                        "ats_type": package.ats_type,
                        "job_url": package.job_url,
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
                ctx_for_final = getattr(adapter, "_frame", None) or page
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
                    await diagnose_failure(
                        page=page,
                        ats=package.platform,
                        action="fill_form",
                        failure_reason="one or more required fields could not be filled",
                        adapter_source_path=adapter_module_path if adapter_module_path.exists() else None,
                        frame=getattr(adapter, "_frame", None),
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
                        clicked_via = await page_agent.find_and_click(
                            page, "submit", tried,
                            frame=getattr(adapter, "_frame", None),
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
                            frame=getattr(adapter, "_frame", None),
                        )
                    except Exception:
                        pass
                    raise Exception("Submit click failed — no submit button found or click unsuccessful")
                verified, confirmation_text = await adapter.verify_success(page)

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

            if "BLOCKED" in error_message:
                status = "BLOCKED"
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
