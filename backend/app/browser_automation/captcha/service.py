import asyncio
import base64
import logging
import os
import random
import time
from typing import Optional

import httpx
from dotenv import load_dotenv
from playwright.async_api import Page

from .models import CaptchaSolution

load_dotenv()
logger = logging.getLogger(__name__)

_PROVIDER_ENV = {
    "2captcha":   "TWO_CAPTCHA_API_KEY",
    "anticaptcha": "ANTI_CAPTCHA_API_KEY",
    "capsolver":  "CAPSOLVER_API_KEY",
    "nopecha":    "NOPECHA_API_KEY",
    "ocilar":     "OCILAR_API_KEY",
    # "ai" provider uses the same Claude/LLM client the AgentLoop talks to —
    # no separate API key required; reuses ANTHROPIC_API_KEY / fallback chain.
    "ai":         "ANTHROPIC_API_KEY",
}

# Ocilar REST endpoint — update if their docs differ.
_OCILAR_BASE = "https://api.ocilar.com/v1"

# Token captcha types that hit a paid provider (anticaptcha/capsolver) directly
# and therefore burn timed-out attempts when the key is dead/zero-balance. The
# balance/auth gate short-circuits these; "image" is excluded because it routes
# to Ocilar OCR regardless of the configured provider.
_PAID_TOKEN_TYPES = {"recaptcha_v2", "turnstile", "hcaptcha"}


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

        # Cached result of the getBalance/auth probe (anticaptcha/capsolver only).
        # Probed at most once per instance so we don't hammer the endpoint.
        self._balance_checked = False
        self._balance_ok = True
        self._balance_reason: Optional[str] = None

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

    async def _anticaptcha_submit(self, client: httpx.AsyncClient, task: dict,
                                  client_key: Optional[str] = None) -> int:
        resp = await client.post(
            "https://api.anti-captcha.com/createTask",
            json={"clientKey": client_key or self.api_key, "task": task},
        )
        body = resp.json()
        if body.get("errorId") != 0:
            raise RuntimeError(f"AntiCaptcha submit failed: {body.get('errorDescription')}")
        return body["taskId"]

    async def _anticaptcha_poll(self, client: httpx.AsyncClient, task_id: int, polls: int = 18,
                                client_key: Optional[str] = None) -> str:
        for _ in range(polls):
            await asyncio.sleep(10)
            r = await client.post(
                "https://api.anti-captcha.com/getTaskResult",
                json={"clientKey": client_key or self.api_key, "taskId": task_id},
            )
            body = r.json()
            if body.get("errorId") != 0:
                raise RuntimeError(f"AntiCaptcha error: {body.get('errorDescription')}")
            if body.get("status") == "ready":
                sol = body.get("solution", {})
                return sol.get("gRecaptchaResponse") or sol.get("token") or sol.get("text") or ""
        raise TimeoutError("AntiCaptcha: timed out waiting for solution")

    def _resolve_anticaptcha_key(self) -> str:
        """Return the AntiCaptcha key regardless of the configured provider.

        Used to route hCaptcha to AntiCaptcha (which supports it) even when
        CAPTCHA_PROVIDER is CapSolver. Placeholder values are treated as unset.
        """
        if self.provider == "anticaptcha":
            return self.api_key
        key = os.getenv("ANTI_CAPTCHA_API_KEY", "")
        if key and key.lower().startswith("your_"):
            return ""
        return key

    async def _ensure_provider_ok(self) -> tuple[bool, Optional[str]]:
        """Lightweight getBalance/auth probe for anticaptcha & capsolver.

        Returns (ok, reason). ok=False means the configured provider reported a
        zero/negative balance or an invalid key — the caller should short-circuit
        rather than burn timed-out solve attempts. Probed at most once per
        instance (result cached). Providers without a getBalance probe, or with
        no key, return (True, None) so the real solve attempt surfaces any error.
        A network hiccup on the probe itself is non-fatal (returns ok=True).
        """
        if self._balance_checked:
            return self._balance_ok, self._balance_reason
        self._balance_checked = True

        if self.provider not in ("anticaptcha", "capsolver") or not self.api_key:
            return True, None

        url = ("https://api.anti-captcha.com/getBalance"
               if self.provider == "anticaptcha"
               else "https://api.capsolver.com/getBalance")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json={"clientKey": self.api_key})
                body = resp.json()
        except Exception as exc:
            # Don't block the real solve on a flaky probe — let it try.
            logger.warning(f"[CAPTCHA] {self.provider} getBalance probe failed (non-fatal): {exc}")
            return True, None

        if body.get("errorId") not in (0, None):
            code = body.get("errorCode") or ""
            desc = body.get("errorDescription") or code or "unknown error"
            logger.error(f"[CAPTCHA] {self.provider} auth check failed: {desc} ({code})")
            self._balance_ok = False
            self._balance_reason = f"{self.provider} key rejected: {desc}"
            return False, self._balance_reason

        balance = body.get("balance")
        if isinstance(balance, (int, float)) and balance <= 0:
            logger.error(f"[CAPTCHA] {self.provider} balance is {balance} — cannot solve captchas")
            self._balance_ok = False
            self._balance_reason = f"{self.provider} balance exhausted (${balance})"
            return False, self._balance_reason

        logger.info(f"[CAPTCHA] {self.provider} balance OK (${balance})")
        return True, None

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

    async def _capsolver_poll_turnstile(self, client: httpx.AsyncClient, task_id: str,
                                        polls: int = 20, interval: float = 3.0) -> str:
        """Poll a CapSolver AntiTurnstileTaskProxyLess task for its token.

        Kept separate from _capsolver_poll because the Turnstile solution lives
        under solution.token (not gRecaptchaResponse), and Turnstile tokens
        expire fast (~120s), so this polls more aggressively (every 3s, 20x).
        """
        for _ in range(polls):
            await asyncio.sleep(interval)
            r = await client.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": self.api_key, "taskId": task_id},
            )
            body = r.json()
            if body.get("errorId") != 0:
                raise RuntimeError(f"CapSolver error: {body.get('errorDescription')}")
            if body.get("status") == "ready":
                sol = body.get("solution", {})
                return sol.get("token") or ""
        raise TimeoutError("CapSolver: timed out waiting for Turnstile solution")

    # ──────────────────────────────────────────────────────────────────────────
    # NopeCHA — Token API (https://nopecha.com/api-reference). Unlike
    # 2Captcha/AntiCaptcha/CapSolver's createTask+poll shape, NopeCHA's submit
    # response is `{"data": job_id}` directly (no errorId wrapper) and the
    # poll endpoint is a GET with the job id in the query string, returning
    # HTTP 409 (not a body flag) while the solve is still in progress.
    # ──────────────────────────────────────────────────────────────────────────

    async def _nopecha_submit_hcaptcha(self, client: httpx.AsyncClient, site_key: str, page_url: str) -> str:
        resp = await client.post(
            "https://api.nopecha.com/v1/token/hcaptcha",
            json={"key": self.api_key, "sitekey": site_key, "url": page_url},
        )
        body = resp.json()
        if resp.status_code != 200 or "data" not in body:
            raise RuntimeError(f"NopeCHA submit failed: {body}")
        return body["data"]  # job id

    async def _nopecha_poll(self, client: httpx.AsyncClient, job_id: str, polls: int = 40) -> str:
        for _ in range(polls):
            await asyncio.sleep(0.5)
            r = await client.get(
                "https://api.nopecha.com/v1/token/hcaptcha",
                params={"key": self.api_key, "id": job_id},
            )
            if r.status_code == 409:
                continue  # still solving
            body = r.json()
            if r.status_code != 200 or "data" not in body:
                raise RuntimeError(f"NopeCHA error: {body}")
            return body["data"]  # solved token
        raise TimeoutError("NopeCHA: timed out waiting for solution")

    # ──────────────────────────────────────────────────────────────────────────
    # Ocilar OCR
    # ──────────────────────────────────────────────────────────────────────────

    async def _ocilar_solve_image(self, client: httpx.AsyncClient, image_b64: str, api_key: Optional[str] = None) -> str:
        """Submit a base64-encoded captcha image to Ocilar and return the OCR text.

        Ocilar uses Bearer-token auth (sk-... key format).  The endpoint below is
        the documented REST path — verify against https://ocilar.com/docs if it
        returns 404 and update _OCILAR_BASE accordingly.
        """
        key = api_key or self.api_key
        resp = await client.post(
            f"{_OCILAR_BASE}/solve",
            headers={"Authorization": f"Bearer {key}"},
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
            return await self._ocilar_poll(client, body["task_id"], api_key=key)
        raise RuntimeError(f"Ocilar unexpected response: {body}")

    async def _ocilar_poll(self, client: httpx.AsyncClient, task_id: str, polls: int = 12, api_key: Optional[str] = None) -> str:
        key = api_key or self.api_key
        for _ in range(polls):
            await asyncio.sleep(5)
            r = await client.get(
                f"{_OCILAR_BASE}/tasks/{task_id}",
                headers={"Authorization": f"Bearer {key}"},
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
                    logger.error(
                        f"[CAPTCHA] Provider {self.provider!r} cannot solve reCAPTCHA v2 tokens; "
                        f"use 2captcha, anticaptcha, or capsolver for this type."
                    )
                    return CaptchaSolution(
                        captcha_type="recaptcha_v2", success=False,
                        error=f"CAPTCHA_UNSUPPORTED: recaptcha_v2 not supported by provider {self.provider!r}",
                        solve_time_seconds=time.monotonic() - start, cost_usd=0)
        except Exception as exc:
            logger.error(f"[CAPTCHA] solve_recaptcha_v2 ({self.provider}) failed: {exc}")
            return CaptchaSolution(captcha_type="recaptcha_v2", success=False,
                                   solve_time_seconds=time.monotonic() - start, cost_usd=0)

        await asyncio.sleep(0)  # yield
        return CaptchaSolution(captcha_type="recaptcha_v2", token=token, success=True,
                               solve_time_seconds=time.monotonic() - start, cost_usd=0.002)

    async def _solve_hcaptcha_anticaptcha(self, client: httpx.AsyncClient, site_key: str,
                                          page_url: str, api_key: str) -> str:
        """Solve hCaptcha via AntiCaptcha's HCaptchaTaskProxyless, with an explicit key."""
        tid = await self._anticaptcha_submit(client, {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }, client_key=api_key)
        return await self._anticaptcha_poll(client, tid, client_key=api_key)

    async def solve_hcaptcha(self, site_key: str, page_url: str) -> CaptchaSolution:
        start = time.monotonic()
        # AntiCaptcha is the reliable hCaptcha solver in our setup; CapSolver
        # answers hCaptcha createTask with "We don't support this service" on the
        # current plan. Prefer AntiCaptcha whenever a key is configured — even if
        # the active provider is CapSolver — and fall back to it if CapSolver
        # rejects the service. The token-injection contract is unchanged: this
        # returns the raw token and the caller injects it.
        anti_key = self._resolve_anticaptcha_key()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if self.provider == "2captcha":
                    rid = await self._2captcha_submit(client, {
                        "method": "hcaptcha",
                        "sitekey": site_key,
                        "pageurl": page_url,
                    })
                    token = await self._2captcha_poll(client, rid)
                elif self.provider == "nopecha":
                    jid = await self._nopecha_submit_hcaptcha(client, site_key, page_url)
                    token = await self._nopecha_poll(client, jid)
                elif self.provider == "anticaptcha":
                    token = await self._solve_hcaptcha_anticaptcha(
                        client, site_key, page_url, anti_key or self.api_key)
                elif self.provider == "capsolver":
                    if anti_key:
                        logger.info("[CAPTCHA] Routing hCaptcha to AntiCaptcha "
                                    "(preferred over CapSolver for this type).")
                        token = await self._solve_hcaptcha_anticaptcha(
                            client, site_key, page_url, anti_key)
                    else:
                        try:
                            tid = await self._capsolver_submit(client, {
                                "type": "HCaptchaTaskProxyless",
                                "websiteURL": page_url,
                                "websiteKey": site_key,
                            })
                            token = await self._capsolver_poll(client, tid)
                        except Exception as cap_exc:
                            _m = str(cap_exc).lower()
                            if "support this service" in _m or "don't support" in _m:
                                logger.error(
                                    "[CAPTCHA] CapSolver does NOT support hCaptcha for this "
                                    "account/site (\"We don't support this service\") and no "
                                    "ANTI_CAPTCHA_API_KEY is configured to fall back to. This "
                                    "is a provider plan limitation, not a code bug."
                                )
                                return CaptchaSolution(
                                    captcha_type="hcaptcha", success=False,
                                    error=("CAPTCHA_UNSUPPORTED: hcaptcha unsupported by CapSolver "
                                           "plan and no AntiCaptcha key configured"),
                                    solve_time_seconds=time.monotonic() - start, cost_usd=0)
                            raise
                else:
                    return CaptchaSolution(
                        captcha_type="hcaptcha", success=False,
                        error=f"CAPTCHA_UNSUPPORTED: hcaptcha not supported by provider {self.provider!r}",
                        solve_time_seconds=time.monotonic() - start, cost_usd=0)
        except Exception as exc:
            logger.error(f"[CAPTCHA] solve_hcaptcha ({self.provider}) failed: {exc}")
            return CaptchaSolution(captcha_type="hcaptcha", success=False,
                                   solve_time_seconds=time.monotonic() - start, cost_usd=0)

        return CaptchaSolution(captcha_type="hcaptcha", token=token, success=True,
                               solve_time_seconds=time.monotonic() - start, cost_usd=0.002)

    async def solve_turnstile(self, page: Page, sitekey: str, action: str = "",
                              cdata: str = "") -> CaptchaSolution:
        """Solve a Cloudflare Turnstile challenge via CapSolver or Anti-Captcha and inject the token.

        Both CapSolver and Anti-Captcha are supported for Turnstile.
        The token is injected IMMEDIATELY after solving because Turnstile tokens
        expire in ~120s — any delay between solve and submit risks a stale token.
        """
        start = time.monotonic()
        if self.provider not in ("capsolver", "anticaptcha"):
            logger.warning(
                f"[CAPTCHA] Turnstile solving requires provider 'capsolver' or 'anticaptcha' "
                f"(current provider={self.provider!r}) — skipping"
            )
            return CaptchaSolution(
                captcha_type="turnstile", success=False,
                error=f"CAPTCHA_UNSUPPORTED: turnstile requires capsolver/anticaptcha (provider={self.provider!r})",
                solve_time_seconds=0, cost_usd=0)

        # Sanity-check the sitekey. Cloudflare Turnstile keys are always
        # "0x…"-prefixed (this is also what _extract_turnstile_sitekey() keys off
        # of). A non-0x value means we extracted the wrong widget's key (e.g. a
        # reCAPTCHA sitekey) — submitting it would trigger the exact
        # "Recaptcha server reported that site key is invalid" failure and burn
        # every retry, so clean-fail immediately instead.
        if not sitekey or not sitekey.startswith("0x"):
            logger.error(
                f"[CAPTCHA] Turnstile sitekey {sitekey!r} is not a valid Turnstile key "
                f"(expected '0x…'); aborting to avoid a guaranteed 'site key invalid' error."
            )
            return CaptchaSolution(
                captcha_type="turnstile", success=False,
                error=f"CAPTCHA_UNSUPPORTED: turnstile sitekey malformed (expected 0x-prefixed, got {sitekey!r})",
                solve_time_seconds=0, cost_usd=0)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if self.provider == "capsolver":
                    # CapSolver's AntiTurnstileTaskProxyLess is the correct task
                    # type for Turnstile. action/cData go in metadata ONLY when the
                    # widget actually sets them — a bogus default ("managed" is a
                    # widget mode, not an action) produces a token that fails
                    # server-side verification, so omit metadata otherwise.
                    task: dict = {
                        "type": "AntiTurnstileTaskProxyLess",
                        "websiteURL": page.url,
                        "websiteKey": sitekey,
                    }
                    metadata: dict = {}
                    if action:
                        metadata["action"] = action
                    if cdata:
                        metadata["cdata"] = cdata
                    if metadata:
                        task["metadata"] = metadata
                    tid = await self._capsolver_submit(client, task)
                    token = await self._capsolver_poll_turnstile(client, tid)
                else:  # anticaptcha
                    # TurnstileTaskProxyless is the correct Turnstile task type —
                    # NOT a reCAPTCHA task (NoCaptchaTaskProxyless), which is what
                    # yields "Recaptcha server reported that site key is invalid".
                    # action/cData are optional and only echoed back when present.
                    task = {
                        "type": "TurnstileTaskProxyless",
                        "websiteURL": page.url,
                        "websiteKey": sitekey,
                    }
                    if action:
                        task["action"] = action
                    if cdata:
                        task["cData"] = cdata
                    tid = await self._anticaptcha_submit(client, task)
                    token = await self._anticaptcha_poll(client, tid, polls=20)
        except Exception as exc:
            _m = str(exc).lower()
            if ("site key is invalid" in _m or "invalid sitekey" in _m
                    or "invalid site key" in _m or "recaptcha_invalid_sitekey" in _m):
                # The sitekey passed the 0x check, so the provider rejecting it
                # means proxyless solving of this (managed-mode) Turnstile can't
                # work from our IP — retrying with the same key/IP is futile.
                # Clean-fail so the executor maps it to a terminal BLOCKED.
                no_proxy = not os.getenv("PROXY_URL")
                reason = (f"turnstile rejected by {self.provider} "
                          f"(managed-mode/proxyless unsolvable or wrong sitekey"
                          f"{'; no residential PROXY_URL configured' if no_proxy else ''})")
                logger.error(f"[CAPTCHA] {reason}: {exc}")
                return CaptchaSolution(
                    captcha_type="turnstile", success=False,
                    error=f"CAPTCHA_UNSUPPORTED: {reason}",
                    solve_time_seconds=time.monotonic() - start, cost_usd=0)
            logger.error(f"[CAPTCHA] solve_turnstile ({self.provider}) failed: {exc}")
            return CaptchaSolution(captcha_type="turnstile", success=False,
                                   solve_time_seconds=time.monotonic() - start, cost_usd=0)

        if not token:
            logger.warning(f"[CAPTCHA] {self.provider} returned an empty Turnstile token")
            return CaptchaSolution(captcha_type="turnstile", success=False,
                                   solve_time_seconds=time.monotonic() - start, cost_usd=0)

        # Inject immediately — token is short-lived. Pass the token as an ARGUMENT
        # (never interpolate into JS source) to avoid breaking on quotes/injection.
        try:
            await self._inject_turnstile_token(page, token)
        except Exception as exc:
            logger.warning(f"[CAPTCHA] Turnstile token injection raised (non-fatal): {exc}")

        return CaptchaSolution(captcha_type="turnstile", token=token, success=True,
                               solve_time_seconds=time.monotonic() - start, cost_usd=0.002)

    async def _inject_turnstile_token(self, page: Page, token: str) -> None:
        """Write the Turnstile token into every response input + invoke the widget callback."""
        for frame in page.frames:
            try:
                await frame.evaluate("""(token) => {
                    // Turnstile writes its token into a hidden input named
                    // "cf-turnstile-response"; some pages render several widgets.
                    const els = document.querySelectorAll('[name="cf-turnstile-response"]');
                    els.forEach((el) => { el.value = token; });
                    // Some integrations reuse the reCAPTCHA-style hidden input name.
                    const gr = document.querySelector('input[name="g-recaptcha-response"]');
                    if (gr) gr.value = token;
                    // Best-effort: if the widget is scripted, hand the token to its callback.
                    try {
                        const container = document.querySelector('.cf-turnstile');
                        if (container) {
                            const cb = container.getAttribute('data-callback');
                            if (cb && typeof window[cb] === 'function') window[cb](token);
                        }
                    } catch (e) { /* callback wiring is best-effort */ }
                }""", token)
            except Exception:
                pass

    async def _extract_turnstile_sitekey(self, page: Page) -> Optional[str]:
        """Locate the Cloudflare Turnstile sitekey on the current page.

        Checks, in order: the canonical .cf-turnstile[data-sitekey] widget; any
        [data-sitekey] element that otherwise looks Turnstile-related; and finally
        the challenges.cloudflare.com iframe src (which embeds the key in its path
        or query). Returns None if no sitekey can be found.
        """
        try:
            # Check all frames for .cf-turnstile element or data-sitekey
            for frame in page.frames:
                try:
                    el = await frame.query_selector(".cf-turnstile[data-sitekey]")
                    if el:
                        key = await el.get_attribute("data-sitekey")
                        if key:
                            return key

                    # Any element carrying a data-sitekey whose class mentions turnstile.
                    candidates = await frame.query_selector_all("[data-sitekey]")
                    for cand in candidates:
                        cls = (await cand.get_attribute("class") or "").lower()
                        if "turnstile" in cls or "cf-" in cls:
                            key = await cand.get_attribute("data-sitekey")
                            if key:
                                return key
                except Exception:
                    pass

            # Fallback: parse the sitekey out of the Cloudflare challenge iframe URL from any frame.
            for frame in page.frames:
                try:
                    url = frame.url
                    if "challenges.cloudflare.com" in url.lower():
                        import urllib.parse
                        parsed = urllib.parse.urlparse(url)
                        # Turnstile embeds the sitekey in the query (?sitekey=... / ?k=...)
                        qs = urllib.parse.parse_qs(parsed.query)
                        for param in ("sitekey", "k"):
                            if qs.get(param) and qs[param][0]:
                                return qs[param][0]
                        # …or as a path segment like /turnstile/if/ov2/av0/rcv/<sitekey>/...
                        for seg in parsed.path.split("/"):
                            if seg.startswith("0x"):  # Turnstile sitekeys start with 0x
                                return seg
                except Exception:
                    pass
        except Exception as exc:
            logger.warning(f"[CAPTCHA] Turnstile sitekey extraction failed: {exc}")
        return None

    async def _extract_turnstile_action(self, page: Page) -> tuple[str, str]:
        """Read the Turnstile widget's data-action / data-cdata attributes.

        Sites that set an explicit action require the solver to echo it back, or
        the token fails server-side verification. Returns ("", "") when absent so
        callers fall back to the "managed" default.
        """
        try:
            for frame in page.frames:
                try:
                    el = await frame.query_selector(".cf-turnstile[data-sitekey]") \
                        or await frame.query_selector(".cf-turnstile")
                    if el:
                        action = await el.get_attribute("data-action") or ""
                        cdata = await el.get_attribute("data-cdata") or ""
                        return action, cdata
                except Exception:
                    pass
        except Exception as exc:
            logger.warning(f"[CAPTCHA] Turnstile action extraction failed: {exc}")
        return "", ""

    async def solve_image_captcha(self, page: Page, captcha_type: str) -> CaptchaSolution:
        """Solve an image/text captcha using Ocilar OCR.

        Always authenticates with the dedicated OCILAR_API_KEY, regardless of
        which provider is configured as self.provider. solve() dispatches here
        for captcha_type == "image" even when CAPTCHA_PROVIDER is "capsolver"
        etc. — without this, self.api_key would hold that other provider's key
        and every call would 401 against Ocilar's endpoint until the separate
        end-of-solve() Ocilar fallback (which builds its own correctly-keyed
        instance) finally took over.
        """
        start = time.monotonic()
        image_b64 = await self._capture_captcha_image_b64(page, captcha_type)
        if not image_b64:
            return CaptchaSolution(captcha_type=captcha_type, success=False,  # type: ignore[arg-type]
                                   solve_time_seconds=0, cost_usd=0)
        ocilar_key = os.getenv("OCILAR_API_KEY", "") if self.provider != "ocilar" else self.api_key
        if not ocilar_key:
            logger.warning("[CAPTCHA] No OCILAR_API_KEY configured — cannot solve image captcha")
            return CaptchaSolution(captcha_type=captcha_type, success=False,  # type: ignore[arg-type]
                                   solve_time_seconds=time.monotonic() - start, cost_usd=0)
        try:
            async with httpx.AsyncClient() as client:
                text = await self._ocilar_solve_image(client, image_b64, api_key=ocilar_key)
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
            import asyncio

            import freecaptcha

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

        elif captcha_type == "turnstile":
            sitekey = await self._extract_turnstile_sitekey(page)
            if not sitekey:
                logger.warning("[CAPTCHA] Turnstile sitekey not found")
                return CaptchaSolution(captcha_type="turnstile", success=False,
                                       solve_time_seconds=0, cost_usd=0)
            action, cdata = await self._extract_turnstile_action(page)
            return await self.solve_turnstile(page, sitekey, action=action, cdata=cdata)

        elif captcha_type == "image":
            return await self.solve_image_captcha(page, captcha_type)

        logger.warning(f"[CAPTCHA] Unsupported captcha type: {captcha_type}")
        return CaptchaSolution(captcha_type=captcha_type, success=False,  # type: ignore[arg-type]
                               error=f"CAPTCHA_UNSUPPORTED: unknown captcha type {captcha_type!r}",
                               solve_time_seconds=0, cost_usd=0)

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

        # ── Provider auth/balance gate ──────────────────────────────────────────
        # Probe getBalance once (anticaptcha/capsolver only) BEFORE committing to
        # timed-out retries. A dead key or zero balance short-circuits the paid
        # loops so we don't burn multiple ~10-28s attempts against a wall. The AI
        # / Whisper solvers below are free and still run regardless.
        paid_ok, paid_reason = await self._ensure_provider_ok()

        # ── Priority Phase: anticaptcha first ──────────────────────────────────
        # If the provider is anticaptcha and it has a valid API key + balance, run
        # it first as priority instead of fallback.
        if self.provider == "anticaptcha" and self.api_key and paid_ok:
            logger.info("[CAPTCHA] anticaptcha key detected and configured as priority. Solving via anticaptcha first.")
            last: Optional[CaptchaSolution] = None
            for attempt in range(1, max_attempts + 1):
                logger.info(f"[CAPTCHA] Priority Attempt {attempt}/{max_attempts} | type={captcha_type} | provider=anticaptcha")
                try:
                    solution = await self._perform_single_solve(page, captcha_type)
                    logger.info(f"[CAPTCHA] Priority Attempt {attempt} result: success={solution.success}, "
                                f"time={solution.solve_time_seconds:.1f}s, cost=${solution.cost_usd:.4f}")
                    if solution.success:
                        return solution
                    # Genuinely unsolvable (unsupported type / managed-mode / bad
                    # sitekey) — retrying is futile, bail immediately.
                    if solution.error and solution.error.startswith("CAPTCHA_UNSUPPORTED:"):
                        logger.error(f"[CAPTCHA] Unsupported captcha — not retrying: {solution.error}")
                        return solution
                    last = solution
                except Exception as exc:
                    logger.error(f"[CAPTCHA] Priority Attempt {attempt} raised exception: {exc}")

                if attempt < max_attempts:
                    jitter = random.uniform(2, 5)
                    logger.info(f"[CAPTCHA] Refreshing captcha widget before retry (wait {jitter:.1f}s)...")
                    await self._refresh_captcha_widget(page, captcha_type)
                    await asyncio.sleep(jitter)

            logger.warning("[CAPTCHA] anticaptcha priority solve failed or exhausted all attempts; falling back to default AI/Whisper solvers.")

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
            logger.info("[CAPTCHA] AI solver did not succeed; trying audio-challenge (Whisper)…")
        except Exception as exc:
            logger.warning(f"[CAPTCHA] AI solver raised (non-fatal): {exc}")

        # ── Phase 0.5: Whisper-based audio-challenge solver ─────────────────
        # Technique vendored from https://github.com/ibedevesh/capsolver (MIT).
        # Runs entirely offline once the Whisper model is downloaded; no
        # API key required. Only applies to reCAPTCHA v2.
        if captcha_type == "recaptcha_v2":
            try:
                from .audio_solver import (
                    detect_recaptcha_v2,
                    solve_recaptcha_v2_via_audio,
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

        # Skip the paid retry loop for token types when the provider auth/balance
        # gate already reported the configured provider is dead/zero-balance —
        # retrying would only burn timed-out attempts. ("image" is exempt because
        # it routes to Ocilar, independent of the configured provider.)
        _skip_paid = (not paid_ok) and (captcha_type in _PAID_TOKEN_TYPES)
        if _skip_paid:
            logger.error(
                f"[CAPTCHA] Skipping {max_attempts} paid attempts for type={captcha_type}: "
                f"{self.provider} unavailable ({paid_reason})."
            )
        else:
            for attempt in range(1, max_attempts + 1):
                logger.info(f"[CAPTCHA] Attempt {attempt}/{max_attempts} | type={captcha_type} | provider={self.provider}")
                try:
                    solution = await self._perform_single_solve(page, captcha_type)
                    logger.info(f"[CAPTCHA] Attempt {attempt} result: success={solution.success}, "
                                f"time={solution.solve_time_seconds:.1f}s, cost=${solution.cost_usd:.4f}")
                    if solution.success:
                        return solution
                    # Genuinely unsolvable — bail immediately instead of retrying.
                    if solution.error and solution.error.startswith("CAPTCHA_UNSUPPORTED:"):
                        logger.error(f"[CAPTCHA] Unsupported captcha — not retrying: {solution.error}")
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

        # Paid loop was skipped because the configured provider is dead/exhausted
        # and no free solver succeeded — surface a distinguishable clean-fail so
        # the executor maps it to a terminal BLOCKED rather than a generic error.
        if _skip_paid and not (last and last.success):
            return CaptchaSolution(captcha_type=captcha_type, success=False,  # type: ignore[arg-type]
                                   error=f"CAPTCHA_UNSUPPORTED: {paid_reason}",
                                   solve_time_seconds=0, cost_usd=0)

        logger.error(f"[CAPTCHA] All {max_attempts} attempts failed for type={captcha_type}, provider={self.provider}")
        return last or CaptchaSolution(captcha_type=captcha_type, success=False,  # type: ignore[arg-type]
                                       solve_time_seconds=0, cost_usd=0)
