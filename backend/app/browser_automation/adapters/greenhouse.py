import asyncio
import logging
from typing import Optional, Tuple
from playwright.async_api import Page, Frame
from .base import BasePlatformAdapter
from ..forms import detect_form, fill_form, upload_file
from ..agent import get_learned_fixes

logger = logging.getLogger(__name__)

# Greenhouse uses two hosting modes:
#   1. boards.greenhouse.io — form rendered directly in the page (no iframe).
#   2. Embedded widget on company site — form lives inside #grnhse_iframe.
# We detect which mode we're in at navigation time and store the active context.

_IFRAME_SEL = "#grnhse_iframe"
_IFRAME_TIMEOUT_MS = 8_000
_FIELD_TIMEOUT_MS = 6_000
_NAV_TIMEOUT_MS = 20_000

# Apply-button selectors used on Greenhouse job-listing pages.
# The form only appears AFTER clicking one of these.
_APPLY_SELECTORS = [
    "a#apply_button",
    "#nav_apply",
    "a[href*='#app']:has-text('Apply')",
    "a:has-text('Apply for this Job')",
    "a:has-text('Apply for This Job')",
    "button:has-text('Apply for this Job')",
    "a:has-text('Apply Now')",
    "button:has-text('Apply Now')",
    ".apply-button",
    "[data-qa='btn-apply']",
    "#jobs-apply",
]


