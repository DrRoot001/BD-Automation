import os
import base64
import time
import asyncio
import random
import logging
import httpx
from typing import Literal, Optional
from playwright.async_api import Page
from dotenv import load_dotenv
from .models import CaptchaSolution

load_dotenv()
logger = logging.getLogger(__name__)

_PROVIDER_ENV = {
    "2captcha":   "TWO_CAPTCHA_API_KEY",
    "anticaptcha": "ANTI_CAPTCHA_API_KEY",
    "capsolver":  "CAPSOLVER_API_KEY",
    "ocilar":     "OCILAR_API_KEY",
    # "ai" provider uses the same Claude/LLM client the AgentLoop talks to —
    # no separate API key required; reuses ANTHROPIC_API_KEY / fallback chain.
    "ai":         "ANTHROPIC_API_KEY",
}

# Ocilar REST endpoint — update if their docs differ.
_OCILAR_BASE = "https://api.ocilar.com/v1"


class CaptchaService:
    def __init__(self, api_key: Optional[str] = None, provider: str = "2captcha"):
        self.provider = provider.lower()
        if self.provider not in _PROVIDER_ENV:
            raise ValueError(f"Provider must be one of {list(_PROVIDER_ENV)}")

        self.api_key = api_key or os.getenv(_PROVIDER_ENV[self.provider], "")
        # Treat placeholder values ("your_2captcha_api_key_here") as missing —
        # otherwise the service raises and the AgentLoop's submit aborts.
        if self.api_key and self.api_key.lower().startswith("your_"):
            logger.warning(
                f"[CAPTCHA] Provider {self.provider!r} key looks like a placeholder "
                f"({self.api_key[:20]!r}…) — treating as unconfigured"
            )
            self.api_key = ""
        # "ai" provider needs no external key — we use the LLM client's chain.
        if not self.api_key and self.provider != "ai":
            logger.warning(
                f"[CAPTCHA] Provider {self.provider!r} has no API key configured. "
                "The AI vision solver will still run first; paid provider will be skipped."
            )

    # ──────────────────────────────────────────────────────────────────────────
    # 2Captcha
    # ──────────────────────────────────────────────────────────────────────────

    async def _2captcha_submit(self, client: httpx.AsyncClient, data: dict) -> str:
        resp = await client.post("https://2captcha.com/in.php", data={**data, "key": self.api_key, "json": 1})
        body = resp.json()
        if body.get("status") != 1:
            raise RuntimeError(f"2Captcha submit failed: {body.get('request')}")
        return body["request"]  # request_id

    async def _2captcha_poll(self, client: httpx.AsyncClient, request_id: str, polls: int = 18) -> str:
        url = f"https://2captcha.com/res.php?key={self.api_key}&action=get&id={request_id}&json=1"
        for _ in range(polls):
            await asyncio.sleep(10)
            r = await client.get(url)
            body = r.json()
            if body.get("status") == 1:
                return body["request"]
            if body.get("request") not in ("CAPCHA_NOT_READY", "ERROR_CAPTCHA_UNSOLVABLE"):
                raise RuntimeError(f"2Captcha poll error: {body.get('request')}")
        raise TimeoutError("2Captcha: timed out waiting for solution")

    # ──────────────────────────────────────────────────────────────────────────
    # AntiCaptcha
    # ──────────────────────────────────────────────────────────────────────────

    async def _anticaptcha_submit(self, client: httpx.AsyncClient, task: dict) -> int:
        resp = await client.post(
            "https://api.anti-captcha.com/createTask",
            json={"clientKey": self.api_key, "task": task},
        )
        body = resp.json()
        if body.get("errorId") != 0:
            raise RuntimeError(f"AntiCaptcha submit failed: {body.get('errorDescription')}")
        return body["taskId"]

    async def _anticaptcha_poll(self, client: httpx.AsyncClient, task_id: int, polls: int = 18) -> str:
        for _ in range(polls):
            await asyncio.sleep(10)
            r = await client.post(
                "https://api.anti-captcha.com/getTaskResult",
                json={"clientKey": self.api_key, "taskId": task_id},
            )
            body = r.json()
            if body.get("errorId") != 0:
                raise RuntimeError(f"AntiCaptcha error: {body.get('errorDescription')}")
            if body.get("status") == "ready":
                sol = body.get("solution", {})
                return sol.get("gRecaptchaResponse") or sol.get("text") or ""
        raise TimeoutError("AntiCaptcha: timed out waiting for solution")

    # ──────────────────────────────────────────────────────────────────────────
    # CapSolver
    # ──────────────────────────────────────────────────────────────────────────

    async def _capsolver_submit(self, client: httpx.AsyncClient, task: dict) -> str:
        resp = await client.post(
            "https://api.capsolver.com/createTask",
            json={"clientKey": self.api_key, "task": task},
        )
        body = resp.json()
        if body.get("errorId") != 0:
            raise RuntimeError(f"CapSolver submit failed: {body.get('errorDescription')}")
        return body["taskId"]

    async def _capsolver_poll(self, client: httpx.AsyncClient, task_id: str, polls: int = 18) -> str:
        for _ in range(polls):
            await asyncio.sleep(10)
            r = await client.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": self.api_key, "taskId": task_id},
            )
            body = r.json()
            if body.get("errorId") != 0:
                raise RuntimeError(f"CapSolver error: {body.get('errorDescription')}")
            if body.get("status") == "ready":
                sol = body.get("solution", {})
                return sol.get("gRecaptchaResponse") or sol.get("text") or ""
        raise TimeoutError("CapSolver: timed out waiting for solution")

    # ──────────────────────────────────────────────────────────────────────────
    # Ocilar OCR
    # ──────────────────────────────────────────────────────────────────────────

    async def _ocilar_solve_image(self, client: httpx.AsyncClient, image_b64: str) -> str:
        """Submit a base64-encoded captcha image to Ocilar and return the OCR text.

        Ocilar uses Bearer-token auth (sk-... key format).  The endpoint below is
        the documented REST path — verify against https://ocilar.com/docs if it
        returns 404 and update _OCILAR_BASE accordingly.
        """
        resp = await client.post(
            f"{_OCILAR_BASE}/solve",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"image": image_b64, "type": "text"},
            timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Ocilar API error {resp.status_code}: {resp.text}")
        body = resp.json()
        # Support both sync ({"text": "abc"}) and async ({"task_id": "..."}) responses.
        if "text" in body:
            return body["text"]
        if "solution" in body:
            return body["solution"].get("text", "")
        if "task_id" in body:
            return await self._ocilar_poll(client, body["task_id"])
        raise RuntimeError(f"Ocilar unexpected response: {body}")

    async def _ocilar_poll(self, client: httpx.AsyncClient, task_id: str, polls: int = 12) -> str:
        for _ in range(polls):
            await asyncio.sleep(5)
            r = await client.get(
                f"{_OCILAR_BASE}/tasks/{task_id}",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            body = r.json()
            if body.get("status") == "ready":
                return body.get("solution", {}).get("text", "")
            if body.get("status") == "error":
                raise RuntimeError(f"Ocilar task failed: {body.get('error')}")
        raise TimeoutError("Ocilar: timed out waiting for solution")

    async def _capture_captcha_image_b64(self, page: Page, captcha_type: str) -> Optional[str]:
        """Screenshot the captcha widget and return as base64 PNG."""
        try:
            sel = ".g-recaptcha" if captcha_type == "recaptcha_v2" else ".h-captcha"
            el = await page.query_selector(sel)
            if el:
                buf = await el.screenshot()
                return base64.b64encode(buf).decode()
        except Exception as exc:
            logger.warning(f"[CAPTCHA] Could not screenshot captcha element: {exc}")
        return None

    # ──────────────────────────────────────────────────────────────────────────
    # Per-type solve helpers (single attempt)
    # ──────────────────────────────────────────────────────────────────────────

    async def solve_recaptcha_v2(self, site_key: str, page_url: str) -> CaptchaSolution:
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if self.provider == "2captcha":
                    rid = await self._2captcha_submit(client, {
                        "method": "userrecaptcha",
                        "googlekey": site_key,
                        "pageurl": page_url,
                    })
                    token = await self._2captcha_poll(client, rid)
                elif self.provider == "anticaptcha":
                    tid = await self._anticaptcha_submit(client, {
                        "type": "NoCaptchaTaskProxyless",
                        "websiteURL": page_url,
                        "websiteKey": site_key,
                    })
                    token = await self._anticaptcha_poll(client, tid)
                elif self.provider == "capsolver":
                    tid = await self._capsolver_submit(client, {
                        "type": "NoCaptchaTaskProxyless",
                        "websiteURL": page_url,
                        "websiteKey": site_key,
                    })
                    token = await self._capsolver_poll(client, tid)
                else:
                    raise RuntimeError("Provider does not support reCAPTCHA v2 token solving; "
                                       "use 2captcha, anticaptcha, or capsolver for this type.")
        except Exception as exc:
            logger.error(f"[CAPTCHA] solve_recaptcha_v2 ({self.provider}) failed: {exc}")
            return CaptchaSolution(captcha_type="recaptcha_v2", success=False,
                                   solve_time_seconds=time.monotonic() - start, cost_usd=0)

        await asyncio.sleep(0)  # yield
        return CaptchaSolution(captcha_type="recaptcha_v2", token=token, success=True,
                               solve_time_seconds=time.monotonic() - start, cost_usd=0.002)

    async def solve_hcaptcha(self, site_key: str, page_url: str) -> CaptchaSolution:
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if self.provider == "2captcha":
                    rid = await self._2captcha_submit(client, {
                        "method": "hcaptcha",
                        "sitekey": site_key,
                        "pageurl": page_url,
                    })
                    token = await self._2captcha_poll(client, rid)
                elif self.provider == "anticaptcha":
                    tid = await self._anticaptcha_submit(client, {
                        "type": "HCaptchaTaskProxyless",
                        "websiteURL": page_url,
                        "websiteKey": site_key,
                    })
                    token = await self._anticaptcha_poll(client, tid)
                elif self.provider == "capsolver":
                    tid = await self._capsolver_submit(client, {
                        "type": "HCaptchaTaskProxyless",
                        "websiteURL": page_url,
                        "websiteKey": site_key,
                    })
                    token = await self._capsolver_poll(client, tid)
                else:
                    raise RuntimeError("Provider does not support hCaptcha token solving.")
        except Exception as exc:
            logger.error(f"[CAPTCHA] solve_hcaptcha ({self.provider}) failed: {exc}")
            return CaptchaSolution(captcha_type="hcaptcha", success=False,
                                   solve_time_seconds=time.monotonic() - start, cost_usd=0)

        return CaptchaSolution(captcha_type="hcaptcha", token=token, success=True,
                               solve_time_seconds=time.monotonic() - start, cost_usd=0.002)

    async def solve_image_captcha(self, page: Page, captcha_type: str) -> CaptchaSolution:
        """Solve an image/text captcha using Ocilar OCR."""
        start = time.monotonic()
        image_b64 = await self._capture_captcha_image_b64(page, captcha_type)
        if not image_b64:
            return CaptchaSolution(captcha_type=captcha_type, success=False,  # type: ignore[arg-type]
                                   solve_time_seconds=0, cost_usd=0)
        try:
            async with httpx.AsyncClient() as client:
                text = await self._ocilar_solve_image(client, image_b64)
            if text:
                return CaptchaSolution(captcha_type=captcha_type, token=text, success=True,  # type: ignore[arg-type]
                                       solve_time_seconds=time.monotonic() - start, cost_usd=0.001)
        except Exception as exc:
            logger.error(f"[CAPTCHA] Ocilar image solve failed: {exc}")
        return CaptchaSolution(captcha_type=captcha_type, success=False,  # type: ignore[arg-type]
                               solve_time_seconds=time.monotonic() - start, cost_usd=0)

    # ──────────────────────────────────────────────────────────────────────────
    # Inject token into DOM
    # ──────────────────────────────────────────────────────────────────────────

    async def _inject_recaptcha_token(self, page: Page, token: str) -> None:
        await page.evaluate("""(token) => {
            const el = document.getElementById('g-recaptcha-response');
            if (el) el.innerHTML = token;
            const container = document.querySelector('.g-recaptcha');
            if (container) {
                const cb = container.getAttribute('data-callback');
                if (cb && window[cb]) window[cb](token);
            }
        }""", token)

    async def _inject_hcaptcha_token(self, page: Page, token: str) -> None:
        await page.evaluate("""(token) => {
            const el = document.querySelector('[name="h-captcha-response"]');
            if (el) el.value = token;
            const container = document.querySelector('.h-captcha');
            if (container) {
                const cb = container.getAttribute('data-callback');
                if (cb && window[cb]) window[cb](token);
            }
        }""", token)

    # ──────────────────────────────────────────────────────────────────────────
    # Captcha widget refresh (between retry attempts)
    # ──────────────────────────────────────────────────────────────────────────

    async def _refresh_captcha_widget(self, page: Page, captcha_type: str) -> None:
        """Reset the captcha widget so the next attempt gets a fresh challenge."""
        try:
            if captcha_type == "recaptcha_v2":
                # Click the reload button inside the reCAPTCHA iframe
                iframe_sel = "iframe[src*='recaptcha/api2/anchor'], iframe[src*='recaptcha/enterprise/anchor']"
                frame_el = await page.query_selector(iframe_sel)
                if frame_el:
                    frame_src = await frame_el.get_attribute("src") or ""
                    # Navigate into the challenge frame to click reload
                    challenge_frame = page.frame_locator("iframe[src*='recaptcha/api2/bframe'], "
                                                          "iframe[src*='recaptcha/enterprise/bframe']").first
                    reload_btn = challenge_frame.locator("#recaptcha-reload-button")
                    if await reload_btn.count() > 0:
                        await reload_btn.click()
                        await asyncio.sleep(2)
                        logger.info("[CAPTCHA] Clicked reCAPTCHA reload button")
                        return
                # Fallback: reset via JS callback
                await page.evaluate("if (window.grecaptcha) window.grecaptcha.reset();")
                await asyncio.sleep(1.5)

            elif captcha_type == "hcaptcha":
                await page.evaluate("if (window.hcaptcha) window.hcaptcha.reset();")
                await asyncio.sleep(1.5)

        except Exception as exc:
            logger.warning(f"[CAPTCHA] Refresh widget failed (non-fatal): {exc}")

    # ──────────────────────────────────────────────────────────────────────────
    # Extract site_key from page
    # ──────────────────────────────────────────────────────────────────────────

    async def _extract_site_key(self, page: Page, captcha_type: str) -> Optional[str]:
        if captcha_type == "recaptcha_v2":
            el = await page.query_selector(".g-recaptcha")
            if el:
                key = await el.get_attribute("data-sitekey")
                if key:
                    return key
            iframe = await page.query_selector("iframe[src*='recaptcha/api2/anchor'], "
                                                "iframe[src*='recaptcha/enterprise/anchor']")
            if iframe:
                src = await iframe.get_attribute("src") or ""
                import urllib.parse
                return urllib.parse.parse_qs(urllib.parse.urlparse(src).query).get("k", [None])[0]

        elif captcha_type == "hcaptcha":
            el = await page.query_selector(".h-captcha")
            if el:
                return await el.get_attribute("data-sitekey")

        return None

    # ──────────────────────────────────────────────────────────────────────────
    # reCAPTCHA v3 (freecaptcha bypass)
    # ──────────────────────────────────────────────────────────────────────────

    async def _extract_v3_anchor_url(self, page: Page) -> Optional[str]:
        """Find the reCAPTCHA v3 iframe and extract its src url."""
        try:
            iframe = await page.query_selector("iframe[src*='recaptcha/api2/anchor'], "
                                                "iframe[src*='recaptcha/enterprise/anchor']")
            if iframe:
                return await iframe.get_attribute("src")
        except Exception as exc:
            logger.warning(f"[CAPTCHA] Failed to extract v3 anchor url: {exc}")
        return None

    async def _inject_recaptcha_v3_token(self, page: Page, token: str) -> None:
        """Inject the v3 token into the form and trigger callbacks."""
        await page.evaluate("""(token) => {
            // Some forms use the same hidden input as v2
            const el = document.getElementById('g-recaptcha-response');
            if (el) el.value = token;
            
            // Try to call grecaptcha.execute callback if exists on a container
            const container = document.querySelector('.g-recaptcha');
            if (container) {
                const cb = container.getAttribute('data-callback');
                if (cb && window[cb]) window[cb](token);
            }
            
            // Dispatch event for frameworks tracking the input
            if (el) {
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }""", token)

    async def solve_recaptcha_v3(self, page: Page) -> CaptchaSolution:
        """Solve reCAPTCHA v3 using the freecaptcha library without an API key."""
        start = time.monotonic()
        anchor_url = await self._extract_v3_anchor_url(page)
        
        if not anchor_url:
            logger.info("[CAPTCHA] reCAPTCHA v3 anchor URL not found on page; trusting auto-bypass")
            return CaptchaSolution(
                captcha_type="recaptcha_v3", token="auto_bypassed",
                success=True, solve_time_seconds=0, cost_usd=0
            )
            
        logger.info("[CAPTCHA] Attempting reCAPTCHA v3 bypass with freecaptcha...")
        try:
            import freecaptcha
            import asyncio
            
            # freecaptcha uses requests/yarl synchronously, so run in executor
            token = await asyncio.get_event_loop().run_in_executor(
                None,
                freecaptcha.reCAPTCHAV3Solver.solve,
                anchor_url,
            )
            
            if token:
                logger.info("[CAPTCHA] freecaptcha v3 token acquired!")
                await self._inject_recaptcha_v3_token(page, token)
                return CaptchaSolution(
                    captcha_type="recaptcha_v3", token=token,
                    success=True, solve_time_seconds=time.monotonic() - start, cost_usd=0
                )
            else:
                logger.warning("[CAPTCHA] freecaptcha returned empty v3 token")
                
        except Exception as exc:
            logger.warning(f"[CAPTCHA] freecaptcha v3 solve failed: {exc}")
            
        # Graceful fallback: trust stealth auto-bypass
        logger.info("[CAPTCHA] Falling back to auto-bypass for reCAPTCHA v3")
        return CaptchaSolution(
            captcha_type="recaptcha_v3", token="auto_bypassed",
            success=True, solve_time_seconds=time.monotonic() - start, cost_usd=0
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Public: solve() with 3-attempt retry + Ocilar fallback
    # ──────────────────────────────────────────────────────────────────────────

    async def _perform_single_solve(self, page: Page, captcha_type: str) -> CaptchaSolution:
        """One solve attempt using the configured provider."""
        page_url = page.url
        site_key = await self._extract_site_key(page, captcha_type)
        null_sol = CaptchaSolution(captcha_type=captcha_type, success=False,  # type: ignore[arg-type]
                                   solve_time_seconds=0, cost_usd=0)

        # Guard: paid provider can only run if a real API key was given.
        # The AI solver ran above this in solve() — if we got here it already
        # failed; without a paid key there's nothing else to try.
        if self.provider != "ai" and not self.api_key:
            logger.info(f"[CAPTCHA] No paid key for {self.provider!r}; skipping external solve")
            return null_sol

        if captcha_type == "recaptcha_v2":
            if not site_key:
                logger.error("[CAPTCHA] reCAPTCHA v2 site key not found on page")
                return null_sol
            # Ocilar can't do v2 — short-circuit
            if self.provider == "ocilar":
                logger.info("[CAPTCHA] Ocilar cannot solve reCAPTCHA v2; skipping")
                return null_sol
            solution = await self.solve_recaptcha_v2(site_key, page_url)
            if solution.success and solution.token:
                await self._inject_recaptcha_token(page, solution.token)
            return solution

        elif captcha_type == "hcaptcha":
            if not site_key:
                logger.error("[CAPTCHA] hCaptcha site key not found on page")
                return null_sol
            solution = await self.solve_hcaptcha(site_key, page_url)
            if solution.success and solution.token:
                await self._inject_hcaptcha_token(page, solution.token)
            return solution

        elif captcha_type == "image":
            return await self.solve_image_captcha(page, captcha_type)

        logger.warning(f"[CAPTCHA] Unsupported captcha type: {captcha_type}")
        return null_sol

    async def solve(self, page: Page, captcha_type: str, max_attempts: int = 3) -> CaptchaSolution:
        """Solve a captcha with up to *max_attempts* retries.

        Layered strategy (AI-first):
          1. AI vision solver — silent checkbox pass / image-grid / OCR.
             Free, fast, no external API key needed. Works on a fraction
             of reCAPTCHA v2 cases and most simple OCR captchas.
          2. Configured paid provider (2Captcha / AntiCaptcha / Ocilar).
          3. Ocilar OCR fallback for image captchas.

        Between attempts the captcha widget is refreshed so the next attempt
        gets a fresh challenge.
        """
        # ── Bypass passive captchas ─────────────────────────────────────────────
        # Invisible v2 process automatically upon form submission. Let Playwright Stealth handle it natively.
        if captcha_type == "recaptcha_invisible":
            logger.info(f"[CAPTCHA] Bypassing pre-solve for passive captcha: {captcha_type}")
            return CaptchaSolution(
                captcha_type=captcha_type,
                token="auto_bypassed",
                success=True,
                solve_time_seconds=0,
                cost_usd=0
            )

        # ── reCAPTCHA v3 Bypass (freecaptcha) ───────────────────────────────────
        if captcha_type == "recaptcha_v3":
            return await self.solve_recaptcha_v3(page)

        # ── Phase 0: try the in-process AI solver first ─────────────────────
        # This is "AI is the master" applied to captchas: before we pay a
        # third-party service, give Claude a shot at it. For reCAPTCHA v2 the
        # silent checkbox pass works often on stealth-configured sessions.
        try:
            from .ai_solver import AICaptchaSolver
            ai_solver = AICaptchaSolver()
            ai_sol = await ai_solver.solve(page, captcha_type, max_attempts=2)
            if ai_sol.success:
                logger.info(f"[CAPTCHA] AI solver succeeded type={captcha_type} "
                            f"token={(ai_sol.token or '')[:24]!r}")
                if ai_sol.token and captcha_type == "recaptcha_v2" and ai_sol.token not in (
                    "checkbox_passed", "grid_solved",
                ):
                    # Real token from provider — inject. (Our AI flow returns
                    # sentinel strings instead because Google's token isn't
                    # exposed to scripts; the DOM widget commits on its own.)
                    await self._inject_recaptcha_token(page, ai_sol.token)
                return ai_sol
            logger.info(f"[CAPTCHA] AI solver did not succeed; trying audio-challenge (Whisper)…")
        except Exception as exc:
            logger.warning(f"[CAPTCHA] AI solver raised (non-fatal): {exc}")

        # ── Phase 0.5: Whisper-based audio-challenge solver ─────────────────
        # Technique vendored from https://github.com/ibedevesh/capsolver (MIT).
        # Runs entirely offline once the Whisper model is downloaded; no
        # API key required. Only applies to reCAPTCHA v2.
        if captcha_type == "recaptcha_v2":
            try:
                from .audio_solver import (
                    detect_recaptcha_v2, solve_recaptcha_v2_via_audio,
                )
                if await detect_recaptcha_v2(page):
                    audio_res = await solve_recaptcha_v2_via_audio(page, max_retries=2)
                    if audio_res.success:
                        logger.info(
                            f"[CAPTCHA] Whisper audio solver SUCCEEDED — "
                            f"token_len={len(audio_res.token or '')}"
                        )
                        return CaptchaSolution(
                            captcha_type=captcha_type,
                            token=audio_res.token or "audio_solved",
                            success=True,
                            solve_time_seconds=0,
                            cost_usd=0,
                        )
                    logger.info(
                        f"[CAPTCHA] Whisper audio solver did not succeed: "
                        f"{audio_res.error!r}; falling through to provider={self.provider}"
                    )
                else:
                    logger.debug("[CAPTCHA] no reCAPTCHA v2 widget on page; skipping Whisper")
            except Exception as exc:
                logger.warning(f"[CAPTCHA] Whisper audio solver raised (non-fatal): {exc}")

        last: Optional[CaptchaSolution] = None

        for attempt in range(1, max_attempts + 1):
            logger.info(f"[CAPTCHA] Attempt {attempt}/{max_attempts} | type={captcha_type} | provider={self.provider}")
            try:
                solution = await self._perform_single_solve(page, captcha_type)
                logger.info(f"[CAPTCHA] Attempt {attempt} result: success={solution.success}, "
                            f"time={solution.solve_time_seconds:.1f}s, cost=${solution.cost_usd:.4f}")
                if solution.success:
                    return solution
                last = solution
            except Exception as exc:
                logger.error(f"[CAPTCHA] Attempt {attempt} raised exception: {exc}")

            if attempt < max_attempts:
                jitter = random.uniform(2, 5)
                logger.info(f"[CAPTCHA] Refreshing captcha widget before retry (wait {jitter:.1f}s)...")
                await self._refresh_captcha_widget(page, captcha_type)
                await asyncio.sleep(jitter)

        # ── Ocilar fallback for image captchas ──
        ocilar_key = os.getenv("OCILAR_API_KEY", "")
        if self.provider != "ocilar" and captcha_type == "image" and ocilar_key:
            logger.info("[CAPTCHA] Primary provider exhausted. Attempting Ocilar OCR fallback...")
            try:
                ocilar_svc = CaptchaService(api_key=ocilar_key, provider="ocilar")
                fallback = await ocilar_svc.solve(page, captcha_type, max_attempts=2)
                if fallback.success:
                    logger.info("[CAPTCHA] Ocilar fallback succeeded.")
                    return fallback
            except Exception as exc:
                logger.error(f"[CAPTCHA] Ocilar fallback failed: {exc}")

        logger.error(f"[CAPTCHA] All {max_attempts} attempts failed for type={captcha_type}, provider={self.provider}")
        return last or CaptchaSolution(captcha_type=captcha_type, success=False,  # type: ignore[arg-type]
                                       solve_time_seconds=0, cost_usd=0)
