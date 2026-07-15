import asyncio
import base64
import logging
import os
import random
import time
from typing import Any, Optional

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


def resolve_captcha_provider(default: str = "anticaptcha") -> str:
    """Single source of truth for the configured captcha provider.

    Every call site MUST use this instead of an ad-hoc os.getenv(..., "<x>")
    default so provider selection is deterministic across every code path. The
    operator's funded Anti-Captcha key is the universal primary: when
    CAPTCHA_PROVIDER is unset (or set to something unknown) we fall back to
    "anticaptcha" rather than to a placeholder/free provider that would silently
    bypass the paid key.
    """
    prov = (os.getenv("CAPTCHA_PROVIDER") or default).lower().strip()
    return prov if prov in _PROVIDER_ENV else default


class CaptchaService:
    def __init__(self, api_key: Optional[str] = None, provider: Optional[str] = None):
        if provider is None:
            provider = resolve_captcha_provider()
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

    async def _anticaptcha_poll_solution(self, client: httpx.AsyncClient, task_id: int,
                                         polls: int = 24, client_key: Optional[str] = None) -> dict:
        """Like _anticaptcha_poll but returns the FULL solution object. Needed for
        AntiCloudflareTask, whose result carries a cf_clearance COOKIE (+ the
        userAgent it must be paired with), not a single token string."""
        for _ in range(polls):
            await asyncio.sleep(5)
            r = await client.post(
                "https://api.anti-captcha.com/getTaskResult",
                json={"clientKey": client_key or self.api_key, "taskId": task_id},
            )
            body = r.json()
            if body.get("errorId") != 0:
                raise RuntimeError(f"AntiCaptcha error: {body.get('errorDescription')}")
            if body.get("status") == "ready":
                return body.get("solution", {}) or {}
        raise TimeoutError("AntiCaptcha: timed out waiting for Cloudflare solution")

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
            code = (body.get("errorCode") or "").upper()
            desc = body.get("errorDescription") or code or "unknown error"
            # Only a genuine key/auth/balance rejection should gate off the
            # provider for the whole run. A transient error (rate limit, brief
            # server trouble) must NOT turn a solvable captcha into a terminal
            # BLOCKED — fall through and let the real solve surface any error.
            _FATAL = (
                "ERROR_KEY_DOES_NOT_EXIST", "ERROR_ZERO_BALANCE",
                "ERROR_ACCOUNT_SUSPENDED", "ERROR_KEY_DENIED_ACCESS",
                "ERROR_IP_BLOCKED", "ERROR_IP_BANNED",
            )
            if any(f in code for f in _FATAL):
                logger.error(f"[CAPTCHA] {self.provider} auth check failed: {desc} ({code})")
                self._balance_ok = False
                self._balance_reason = f"{self.provider} key rejected: {desc}"
                return False, self._balance_reason
            logger.warning(
                f"[CAPTCHA] {self.provider} getBalance transient error "
                f"({desc}/{code}); proceeding to a real solve attempt"
            )
            return True, None

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

    async def _is_cloudflare_interstitial(self, page: Page) -> bool:
        """True when the page is a Cloudflare full-page bot-check interstitial
        (managed challenge), as opposed to a standalone Turnstile widget on a
        real application form. Detected by title / challenge containers / the
        'security verification' body text / a challenges.cloudflare.com script
        on an otherwise near-empty page."""
        try:
            title = (await page.title() or "").lower()
        except Exception:
            title = ""
        if "just a moment" in title or "attention required" in title or "access denied" in title:
            return True
        try:
            return bool(await page.evaluate(r'''() => {
                if (document.querySelector('#challenge-running, #challenge-stage, #cf-challenge-running, #trk_jschal_js, #cf-please-wait')) return true;
                const t = ((document.body && document.body.innerText) || '').toLowerCase();
                if (t.includes('performing security verification')
                    || t.includes('verify you are human')
                    || t.includes('checking your browser')
                    || t.includes('needs to review the security of your connection')) return true;
                // A Cloudflare Turnstile challenge script on an otherwise
                // content-less page is an interstitial, not a form widget.
                const cf = !!document.querySelector('script[src*="challenges.cloudflare.com"]');
                const bodyLen = (document.body && document.body.innerText || '').length;
                return cf && bodyLen < 800;
            }'''))
        except Exception:
            return False

    # Injected BEFORE the challenge renders: hooks window.turnstile.render so we
    # capture the Cloudflare-specific params (sitekey/action/cData/chlPageData)
    # and the success callback. Anti-Captcha needs cData+chlPageData to solve a
    # Cloudflare CHALLENGE-PAGE Turnstile (a bare sitekey → "site key invalid"),
    # and the token must be handed to the widget's own callback so Cloudflare
    # issues cf_clearance and navigates.
    _CF_TURNSTILE_HOOK_JS = r"""() => {
        if (window.__cfHookInstalled) return;
        window.__cfHookInstalled = true;
        window.__cfParams = null;
        window.__cfCallback = null;
        const wrap = (ts) => {
            try {
                if (!ts || ts.__hooked) return ts;
                ts.__hooked = true;
                const orig = ts.render;
                ts.render = function(container, opts) {
                    try {
                        window.__cfParams = {
                            sitekey: (opts && opts.sitekey) || '',
                            action: (opts && opts.action) || '',
                            cData: (opts && opts.cData) || '',
                            chlPageData: (opts && (opts.chlPageData || opts.pagedata)) || '',
                        };
                        window.__cfCallback = (opts && opts.callback) || null;
                    } catch (e) {}
                    return orig.apply(this, arguments);
                };
            } catch (e) {}
            return ts;
        };
        // defineProperty SETTER TRAP: catch the exact moment Cloudflare's
        // api.js assigns window.turnstile (a poll races the onload callback and
        // loses). Wrap whatever is already there too.
        let _ts = window.turnstile;
        if (_ts) wrap(_ts);
        try {
            Object.defineProperty(window, 'turnstile', {
                configurable: true,
                get() { return _ts; },
                set(v) { _ts = wrap(v); },
            });
        } catch (e) {}
        // Belt-and-suspenders poll for cache-loaded cases.
        let n = 0;
        const iv = setInterval(() => { if (window.turnstile) wrap(window.turnstile); if (++n > 160) clearInterval(iv); }, 40);
    }"""

    async def solve_cloudflare_challenge(self, page: Page) -> CaptchaSolution:
        """Solve a Cloudflare MANAGED-CHALLENGE interstitial ("Just a moment…").

        A CF challenge page renders a Turnstile widget whose params (cData /
        chlPageData / action) are REQUIRED by Anti-Captcha's TurnstileTask — a
        bare sitekey yields "site key is invalid" (the failure we saw live). We:
          1. install a turnstile.render hook, reload so it fires, and capture
             {sitekey, action, cData, chlPageData} + the widget's callback;
          2. solve via TurnstileTaskProxyless, and if that is rejected, retry as
             the PROXIED TurnstileTask through the SAME residential IP the
             browser uses (get_proxy_for_context) with a matching userAgent;
          3. hand the returned token to the widget's callback (and the hidden
             cf-turnstile-response input) so Cloudflare completes and navigates.

        Returns success only when the page actually leaves the challenge.
        """
        start = time.monotonic()

        # GUARD: this solver RELOADS the page repeatedly to trigger the Turnstile
        # render hook. That only belongs on a genuine Cloudflare managed-challenge
        # INTERSTITIAL. On an application form merely FRONTED by Cloudflare — whose
        # real gate is reCAPTCHA/hCaptcha, and which was misclassified as
        # "turnstile" because a challenges.cloudflare.com script is present — those
        # reloads capture no widget ("no widget rendered") AND can navigate/close
        # the page mid-apply (the observed "Target page has been closed" ERROR that
        # killed CareerPlug + Lever applies). Bail cleanly BEFORE reloading so the
        # page survives and the run resolves to a clean terminal instead of a crash.
        try:
            if not await self._is_cloudflare_interstitial(page):
                logger.info(
                    "[CAPTCHA] solve_cloudflare_challenge: not a Cloudflare interstitial "
                    "— skipping the reload-based Turnstile solve (real captcha, if any, "
                    "is reCAPTCHA/hCaptcha; avoids the page-reload crash)."
                )
                return CaptchaSolution(
                    captcha_type="turnstile", success=False,
                    error="CAPTCHA_UNSUPPORTED: not a Cloudflare interstitial (no Turnstile challenge present)",
                    solve_time_seconds=time.monotonic() - start, cost_usd=0)
        except Exception as exc:
            logger.debug(f"[CAPTCHA] interstitial pre-check skipped: {exc}")

        ac_key = self._resolve_anticaptcha_key()
        if not ac_key:
            return CaptchaSolution(
                captcha_type="turnstile", success=False,
                error="CAPTCHA_UNSUPPORTED: Cloudflare challenge needs an Anti-Captcha key (ANTI_CAPTCHA_API_KEY)",
                solve_time_seconds=0, cost_usd=0)

        # Resolve the browser's egress proxy + UA once.
        try:
            user_agent = await page.evaluate("() => navigator.userAgent")
        except Exception:
            user_agent = ""
        proxy_config = None
        try:
            from ..browser.context_manager import get_proxy_for_context
            proxy_config = get_proxy_for_context(page.context)
        except Exception:
            proxy_config = None

        # Install the render hook; it re-arms on every navigation (document-start).
        try:
            await page.context.add_init_script(self._CF_TURNSTILE_HOOK_JS)
        except Exception as exc:
            logger.debug(f"[CAPTCHA] could not add turnstile hook (non-fatal): {exc}")
        challenge_url = page.url

        async def _capture_params():
            """Reload + capture FRESH CF Turnstile params. They expire in seconds
            (a stale cData/chlPageData → 'could not load widget'), so every solve
            attempt re-captures. The widget renders in a cross-origin
            challenges.cloudflare.com IFRAME, so scan ALL frames (Playwright's
            init script runs in each) and return the capturing frame too."""
            try:
                await page.reload(wait_until="domcontentloaded", timeout=45_000)
            except Exception:
                pass
            for _ in range(18):
                await asyncio.sleep(1.0)
                for fr in page.frames:
                    try:
                        p = await fr.evaluate("() => window.__cfParams")
                    except Exception:
                        p = None
                    if p and p.get("sitekey"):
                        return p, fr
            return None, None

        def _build_tasks(params: dict) -> list:
            base = {"websiteURL": challenge_url, "websiteKey": params["sitekey"]}
            if params.get("action"):
                base["action"] = params["action"]
            if params.get("cData"):
                base["cData"] = params["cData"]
            if params.get("chlPageData"):
                base["chlPageData"] = params["chlPageData"]
            tasks = [{**base, "type": "TurnstileTaskProxyless"}]
            # Proxied variant: anti-captcha requires an IP (not hostname) and a
            # <1s proxy. IPRoyal residential latency trips AC's 1s gate, so it is
            # OFF by default (it would just waste ~50s failing). Enable
            # CF_SOLVE_PROXIED_TASK=true only with a fast (datacenter/premium)
            # proxy. The sticky-session token still pins the exit IP.
            _use_proxied = os.getenv("CF_SOLVE_PROXIED_TASK", "false").strip().lower() in ("1", "true", "yes", "on")
            if _use_proxied and proxy_config and proxy_config.get("server"):
                from urllib.parse import urlparse as _up
                import socket as _sock
                pp = _up(proxy_config["server"])
                sch = (pp.scheme or "http").lower()
                try:
                    pip = _sock.gethostbyname(pp.hostname or "")
                except Exception:
                    pip = pp.hostname or ""
                pt = dict(base)
                pt["type"] = "TurnstileTask"
                pt["proxyType"] = "socks5" if sch.startswith("socks") else "http"
                pt["proxyAddress"] = pip
                pt["proxyPort"] = pp.port or (443 if sch == "https" else 80)
                if user_agent:
                    pt["userAgent"] = user_agent
                if proxy_config.get("username"):
                    pt["proxyLogin"] = proxy_config["username"]
                if proxy_config.get("password"):
                    pt["proxyPassword"] = proxy_config["password"]
                tasks.append(pt)
            return tasks

        # A CF challenge's params are ephemeral, so a transient anti-captcha
        # "could not load widget / try again" is retried with a FRESH capture.
        _TRANSIENT = ("could not load", "try again", "unable to load", "please try")
        try:
            max_rounds = int(os.getenv("CF_SOLVE_ROUNDS", "3"))
        except ValueError:
            max_rounds = 3
        token = ""
        params_frame = None
        last_err = ""
        for rnd in range(1, max_rounds + 1):
            params, params_frame = await _capture_params()
            if not params or not params.get("sitekey"):
                dom_key = await self._extract_turnstile_sitekey(page)
                if not dom_key:
                    last_err = "could not capture Turnstile params (no widget rendered)"
                    logger.warning(f"[CAPTCHA] CF round {rnd}/{max_rounds}: {last_err}")
                    continue
                params = {"sitekey": dom_key, "action": "", "cData": "", "chlPageData": ""}
            logger.info(
                f"[CAPTCHA] CF round {rnd}/{max_rounds}: sitekey={params['sitekey']!r} "
                f"cData={'y' if params.get('cData') else 'n'} chlPageData={'y' if params.get('chlPageData') else 'n'}"
            )
            round_transient = False
            for task in _build_tasks(params):
                try:
                    async with httpx.AsyncClient(timeout=60) as client:
                        tid = await self._anticaptcha_submit(client, task, client_key=ac_key)
                        sol = await self._anticaptcha_poll_solution(client, tid, polls=24, client_key=ac_key)
                    token = sol.get("token") or sol.get("gRecaptchaResponse") or ""
                    if token:
                        logger.info(f"[CAPTCHA] CF Turnstile token obtained via {task['type']}")
                        break
                except Exception as exc:
                    last_err = str(exc)
                    is_t = any(m in last_err.lower() for m in _TRANSIENT)
                    round_transient = round_transient or is_t
                    logger.warning(
                        f"[CAPTCHA] {task['type']} failed ({'transient' if is_t else 'hard'}): {last_err[:160]}"
                    )
                    continue
            if token:
                break
            if not round_transient:
                break  # hard failure (bad sitekey, unsupported) — re-capture won't help

        if not token:
            return CaptchaSolution(
                captcha_type="turnstile", success=False,
                error=f"CAPTCHA_UNSUPPORTED: Cloudflare Turnstile unsolved ({last_err[:120]})",
                solve_time_seconds=time.monotonic() - start, cost_usd=0)

        # Hand the token to the widget callback (this is what makes Cloudflare
        # accept it, set cf_clearance, and navigate) + fill the hidden input.
        # Invoke in the SAME frame that rendered the widget, then every frame.
        inject_js = (
            "(tok) => { try { if (window.__cfCallback) window.__cfCallback(tok); } catch(e){} "
            "try { document.querySelectorAll('[name=\"cf-turnstile-response\"]').forEach(el=>el.value=tok); } catch(e){} }"
        )
        _inject_frames = [params_frame] if params_frame else []
        _inject_frames += [f for f in page.frames if f is not params_frame]
        for fr in _inject_frames:
            try:
                await fr.evaluate(inject_js, token)
            except Exception:
                continue

        # Wait for Cloudflare to clear the interstitial and navigate to the site.
        cleared = False
        for _ in range(20):
            await asyncio.sleep(1.0)
            try:
                title = (await page.title()) or ""
            except Exception:
                title = ""
            if title and "just a moment" not in title.lower():
                cleared = True
                break
        if not cleared:
            # Last resort: a reload sometimes finalizes the clearance cookie.
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(3.0)
                title = (await page.title()) or ""
                cleared = "just a moment" not in title.lower()
            except Exception:
                pass

        if cleared:
            logger.info(f"[CAPTCHA] Cloudflare challenge CLEARED in {time.monotonic() - start:.1f}s")
        else:
            logger.warning("[CAPTCHA] Cloudflare token applied but interstitial did not clear")
        return CaptchaSolution(
            captcha_type="turnstile", token=token, success=cleared,
            error=None if cleared else "cloudflare challenge token applied but page did not clear",
            solve_time_seconds=time.monotonic() - start, cost_usd=0.003)

    async def _apply_cf_cookies(self, page: Page, cookies: Any) -> bool:
        """Set anti-captcha-returned Cloudflare cookies (cf_clearance etc.) on the
        live context, scoped to the current host. Accepts either a {name: value}
        dict or a list of cookie objects. Returns True if any cookie was set."""
        if not cookies:
            return False
        from urllib.parse import urlparse as _up
        host = (_up(page.url).hostname or "").lower()
        if not host:
            return False
        url = f"https://{host}/"
        to_add = []
        try:
            if isinstance(cookies, dict):
                for name, value in cookies.items():
                    if name and value is not None:
                        to_add.append({"name": str(name), "value": str(value), "url": url})
            elif isinstance(cookies, list):
                for c in cookies:
                    if isinstance(c, dict) and c.get("name"):
                        entry = {"name": str(c["name"]), "value": str(c.get("value", "")), "url": url}
                        to_add.append(entry)
            if not to_add:
                return False
            await page.context.add_cookies(to_add)
            logger.info(f"[CAPTCHA] applied {len(to_add)} Cloudflare cookie(s) to {host}")
            return True
        except Exception as exc:
            logger.warning(f"[CAPTCHA] could not apply Cloudflare cookies: {exc}")
            return False

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

        # Prefer the funded universal provider (Anti-Captcha ImageToTextTask)
        # for OCR when it is the configured provider — so the paid key covers
        # image captchas too, not just token captchas. Ocilar remains the
        # fallback below.
        anti_key = self._resolve_anticaptcha_key()
        if self.provider == "anticaptcha" and anti_key:
            try:
                async with httpx.AsyncClient(timeout=40) as client:
                    tid = await self._anticaptcha_submit(
                        client, {"type": "ImageToTextTask", "body": image_b64},
                        client_key=anti_key)
                    text = await self._anticaptcha_poll(client, tid, client_key=anti_key)
                if text:
                    logger.info("[CAPTCHA] image solved via Anti-Captcha ImageToTextTask")
                    return CaptchaSolution(captcha_type=captcha_type, token=text, success=True,  # type: ignore[arg-type]
                                           solve_time_seconds=time.monotonic() - start, cost_usd=0.001)
                logger.info("[CAPTCHA] Anti-Captcha image OCR empty; falling back to Ocilar")
            except Exception as exc:
                logger.warning(f"[CAPTCHA] Anti-Captcha image OCR failed: {exc}; falling back to Ocilar")

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

    async def _extract_v3_sitekey(self, page: Page) -> Optional[str]:
        """Pull the reCAPTCHA v3 site key from the anchor iframe (?k=…) or a
        data-sitekey attribute so Anti-Captcha can mint a scored token."""
        anchor = await self._extract_v3_anchor_url(page)
        if anchor:
            import urllib.parse
            k = urllib.parse.parse_qs(urllib.parse.urlparse(anchor).query).get("k", [None])[0]
            if k:
                return k
        try:
            el = await page.query_selector("[data-sitekey]")
            if el:
                key = await el.get_attribute("data-sitekey")
                if key:
                    return key
        except Exception:
            pass
        return None

    async def _solve_recaptcha_v3_anticaptcha(self, page: Page) -> Optional[str]:
        """Mint a scored reCAPTCHA v3 token via Anti-Captcha RecaptchaV3TaskProxyless."""
        site_key = await self._extract_v3_sitekey(page)
        if not site_key:
            return None
        try:
            min_score = float(os.getenv("RECAPTCHA_V3_MIN_SCORE", "0.7"))
        except (TypeError, ValueError):
            min_score = 0.7
        anti_key = self._resolve_anticaptcha_key()
        task: dict = {
            "type": "RecaptchaV3TaskProxyless",
            "websiteURL": page.url,
            "websiteKey": site_key,
            "minScore": min_score,
        }
        page_action = os.getenv("RECAPTCHA_V3_ACTION", "").strip()
        if page_action:
            task["pageAction"] = page_action
        async with httpx.AsyncClient(timeout=30) as client:
            tid = await self._anticaptcha_submit(client, task, client_key=anti_key or self.api_key)
            return await self._anticaptcha_poll(client, tid, polls=20,
                                                client_key=anti_key or self.api_key)

    async def solve_recaptcha_v3(self, page: Page) -> CaptchaSolution:
        """Solve reCAPTCHA v3, preferring the funded Anti-Captcha key.

        Order: (1) Anti-Captcha RecaptchaV3TaskProxyless (mints a scored token
        we inject), (2) the free freecaptcha library, (3) a last-resort
        auto-bypass — legitimate for v3 specifically, which is invisible and
        score-based (there is no client-side "passed" signal to verify; the
        page's own grecaptcha.execute() may still pass on a stealth session)."""
        start = time.monotonic()

        # 1) Preferred — mint a real scored token via Anti-Captcha.
        if self.provider == "anticaptcha" and (self._resolve_anticaptcha_key() or self.api_key):
            try:
                token = await self._solve_recaptcha_v3_anticaptcha(page)
                if token:
                    logger.info("[CAPTCHA] reCAPTCHA v3 token minted via Anti-Captcha")
                    await self._inject_recaptcha_v3_token(page, token)
                    return CaptchaSolution(
                        captcha_type="recaptcha_v3", token=token, success=True,
                        solve_time_seconds=time.monotonic() - start, cost_usd=0.002)
                logger.info("[CAPTCHA] Anti-Captcha v3 returned no token; trying freecaptcha")
            except Exception as exc:
                logger.warning(f"[CAPTCHA] Anti-Captcha v3 solve failed: {exc}; trying freecaptcha")

        anchor_url = await self._extract_v3_anchor_url(page)

        if not anchor_url:
            logger.info("[CAPTCHA] reCAPTCHA v3 anchor URL not found on page; trusting auto-bypass")
            return CaptchaSolution(
                captcha_type="recaptcha_v3", token="auto_bypassed",
                success=True, solve_time_seconds=time.monotonic() - start, cost_usd=0
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
            # CRITICAL ORDERING: a Cloudflare full-page MANAGED CHALLENGE
            # ("Just a moment…") often DOES render a Turnstile widget with an
            # extractable sitekey — but the proxyless TurnstileTaskProxyless
            # CANNOT solve an interstitial-embedded widget (anti-captcha returns
            # "site key is invalid"). So detect the interstitial FIRST and route
            # to the proxy-based AntiCloudflareTask (cf_clearance cookie) even
            # when a sitekey is present. Only a standalone widget on a real form
            # (not an interstitial) takes the token path. This is the
            # himalayas.app / bot-walled-host path the proxy + Anti-Captcha are FOR.
            if await self._is_cloudflare_interstitial(page):
                logger.info("[CAPTCHA] Cloudflare managed-challenge interstitial detected → AntiCloudflareTask (proxy)")
                return await self.solve_cloudflare_challenge(page)
            sitekey = await self._extract_turnstile_sitekey(page)
            if sitekey:
                # Standalone Turnstile widget on a form → token solve.
                action, cdata = await self._extract_turnstile_action(page)
                return await self.solve_turnstile(page, sitekey, action=action, cdata=cdata)
            # No sitekey and not obviously an interstitial — last resort: still
            # try the Cloudflare challenge solver (it no-ops cleanly if there's
            # no proxy / nothing to solve).
            logger.info("[CAPTCHA] Turnstile has no sitekey → attempting Cloudflare managed-challenge solve")
            return await self.solve_cloudflare_challenge(page)

        elif captcha_type == "image":
            return await self.solve_image_captcha(page, captcha_type)

        logger.warning(f"[CAPTCHA] Unsupported captcha type: {captcha_type}")
        return CaptchaSolution(captcha_type=captcha_type, success=False,  # type: ignore[arg-type]
                               error=f"CAPTCHA_UNSUPPORTED: unknown captcha type {captcha_type!r}",
                               solve_time_seconds=0, cost_usd=0)

    async def solve(self, page: Page, captcha_type: str, max_attempts: int = 3) -> CaptchaSolution:
        """Solve a captcha with up to *max_attempts* retries.

        Layered strategy (Anti-Captcha-primary):
          1. PRIORITY: the funded Anti-Captcha key runs FIRST for every token
             type it supports (reCAPTCHA v2/v3, hCaptcha, Turnstile) and for
             image OCR — this is the operator's universal primary solver.
          2. Free in-process AI vision solver + Whisper audio (v2) — fallback
             when the paid provider is unavailable or exhausted.
          3. Ocilar OCR as the final image-captcha fallback.

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

        # Tracks whether the anticaptcha priority phase already ran the paid
        # provider — if it did, the standard paid loop below MUST NOT run it
        # again (that was a bug: up to 6 paid attempts instead of 3, doubling
        # both wall-clock and cost).
        priority_ran = False
        priority_last: Optional[CaptchaSolution] = None

        # ── Priority Phase: anticaptcha first ──────────────────────────────────
        # If the provider is anticaptcha and it has a valid API key + balance, run
        # it first as priority instead of fallback.
        if self.provider == "anticaptcha" and self.api_key and paid_ok:
            logger.info("[CAPTCHA] anticaptcha key detected and configured as priority. Solving via anticaptcha first.")
            priority_ran = True
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

            priority_last = last
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

        # Preserve any failed-but-informative solution from the priority phase.
        last: Optional[CaptchaSolution] = priority_last

        # Skip the paid retry loop when either (a) the provider auth/balance gate
        # reported the configured provider dead/zero-balance for a token type, or
        # (b) the anticaptcha priority phase already exhausted the paid attempts —
        # re-running the same provider would only burn duplicate timed-out
        # attempts. ("image" is exempt from (a) because it routes to Ocilar.)
        _gate_skip = (not paid_ok) and (captcha_type in _PAID_TOKEN_TYPES)
        _skip_paid = _gate_skip or priority_ran
        if _gate_skip:
            logger.error(
                f"[CAPTCHA] Skipping {max_attempts} paid attempts for type={captcha_type}: "
                f"{self.provider} unavailable ({paid_reason})."
            )
        elif priority_ran:
            logger.info(
                "[CAPTCHA] Skipping standard paid loop — anticaptcha priority "
                "phase already exhausted the paid attempts for this captcha."
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

        # Paid loop was skipped because the configured provider's key is
        # dead/zero-balance and no free solver succeeded — surface a
        # distinguishable clean-fail so the executor maps it to a terminal
        # BLOCKED rather than a generic error. (Only the auth/balance gate skip
        # is "unsupported"; a priority-phase exhaustion is a normal failure.)
        if _gate_skip and not (last and last.success):
            return CaptchaSolution(captcha_type=captcha_type, success=False,  # type: ignore[arg-type]
                                   error=f"CAPTCHA_UNSUPPORTED: {paid_reason}",
                                   solve_time_seconds=0, cost_usd=0)

        logger.error(f"[CAPTCHA] All {max_attempts} attempts failed for type={captcha_type}, provider={self.provider}")
        return last or CaptchaSolution(captcha_type=captcha_type, success=False,  # type: ignore[arg-type]
                                       solve_time_seconds=0, cost_usd=0)