class GreenhouseAdapter(BasePlatformAdapter):
    platform_name = "greenhouse"
    container_selector = None  # whole-page scan; scoped below if iframe mode

    def __init__(self):
        # Set during navigate_to_application
        self._iframe_mode: bool = False
        self._frame_locator = None

    # ──────────────────────────────────────────────────────────────────────────
    # Navigation
    # ──────────────────────────────────────────────────────────────────────────

    async def _settle_page(self, page: Page, timeout_ms: int = 6_000) -> None:
        """Wait for any in-flight navigation to finish, then stop loading.

        Called after a goto() timeout to prevent subsequent navigations from
        racing against an unfinished prior one — which causes Playwright to
        throw 'Page.content: Unable to retrieve content because the page is
        navigating and changing the content.'
        """
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        try:
            await page.evaluate("window.stop()")   # cancel in-flight resources
        except Exception:
            pass
        await asyncio.sleep(0.5)

    async def _safe_page_content(self, page: Page) -> str:
        """Read page.content() safely, waiting for any navigation to settle first."""
        for attempt in range(3):
            try:
                return (await page.content())[:800].lower()
            except Exception:
                if attempt < 2:
                    await self._settle_page(page, timeout_ms=4_000)
                else:
                    return ""
        return ""

    async def _warm_up_domain(self, page: Page, base_url: str) -> None:
        """Quick homepage visit to establish a session cookie before the job page.
        Only visits the root — no /careers (it's a heavy SPA that times out)."""
        try:
            await page.goto(base_url, wait_until="domcontentloaded", timeout=15_000)
            title = (await page.title()).lower()
            if "request could not be satisfied" in title or "403" in title:
                raise RuntimeError(f"BLOCKED: IP flagged by WAF on {base_url} — use a VPN or proxy")
            await self.human_delay(0.5, 1.5)
            logger.info(f"[GH] Warm-up OK: {page.url!r}")
        except RuntimeError:
            raise
        except Exception as exc:
            logger.warning(f"[GH] Warm-up {base_url!r} failed (non-fatal): {exc}")
            await self._settle_page(page)

    async def navigate_to_application(self, page: Page, job_url: str) -> None:
        # ── 0. Quick homepage warm-up for sites with CloudFront WAF ──
        # boards.greenhouse.io/monks/* redirects to monks.com — warm up monks.com
        if "monks.com" in job_url or "greenhouse.io/monks" in job_url:
            await self._warm_up_domain(page, "https://www.monks.com")

        # ── 1. Load the URL (job listing page or direct application form) ──
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        except Exception as exc:
            logger.warning(f"[GH] Job page navigation timeout/error: {exc}")
            # Settle the page so content() won't throw "page is navigating"
            await self._settle_page(page, timeout_ms=8_000)

        # Check for bot-block pages (CloudFront/WAF 403)
        try:
            title = (await page.title()).lower()
        except Exception:
            title = ""
        content_snippet = await self._safe_page_content(page)

        if any(t in title or t in content_snippet for t in (
            "request could not be satisfied", "request blocked",
            "access denied", "403 error", "error: the request",
        )):
            logger.error(f"[GH] Bot block detected at {page.url} — title: {title!r}")
            raise RuntimeError(f"BLOCKED: Bot detection triggered at {page.url}")

        logger.info(f"[GH] Loaded: {page.url!r} (title: {title!r})")
        await self.human_delay(0.5, 1.5)

        # ── 2. Detect iframe mode first (for embedded widgets on company sites) ──
        self._iframe_mode = False
        self._frame = None
        try:
            iframe_el = await page.wait_for_selector(_IFRAME_SEL, timeout=_IFRAME_TIMEOUT_MS)
            if iframe_el:
                logger.info(f"[GH] Found #{_IFRAME_SEL} selector")
                self._frame_locator = page.frame_locator(_IFRAME_SEL)
                self._iframe_mode = True
                logger.info("[GH] Embedded iframe mode detected (using frame_locator)")
                # Wait for the iframe content to be ready
                try:
                    await self._frame_locator.locator("input, select, textarea").first.wait_for(
                        state="attached", timeout=_FIELD_TIMEOUT_MS
                    )
                    logger.info("[GH] Iframe form content is ready")
                except Exception as exc:
                    logger.warning(f"[GH] Iframe content wait: {exc}")
            else:
                logger.warning("[GH] #grnhse_iframe selector wait timed out; falling back to hosted-board mode")
        except Exception:
            logger.info("[GH] No #grnhse_iframe detected — hosted boards mode (direct page)")

        # ── 3. If we're on a job LISTING page, click "Apply" to reach the form ──
        # boards.greenhouse.io job pages show description + an Apply link, not the form itself.
        target = self._frame_locator if self._iframe_mode else page
        has_inputs = False
        try:
            await target.locator("input, select, textarea").first.wait_for(
                state="attached", timeout=2_000
            )
            has_inputs = True
        except Exception:
            pass

        if not has_inputs:
            logger.info("[GH] No form inputs detected yet — looking for Apply button")
            # Try learned selectors first — they were proven to work last time.
            learned = get_learned_fixes("greenhouse").get("apply_button")
            apply_candidates = learned + [s for s in _APPLY_SELECTORS if s not in learned]
            for sel in apply_candidates:
                try:
                    btn = target.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        href = await btn.get_attribute("href") or ""
                        logger.info(f"[GH] Clicking Apply button: {sel!r} (href={href!r})")
                        await btn.scroll_into_view_if_needed()
                        await self.human_delay(0.3, 0.8)
                        await btn.click()
                        try:
                            await page.wait_for_load_state("networkidle", timeout=15_000)
                        except Exception:
                            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                        await self.human_delay(1.0, 2.0)
                        logger.info(f"[GH] After Apply click → {page.url!r}")
                        if self._iframe_mode:
                            logger.info("[GH] Iframe swap expected after Apply click. Waiting for DOM stability proactively...")
                            from ..frame_utils import wait_for_stability
                            stable = await wait_for_stability(page, self._frame_locator, is_iframe_mode=True)
                            if not stable:
                                logger.warning(f"[GH] Stability wait failed for selector {sel!r}, retrying click if possible...")
                                # Try clicking again and waiting again
                                await self.human_delay(1.0, 2.0)
                                await btn.click()
                                await wait_for_stability(page, self._frame_locator, is_iframe_mode=True)
                        break
                except Exception as exc:
                    logger.debug(f"[GH] Apply selector {sel!r} failed: {exc}")

        # ── 4. Wait for the application form inputs ──
        target = self._frame_locator if self._iframe_mode else page
        try:
            await target.locator("input, select, textarea").first.wait_for(
                state="attached", timeout=_FIELD_TIMEOUT_MS
            )
            logger.info("[GH] Form inputs ready")
        except Exception:
            logger.warning("[GH] Timed out waiting for form inputs; proceeding anyway")

        # Dismiss cookie banners to prevent pointer event interception
        try:
            cookie_btns = [
                "button#onetrust-accept-btn-handler",
                "button#onetrust-reject-all-handler",
                "button.osano-cm-accept",
                "button.osano-cm-deny"
            ]
            for btn_sel in cookie_btns:
                btn = page.locator(btn_sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=2000)
                    logger.info(f"[GH] Dismissed cookie banner via: {btn_sel}")
                    await asyncio.sleep(0.5)
                    break
        except Exception:
            pass

        await self.human_delay(0.5, 1.5)

    async def detect_application_type(self, page: Page) -> str:
        return "EXTERNAL_FORM"

    async def refresh_frame(self, page: Page) -> None:
        if self._iframe_mode:
            logger.info("[GH] refresh_frame called: frame_locator resolves lazily, no action needed")

    # ──────────────────────────────────────────────────────────────────────────
    # Form filling
    # ──────────────────────────────────────────────────────────────────────────

    async def fill_application(
        self,
        page: Page,
        profile: dict,
        resume_path: str,
        cover_letter_path: Optional[str],
        screening_answers: Optional[dict],
        pre_detected_form=None,
        candidate_id: Optional[str] = None,
    ) -> bool:
        from ..frame_utils import get_live_frame
        live_frame = await get_live_frame(self._frame_locator) if self._iframe_mode else None
        eval_ctx = live_frame if self._iframe_mode else page
        loc_ctx = self._frame_locator if self._iframe_mode else page

        form = pre_detected_form or await detect_form(eval_ctx, container_selector=self.container_selector)

        file_fields = [f for f in form.fields if f.field_type == "file"]
        logger.info(f"[GH] Detected {len(file_fields)} file field(s): "
                    f"{[(f.label, f.selector) for f in file_fields]}")

        fill_success = await fill_form(loc_ctx, form, profile, screening_answers, candidate_id=candidate_id)

        # Re-fetch the live frame because fill_form (and LLM generation) may take a long time,
        # during which the original eval_ctx (Frame object) may have become detached or stale.
        eval_ctx_for_upload = (await get_live_frame(self._frame_locator)) if self._iframe_mode else page
        await self._upload_greenhouse_files(eval_ctx_for_upload, loc_ctx, resume_path, cover_letter_path)

        # Re-scan DISABLED while debugging: rescan calls detect_form which
        # iterates DOM elements; on some Greenhouse builds this triggers a
        # form-wide React rerender that resets react-select widgets back to
        # placeholder, even though the initial fill committed cleanly.
        rescan = None  # await detect_form(ctx, container_selector=self.container_selector, skip_scroll=True)
        new_fields = []
        if False and rescan and new_fields:
            logger.info(f"[GH] Re-scan found {len(new_fields)} new field(s): "
                        f"{[(f.label, f.field_type) for f in new_fields]}")
            from ..forms.models import DetectedForm as _DF
            extra_form = _DF(
                form_type=form.form_type, fields=new_fields, steps=1,
                current_step=1, has_captcha=False, captcha_type=None,
                has_file_upload=False, submit_selector=form.submit_selector,
            )
            await fill_form(ctx, extra_form, profile, screening_answers)

        return fill_success

    # ──────────────────────────────────────────────────────────────────────────
    # File uploads
    # ──────────────────────────────────────────────────────────────────────────

    async def _upload_greenhouse_files(
        self,
        eval_ctx,     # Frame or Page for evaluate()
        loc_ctx,      # FrameLocator or Page for locator()
        resume_path: Optional[str],
        cover_letter_path: Optional[str],
    ) -> None:
        """Upload resume and cover letter to the correct Greenhouse file inputs.

        Uses a single JS call to read ALL file inputs and their surrounding
        context at once, then maps each PDF to the correct slot by label.
        """
        if not eval_ctx:
            logger.warning("[GH] eval_ctx is missing, cannot evaluate file inputs")
            return

        # ── Classify file inputs by walking up to the fieldset.attachment ancestor ──
        # Greenhouse wraps each file input in <fieldset class="attachment"> whose
        # text content starts with the label ("Resume / CV*" or "Cover Letter").
        file_info = await eval_ctx.evaluate("""() => {
            let inputs = document.querySelectorAll("input[name='file-attachment'], input[type='file']");
            let results = [];
            for (let i = 0; i < inputs.length; i++) {
                let inp = inputs[i];
                let purpose = 'unknown';
                let matchedText = '';

                // Strategy A: walk up to the fieldset.attachment ancestor,
                // its FIRST text content starts with the label
                let node = inp.parentElement;
                for (let j = 0; j < 10 && node; j++) {
                    let cls = (typeof node.className === 'string') ? node.className.toLowerCase() : '';
                    if (cls.includes('attachment') || cls.includes('contains-attachment') ||
                        node.tagName === 'FIELDSET') {
                        // Read first 60 chars of text — that's the label
                        let txt = (node.textContent || '').replace(/\\s+/g, ' ').trim().substring(0, 80).toLowerCase();
                        matchedText = txt;
                        if (/^cover\\s*letter/.test(txt) || /cover\\s*letter/.test(txt.substring(0, 30))) {
                            purpose = 'cover';
                            break;
                        }
                        if (/^resume/.test(txt) || /^cv/.test(txt) ||
                            /resume\\s*\\/\\s*cv/.test(txt.substring(0, 30))) {
                            purpose = 'resume';
                            break;
                        }
                    }
                    node = node.parentElement;
                }

                // Strategy B: self attributes
                if (purpose === 'unknown') {
                    let selfText = (
                        (inp.id || '') + ' ' +
                        (inp.getAttribute('aria-label') || '')
                    ).toLowerCase();
                    if (/cover[_\\s-]*letter/.test(selfText)) {
                        purpose = 'cover'; matchedText = 'self:' + selfText;
                    } else if (/\\bresume\\b|\\bcv\\b/.test(selfText)) {
                        purpose = 'resume'; matchedText = 'self:' + selfText;
                    }
                }

                results.push({
                    index: i,
                    purpose: purpose,
                    matched: matchedText.substring(0, 120),
                });
            }
            return results;
        }""")

        if not file_info:
            logger.warning("[GH] No file inputs found on page — skipping file uploads")
            return

        logger.info(f"[GH] Found {len(file_info)} file input(s)")

        gh_inputs = loc_ctx.locator("input[name='file-attachment']")
        count = await gh_inputs.count()
        if count == 0:
            gh_inputs = loc_ctx.locator("input[type='file']")
            count = await gh_inputs.count()

        resume_slot = None
        cover_slot = None

        for info in file_info:
            logger.info(f"[GH] File input #{info['index']} matched heading: "
                        f"'{info['matched']}' -> purpose={info['purpose']}")
            if info["purpose"] == "resume" and resume_slot is None:
                resume_slot = info["index"]
            elif info["purpose"] == "cover" and cover_slot is None:
                cover_slot = info["index"]

        # Positional fallback ONLY if both are unidentified
        if resume_slot is None and cover_slot is None and len(file_info) >= 2:
            resume_slot = 0
            cover_slot = 1
            logger.warning("[GH] No labels matched — using positional fallback (resume=#0)")
        elif resume_slot is None and cover_slot is not None and len(file_info) >= 2:
            # Cover was identified, resume is the OTHER slot
            resume_slot = 1 - cover_slot if cover_slot in (0, 1) else 0
            logger.info(f"[GH] Inferred resume_slot=#{resume_slot} (opposite of cover #{cover_slot})")
        elif cover_slot is None and resume_slot is not None and len(file_info) >= 2:
            cover_slot = 1 - resume_slot if resume_slot in (0, 1) else 1
            logger.info(f"[GH] Inferred cover_slot=#{cover_slot} (opposite of resume #{resume_slot})")
        elif resume_slot is None and len(file_info) >= 1:
            resume_slot = 0

        # Upload to correct slots
        if resume_path and resume_slot is not None and resume_slot < count:
            await self._safe_upload(gh_inputs.nth(resume_slot), "resume", resume_path)
        if cover_letter_path and cover_slot is not None and cover_slot < count:
            await self._safe_upload(gh_inputs.nth(cover_slot), "cover letter", cover_letter_path)
        elif cover_letter_path and count == 1:
            logger.info("[GH] Only 1 file input present; cover letter skipped (no slot)")

    async def _safe_upload(self, locator, label: str, path: str) -> None:
        try:
            await locator.set_input_files(path, timeout=_FIELD_TIMEOUT_MS)
            logger.info(f"[GH] Uploaded {label}: {path}")
        except Exception as exc:
            logger.error(f"[GH] Upload FAILED for {label} ({path}): {exc}")

    # ──────────────────────────────────────────────────────────────────────────
    # Submit & verify
    # ──────────────────────────────────────────────────────────────────────────

    async def submit(self, page: Page) -> bool:
        ctx = self._frame_locator if self._iframe_mode else page
        # Try learned selectors first, then the hardcoded fallback list
        learned = get_learned_fixes("greenhouse").get("submit")
        hardcoded = [
            "button[type='submit']",
            "#submit_app",
            "input[type='submit']",
            "button:has-text('Submit Application')",
            "button:has-text('Submit')",
        ]
        selectors = learned + [s for s in hardcoded if s not in learned]
        for sel in selectors:
            try:
                btn = ctx.locator(sel).first
                if await btn.count() > 0:
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=_FIELD_TIMEOUT_MS)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        pass
                    await self.human_delay(1.0, 2.0)
                    logger.info(f"[GH] Clicked submit via: {sel}")
                    return True
            except Exception as exc:
                logger.debug(f"[GH] Submit selector '{sel}' failed: {exc}")
        logger.error("[GH] Could not find a submit button")
        return False

    async def verify_success(self, page: Page) -> Tuple[bool, Optional[str]]:
        # Check page content first, then the frame if in iframe mode
        from ..frame_utils import get_live_frame
        live_frame = await get_live_frame(self._frame_locator) if self._iframe_mode else None
        for ctx in ([live_frame, page] if self._iframe_mode else [page]):
            if ctx is None:
                continue
            try:
                content = (await ctx.content()).lower()
                for pattern in (
                    "application has been submitted",
                    "thank you for applying",
                    "thank you",
                    "successfully applied",
                    "application received",
                    "we have received your application",
                ):
                    if pattern in content:
                        logger.info(f"[GH] Submission verified via pattern: '{pattern}'")
                        return True, pattern
            except Exception:
                pass
        return False, None
