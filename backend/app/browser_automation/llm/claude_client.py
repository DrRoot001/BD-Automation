"""Anthropic Claude client for the autonomous browser agent.

Mirrors the small surface area the rest of the codebase consumed from the
previous Gemini client::

    client.generate_json(prompt, image_bytes=None, temperature=0.1, timeout_s=...)
    client.generate_text(prompt, image_bytes=None, temperature=0.2, timeout_s=...)

Both methods are async, both transparently support a single inline screenshot
(``image_bytes``) and raise :class:`LLMUnavailable` on any non-recoverable
error so callers fall back to the deterministic path.

Configuration via environment::

    ANTHROPIC_API_KEY          # preferred; falls back to GEMINI_API_KEY for
                               # backwards-compat with the operator's .env
    CLAUDE_MODEL=claude-haiku-4-5-20251001     # default; cheap + fast
    CLAUDE_VISION_MODEL=claude-haiku-4-5-20251001
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import asyncio
import os
import re
import weakref
from typing import Any, List, Optional

import httpx

from . import telemetry

logger = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    """Raised when the LLM cannot be used (no key, SDK missing, repeated bad JSON)."""


# Defaults are the native Anthropic model IDs. When we route through OpenRouter
# the model id is translated to its OpenRouter form (`anthropic/<id>`) on the fly.
_DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
_DEFAULT_VISION_MODEL = os.getenv("CLAUDE_VISION_MODEL", "claude-haiku-4-5-20251001")
# Output-token caps. Kept well under typical OpenRouter free-tier headroom
# (~3,000 tokens). Vision calls return short JSON; field-fill JSON is bounded
# by field count. If you see 402 errors, lower these further or top up credit.
# Output-token caps. Form-fill JSON for ~25 fields needs ~2000 tokens.
# Old default (600) truncated Gemini's JSON mid-response on multi-field forms.
# Gemini's free tier gives generous quota — raising the cap is safe.
_MAX_TOKENS_TEXT = int(os.getenv("CLAUDE_MAX_TOKENS_TEXT", "2400"))
# Lever 4 (cost optimization): AgentLoop vision turns return a single-action
# JSON object. Bare JSON is ~70 tokens, BUT reasoning-capable models (Gemini
# 2.5 Flash, Opus 4.x) spend hidden "thinking" tokens that COUNT against
# max_output_tokens. The old cap of 200 was being burned entirely on
# reasoning, leaving the JSON truncated mid-string (`{"kind":"fill_field"`).
# 800 gives reasoning headroom without meaningful cost — only generated
# tokens are billed, not the cap.
_MAX_TOKENS_VISION = int(os.getenv("CLAUDE_MAX_TOKENS_VISION", "800"))
_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "2400"))  # back-compat
_OPENROUTER_BASE = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# Native Gemini (Google AI Studio) — used when the operator drops in an AIza* key.
# Default model is gemini-3.5-flash (multimodal — handles the AgentLoop's
# screenshot+DOM vision turns and text turns). Overridable via GEMINI_MODEL /
# GEMINI_VISION_MODEL without a code change.
_GEMINI_BASE = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
_GEMINI_TEXT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
_GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-3.5-flash")


def _resolve_api_keys() -> list[str]:
    """Return all configured API keys, deduplicated, non-empty, in priority order.

    Multiple Groq keys (GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3) are each
    honored. Groq's free tier rate-limits PER ACCOUNT (30k TPM for Llama 4
    Scout), so keys from different Groq accounts give independent windows —
    when one key hits its TPM cap mid-form, the chain rolls to the next and
    keeps the agent moving instead of stalling on a 429.
    """
    # Priority order. Anthropic first (best model when funded). Gemini is placed
    # AHEAD of Groq/OpenRouter because it's a capable vision model with a
    # generous free tier (high RPM/TPM) — when Anthropic has no credits it
    # becomes the reliable primary, whereas Groq's free tier (~12k TPM) gets
    # exhausted mid-run and 429s, and OpenRouter's free tier caps prompt tokens.
    # An explicit LLM_KEY_PRIORITY env (comma-separated env-var names) overrides.
    default_order = [
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_2",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY_2",
        "CLAUDE_API_KEY",
        "GROQ_API_KEY",
        "GROQ_API_KEY_2",
        "GROQ_API_KEY_3",
        "OPENROUTER_API_KEY",
    ]
    order = [n.strip() for n in os.getenv("LLM_KEY_PRIORITY", "").split(",") if n.strip()] or default_order
    candidates = [os.getenv(name, "") for name in order]
    seen: set[str] = set()
    result = []
    for k in candidates:
        k = k.strip()
        if k and k not in seen:
            seen.add(k)
            result.append(k)
    return result


def _resolve_api_key() -> str:
    keys = _resolve_api_keys()
    return keys[0] if keys else ""


def _detect_provider(key: str) -> str:
    """Pick a provider based on the API-key prefix.

    - ``sk-ant-*`` → native Anthropic
    - ``sk-or-*``  → OpenRouter (routes to any model via OpenAI-compat API)
    - ``gsk_*``    → Groq (OpenAI-compatible chat-completions API)
    - everything else (``AIza*``, ``AQ.Ab*``, etc.) → native Gemini
    """
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("sk-or-"):
        return "openrouter"
    if key.startswith("gsk_"):
        return "groq"
    return "gemini"


class ClaudeClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        self.model_name = model or _DEFAULT_MODEL

        if api_key:
            self._keys = [api_key.strip()]
        else:
            self._keys = _resolve_api_keys()

        if not self._keys:
            logger.warning(
                "[Claude] No API key found (ANTHROPIC_API_KEY / ANTHROPIC_API_KEY_2 / "
                "OPENROUTER_API_KEY / GEMINI_API_KEY)"
            )
            self.api_key = ""
            self.provider = "anthropic"
            self._anthropic = None
            return

        self.api_key = self._keys[0]
        self.provider = provider or _detect_provider(self.api_key)
        self._anthropic = self._make_anthropic_client(self.api_key)
        # Show the model that will ACTUALLY be used for this provider — logging
        # the Anthropic default while running on Gemini was misleading.
        _disp_model = _GEMINI_TEXT_MODEL if self.provider == "gemini" else self.model_name
        logger.info(
            f"[LLM] provider={self.provider} model={_disp_model} "
            f"key_prefix={self.api_key[:7]!r} fallback_keys={len(self._keys) - 1}"
        )

    def _make_anthropic_client(self, key: str):
        if _detect_provider(key) != "anthropic":
            return None
        try:
            from anthropic import AsyncAnthropic
            return AsyncAnthropic(api_key=key)
        except Exception as exc:
            logger.error(f"[Claude] Anthropic SDK import failed: {exc}")
            return None

    def effective_provider(self) -> str:
        """The provider that will actually serve the NEXT call — i.e. the
        first key still in rotation. Dead keys (exhausted Anthropic, bad
        keys) get removed from `self._keys` as they fail, so this reflects
        reality, not the original primary. Callers use it to size payloads:
        when the live provider is Groq we ship smaller screenshots / resume
        blocks to fit the 30k TPM cap.
        """
        for k in self._keys:
            return _detect_provider(k)
        return self.provider

    def _ensure(self) -> None:
        if not self.api_key:
            raise LLMUnavailable("LLM API key missing")
        if self.provider == "anthropic" and self._anthropic is None:
            raise LLMUnavailable("Anthropic SDK not available")
        # OpenRouter and Gemini use pure httpx — no extra SDK setup needed.

    @staticmethod
    def _strip_json_fences(text: str) -> str:
        """Strip ```json fences. Tolerates missing closing fence (truncated
        responses still get the JSON body extracted instead of failing parse).
        """
        s = text.strip()
        # Full ```...``` block
        m = re.search(r"```(?:json)?\s*(.*?)```", s, flags=re.DOTALL)
        if m:
            return m.group(1).strip()
        # Opening fence with no closing fence (truncated)
        m = re.match(r"```(?:json)?\s*(.*)$", s, flags=re.DOTALL)
        if m:
            return m.group(1).strip()
        return s

    # ──────────────────────────────────────────────────────────────────────
    # Anthropic-native content shape
    # ──────────────────────────────────────────────────────────────────────
    def _anthropic_content(self, prompt: str, image_bytes: Optional[bytes]) -> List[dict]:
        if not image_bytes:
            return [{"type": "text", "text": prompt}]
        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg" if image_bytes[:3] == b"\xff\xd8\xff" else "image/png",
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                },
            },
            {"type": "text", "text": prompt},
        ]

    # ──────────────────────────────────────────────────────────────────────
    # OpenRouter content shape (OpenAI chat-completions format)
    # ──────────────────────────────────────────────────────────────────────
    @staticmethod
    def _openrouter_model(model: str) -> str:
        # OpenRouter expects vendor-prefixed model ids. Translate native Anthropic
        # ids to their OpenRouter form unless the operator already namespaced it.
        if "/" in model:
            return model
        # claude-haiku-4-5-20251001 → anthropic/claude-haiku-4.5
        m = re.match(r"claude-([a-z]+)-(\d+)-(\d+)", model)
        if m:
            family, major, minor = m.groups()
            return f"anthropic/claude-{family}-{major}.{minor}"
        return f"anthropic/{model}"

    def _openrouter_content(self, prompt: str, image_bytes: Optional[bytes]) -> list:
        if not image_bytes:
            return prompt
        return [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {
                    "url": (
                        "data:image/jpeg;base64,"
                        if image_bytes[:3] == b"\xff\xd8\xff"
                        else "data:image/png;base64,"
                    )
                    + base64.b64encode(image_bytes).decode("ascii")
                },
            },
        ]

    async def _call_anthropic(
        self,
        prompt: str,
        image_bytes: Optional[bytes],
        temperature: float,
        timeout_s: float,
        system: Optional[str],
    ) -> str:
        model = _DEFAULT_VISION_MODEL if image_bytes else self.model_name
        kwargs: dict = {
            "model": model,
            "max_tokens": _MAX_TOKENS_VISION if image_bytes else _MAX_TOKENS_TEXT,
            "temperature": temperature,
            "messages": [
                {"role": "user", "content": self._anthropic_content(prompt, image_bytes)}
            ],
        }
        if system:
            # Cache the system prompt on Anthropic. AgentLoop builds it once at
            # session start and re-uses it for every turn — cache hits charge
            # 0.1x normal input cost, saving ~90% on the 900+ token identity card.
            if len(system) >= 1024:
                kwargs["system"] = [{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }]
            else:
                kwargs["system"] = system
        try:
            resp = await asyncio.wait_for(
                self._anthropic.messages.create(**kwargs), timeout=timeout_s
            )
        except asyncio.TimeoutError as exc:
            raise LLMUnavailable(f"Claude call timed out after {timeout_s}s") from exc
        except Exception as exc:
            raise LLMUnavailable(f"Claude call failed: {exc}") from exc
        blocks = getattr(resp, "content", None) or []
        text = "".join(getattr(b, "text", "") or "" for b in blocks).strip()
        usage = getattr(resp, "usage", None)
        # Anthropic returns cache_read_input_tokens / cache_creation_input_tokens
        # when prompt caching is active. We add those to the input total so the
        # telemetry reflects the *effective* tokens billed.
        in_tok = (getattr(usage, "input_tokens", 0) or 0)
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
        if not in_tok:
            in_tok = telemetry.estimate_from_text(prompt)
        telemetry.record(
            provider="anthropic", model=model,
            input_tokens=in_tok + cache_create + cache_read,
            output_tokens=getattr(usage, "output_tokens", 0) or telemetry.estimate_from_text(text),
            has_image=bool(image_bytes),
        )
        if cache_read or cache_create:
            logger.info(
                f"[Tokens] anthropic cache: read={cache_read} create={cache_create} fresh={in_tok}"
            )
        return text

    async def _call_openrouter(
        self,
        prompt: str,
        image_bytes: Optional[bytes],
        temperature: float,
        timeout_s: float,
        system: Optional[str],
    ) -> str:
        model = self._openrouter_model(
            _DEFAULT_VISION_MODEL if image_bytes else self.model_name
        )
        messages: list = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({
            "role": "user",
            "content": self._openrouter_content(prompt, image_bytes),
        })
        payload = {
            "model": model,
            "max_tokens": _MAX_TOKENS_VISION if image_bytes else _MAX_TOKENS_TEXT,
            "temperature": temperature,
            "messages": messages,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # Optional but recommended by OpenRouter for analytics / leaderboard
            "HTTP-Referer": os.getenv("OPENROUTER_REFERER", "https://bd-automator.local"),
            "X-Title": os.getenv("OPENROUTER_TITLE", "BD-Automator-Agent"),
        }
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                r = await client.post(
                    f"{_OPENROUTER_BASE}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            if r.status_code != 200:
                raise LLMUnavailable(
                    f"OpenRouter HTTP {r.status_code}: {r.text[:400]}"
                )
            body = r.json()
        except LLMUnavailable:
            raise
        except Exception as exc:
            raise LLMUnavailable(f"OpenRouter call failed: {exc}") from exc

        try:
            choices = body.get("choices") or []
            msg = (choices[0] if choices else {}).get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                text = "".join(
                    (c or {}).get("text", "") for c in content if isinstance(c, dict)
                ).strip()
            else:
                text = str(content or "").strip()
            usage = body.get("usage") or {}
            telemetry.record(
                provider="openrouter", model=model,
                input_tokens=int(usage.get("prompt_tokens") or telemetry.estimate_from_text(prompt)),
                output_tokens=int(usage.get("completion_tokens") or telemetry.estimate_from_text(text)),
                has_image=bool(image_bytes),
            )
            return text
        except Exception as exc:
            raise LLMUnavailable(f"OpenRouter response parse failed: {exc}") from exc

    async def _call_groq(
        self,
        prompt: str,
        image_bytes: Optional[bytes],
        temperature: float,
        timeout_s: float,
        system: Optional[str],
    ) -> str:
        """Groq chat-completions API. OpenAI-compatible; only the base URL and
        model names differ. Vision turns route to Llama 4 vision; text turns to
        Llama 3.3-70B for higher reasoning quality.

        Token-budget note: Groq free-tier TPM caps are 30k for Llama 4 Scout
        and 12k for Llama 3.3-70B. We cap max_tokens lower than the default
        to keep individual turns small (action JSON is ~100 tokens — no need
        to reserve 800). Combined with the system-prompt-resume trimming
        in loop.py and respecting the 429 retry-after, this lets a full
        form-fill session fit inside the free quota.
        """
        groq_base = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
        if image_bytes:
            model = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        else:
            model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        # Per-provider output cap. Defaults: 350 (vision) / 600 (text). The
        # action schema's JSON is ~100 tokens; 350 leaves headroom for the
        # rare longer answer (textarea acknowledgments) without burning a
        # full 800-token reservation against TPM.
        _max_vision = int(os.getenv("GROQ_MAX_TOKENS_VISION", "350"))
        _max_text = int(os.getenv("GROQ_MAX_TOKENS_TEXT", "600"))

        # Build content — same shape as OpenRouter / OpenAI vision.
        if image_bytes:
            user_content: Any = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode()
                    },
                },
            ]
        else:
            user_content = prompt
        messages: list = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})
        payload = {
            "model": model,
            "max_tokens": _max_vision if image_bytes else _max_text,
            "temperature": temperature,
            "messages": messages,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                r = await client.post(
                    f"{groq_base}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            if r.status_code != 200:
                raise LLMUnavailable(f"Groq HTTP {r.status_code}: {r.text[:400]}")
            body = r.json()
        except LLMUnavailable:
            raise
        except Exception as exc:
            raise LLMUnavailable(f"Groq call failed: {exc}") from exc
        try:
            choices = body.get("choices") or []
            msg = (choices[0] if choices else {}).get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                text = "".join(
                    (c or {}).get("text", "") for c in content if isinstance(c, dict)
                ).strip()
            else:
                text = str(content or "").strip()
            usage = body.get("usage") or {}
            telemetry.record(
                provider="groq", model=model,
                input_tokens=int(usage.get("prompt_tokens") or telemetry.estimate_from_text(prompt)),
                output_tokens=int(usage.get("completion_tokens") or telemetry.estimate_from_text(text)),
                has_image=bool(image_bytes),
            )
            return text
        except Exception as exc:
            raise LLMUnavailable(f"Groq response parse failed: {exc}") from exc

    async def _call_gemini(
        self,
        prompt: str,
        image_bytes: Optional[bytes],
        temperature: float,
        timeout_s: float,
        system: Optional[str],
    ) -> str:
        model = _GEMINI_VISION_MODEL if image_bytes else _GEMINI_TEXT_MODEL
        parts: list = []
        if image_bytes:
            mime = "image/jpeg" if image_bytes[:3] == b"\xff\xd8\xff" else "image/png"
            parts.append({
                "inline_data": {
                    "mime_type": mime,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }
            })
        parts.append({"text": prompt})
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": _MAX_TOKENS_VISION if image_bytes else _MAX_TOKENS_TEXT,
                "responseMimeType": "application/json" if not image_bytes else "text/plain",
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        url = f"{_GEMINI_BASE}/models/{model}:generateContent?key={self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                r = await client.post(url, json=payload)
            if r.status_code != 200:
                raise LLMUnavailable(f"Gemini HTTP {r.status_code}: {r.text[:400]}")
            body = r.json()
        except LLMUnavailable:
            raise
        except Exception as exc:
            raise LLMUnavailable(f"Gemini call failed: {exc}") from exc
        try:
            cands = body.get("candidates") or []
            if not cands:
                raise LLMUnavailable(f"Gemini returned no candidates: {body}")
            content = cands[0].get("content") or {}
            parts_out = content.get("parts") or []
            text = "".join(p.get("text", "") for p in parts_out if isinstance(p, dict)).strip()
            usage = body.get("usageMetadata") or {}
            telemetry.record(
                provider="gemini", model=model,
                input_tokens=int(usage.get("promptTokenCount") or telemetry.estimate_from_text(prompt)),
                output_tokens=int(usage.get("candidatesTokenCount") or telemetry.estimate_from_text(text)),
                has_image=bool(image_bytes),
            )
            return text
        except LLMUnavailable:
            raise
        except Exception as exc:
            raise LLMUnavailable(f"Gemini response parse failed: {exc}") from exc

    async def _call(
        self,
        prompt: str,
        image_bytes: Optional[bytes],
        temperature: float,
        timeout_s: float,
        system: Optional[str] = None,
    ) -> str:
        self._ensure()
        last_exc: Exception = LLMUnavailable("No keys to try")
        # Track the shortest rate-limit retry-after we saw across all keys.
        # We only sleep on it AFTER trying every other key — if a different
        # key/provider can serve right now, rolling to it beats sleeping.
        pending_retry_after: Optional[float] = None
        keys_snapshot = list(self._keys)
        for _idx, key in enumerate(keys_snapshot):
            provider = _detect_provider(key)
            try:
                if provider == "openrouter":
                    # Temporarily swap key for this call
                    orig_key, self.api_key = self.api_key, key
                    try:
                        return await self._call_openrouter(prompt, image_bytes, temperature, timeout_s, system)
                    finally:
                        self.api_key = orig_key
                elif provider == "groq":
                    orig_key, self.api_key = self.api_key, key
                    try:
                        return await self._call_groq(prompt, image_bytes, temperature, timeout_s, system)
                    finally:
                        self.api_key = orig_key
                elif provider == "gemini":
                    orig_key, self.api_key = self.api_key, key
                    try:
                        return await self._call_gemini(prompt, image_bytes, temperature, timeout_s, system)
                    finally:
                        self.api_key = orig_key
                else:
                    anthropic_client = self._anthropic if key == self._keys[0] else self._make_anthropic_client(key)
                    if anthropic_client is None:
                        continue
                    orig_client, self._anthropic = self._anthropic, anthropic_client
                    try:
                        return await self._call_anthropic(prompt, image_bytes, temperature, timeout_s, system)
                    finally:
                        self._anthropic = orig_client
            except LLMUnavailable as exc:
                msg = str(exc)
                # Permanently drop keys whose failure is NOT transient:
                #  * 402 Payment Required
                #  * "credit balance is too low" (Anthropic, returned as 400)
                #  * 401 / invalid api key
                # Without this, an exhausted Anthropic key gets retried on
                # EVERY step (5 dead keys × 20 steps = 100 wasted calls), which
                # also delays reaching the one working provider (Groq) and eats
                # into its TPM window via wall-clock churn.
                _permanent = (
                    "402" in msg
                    or "Payment Required" in msg
                    or "credit balance is too low" in msg
                    or "invalid_api_key" in msg
                    or "401" in msg
                )
                if _permanent:
                    logger.warning(
                        f"[Claude] key prefix={key[:7]!r} permanently unavailable "
                        f"({msg[:60]!r}). Removing from session rotation."
                    )
                    if key in self._keys:
                        self._keys.remove(key)
                # Token-per-minute rate-limit: capture the retry-after hint but
                # DON'T sleep yet. With multiple keys (e.g. two Groq accounts),
                # rolling to the next key serves the call immediately instead of
                # waiting out one key's TPM window. We only sleep at the end if
                # every key was rate-limited and none could serve.
                if "429" in msg or "rate_limit" in msg.lower():
                    import re as _re_rl
                    m = _re_rl.search(r"try again in (\d+(?:\.\d+)?)s", msg, _re_rl.I)
                    if m:
                        wait = min(float(m.group(1)), 12.0)
                        if pending_retry_after is None or wait < pending_retry_after:
                            pending_retry_after = wait
                logger.warning(f"[Claude] key prefix={key[:7]!r} failed: {exc} — trying next key")
                last_exc = exc
        # Every key failed this pass. If at least one was a transient rate
        # limit, sleep the shortest retry-after so the CALLER's retry loop
        # has a fresh window to land in. (Caller re-invokes _call on its own.)
        if pending_retry_after:
            logger.info(
                f"[Claude] all keys exhausted this pass; sleeping "
                f"{pending_retry_after:.1f}s per shortest retry-after hint "
                "before returning to caller"
            )
            await asyncio.sleep(pending_retry_after)
        raise last_exc

    async def generate_json(
        self,
        prompt: str,
        image_bytes: Optional[bytes] = None,
        temperature: float = 0.1,
        timeout_s: float = 25.0,
        system: Optional[str] = None,
    ) -> Any:
        if system is None:
            system = (
                "You are a strict-JSON API. Respond with exactly one JSON object "
                "matching the schema implied by the user prompt. Do not include "
                "prose, markdown fences, or explanations."
            )
        text = await self._call(prompt, image_bytes, temperature, timeout_s, system=system)
        cleaned = self._strip_json_fences(text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # The model (especially reasoning-heavy ones like Opus) sometimes
            # prefixes the action with prose ("I need to analyze... {json}").
            # Rather than fail the whole turn, extract the LAST balanced
            # top-level {...} object from the text and parse that. The action
            # JSON is almost always the final object the model emits.
            extracted = self._extract_json_object(text)
            if extracted is not None:
                try:
                    obj = json.loads(extracted)
                    logger.info(
                        "[Claude] recovered JSON object from prose-wrapped response"
                    )
                    return obj
                except json.JSONDecodeError:
                    pass
            logger.warning(f"[Claude] JSON parse failed; raw head={text[:200]!r}")
            raise LLMUnavailable(
                f"Claude returned non-JSON: {text[:80]!r}"
            )

    @staticmethod
    def _extract_json_object(text: str) -> Optional[str]:
        """Find the last balanced {...} object in free text.

        Scans for brace pairs respecting string literals/escapes so a model
        that 'thinks out loud' before emitting its action JSON still yields a
        parseable object. Returns the substring, or None if no balanced
        object is found.
        """
        if not text:
            return None
        candidates = []
        depth = 0
        start = -1
        in_str = False
        esc = False
        for i, ch in enumerate(text):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        candidates.append(text[start:i + 1])
                        start = -1
        # Prefer the last balanced object (the action usually comes last).
        return candidates[-1] if candidates else None

    async def generate_text(
        self,
        prompt: str,
        image_bytes: Optional[bytes] = None,
        temperature: float = 0.2,
        timeout_s: float = 30.0,
    ) -> str:
        return await self._call(prompt, image_bytes, temperature, timeout_s)


_singleton: Optional[ClaudeClient] = None
# One client PER EVENT LOOP. Each concurrent Celery browser task runs in its own
# thread with its own asyncio loop (asyncio.run), so keying by the running loop
# gives every concurrent AgentLoop its OWN ClaudeClient. This eliminates the
# cross-task race where _call() temporarily mutates shared self.api_key /
# self._anthropic during key rotation (task A could send a request with task B's
# key mid-flight). Within a single task the client is reused across steps, so
# per-run rotation state (dropped/exhausted keys) is preserved. Keyed weakly by
# the loop object so entries evict automatically when the loop is GC'd — no leak.
_loop_clients: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def get_llm() -> ClaudeClient:
    """Return the ClaudeClient bound to the current event loop (task-isolated).

    Falls back to a process-global singleton only when called with no running
    loop (pure-sync context), where there is no concurrency to race."""
    global _singleton
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        client = _loop_clients.get(loop)
        if client is None:
            client = ClaudeClient()
            _loop_clients[loop] = client
        return client
    if _singleton is None:
        _singleton = ClaudeClient()
    return _singleton


# Backwards-compat alias so existing imports continue to work even though the
# provider has flipped to Anthropic. Anything still importing ``get_gemini``
# transparently receives a Claude-backed client with the same interface.
def get_gemini() -> ClaudeClient:  # pragma: no cover — alias
    return get_llm()


GeminiClient = ClaudeClient  # type alias for old type hints
