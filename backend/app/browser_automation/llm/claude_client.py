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
import os
import re
from typing import Any, List, Optional

import httpx

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
_MAX_TOKENS_VISION = int(os.getenv("CLAUDE_MAX_TOKENS_VISION", "600"))
_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "2400"))  # back-compat
_OPENROUTER_BASE = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# Native Gemini (Google AI Studio) — used when the operator drops in an AIza* key.
# Free tier on gemini-2.5-flash gives generous quota; no separate top-up needed.
_GEMINI_BASE = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
_GEMINI_TEXT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
_GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash")


def _resolve_api_key() -> str:
    return (
        os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("CLAUDE_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or ""
    ).strip()


def _detect_provider(key: str) -> str:
    """Pick a provider based on the API-key prefix.

    - ``sk-ant-*`` → native Anthropic
    - ``sk-or-*``  → OpenRouter (routes to any model via OpenAI-compat API)
    - everything else (``AIza*``, ``AQ.Ab*``, etc.) → native Gemini

    Google AI Studio has shipped multiple key formats (``AIza...`` classic,
    ``AQ.Ab...`` newer). Rather than chase prefixes, we treat any non-Anthropic /
    non-OpenRouter key as Gemini — matches operator intent when they drop a
    Google key into ``GEMINI_API_KEY``.
    """
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("sk-or-"):
        return "openrouter"
    return "gemini"


class ClaudeClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        self.api_key = (api_key or _resolve_api_key()).strip()
        self.model_name = model or _DEFAULT_MODEL
        self.provider = provider or _detect_provider(self.api_key)
        self._anthropic = None  # SDK client for native Anthropic
        if not self.api_key:
            logger.warning(
                "[Claude] No API key found (ANTHROPIC_API_KEY / OPENROUTER_API_KEY / "
                "GEMINI_API_KEY)"
            )
            return
        logger.info(
            f"[Claude] provider={self.provider} model={self.model_name} "
            f"key_prefix={self.api_key[:7]!r}"
        )
        if self.provider == "anthropic":
            try:
                from anthropic import AsyncAnthropic
                self._anthropic = AsyncAnthropic(api_key=self.api_key)
            except Exception as exc:
                logger.error(f"[Claude] Anthropic SDK import failed: {exc}")
                self._anthropic = None

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
        return "".join(getattr(b, "text", "") or "" for b in blocks).strip()

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
                return "".join(
                    (c or {}).get("text", "") for c in content if isinstance(c, dict)
                ).strip()
            return str(content or "").strip()
        except Exception as exc:
            raise LLMUnavailable(f"OpenRouter response parse failed: {exc}") from exc

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
            return "".join(p.get("text", "") for p in parts_out if isinstance(p, dict)).strip()
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
        if self.provider == "openrouter":
            return await self._call_openrouter(prompt, image_bytes, temperature, timeout_s, system)
        if self.provider == "gemini":
            return await self._call_gemini(prompt, image_bytes, temperature, timeout_s, system)
        return await self._call_anthropic(prompt, image_bytes, temperature, timeout_s, system)

    async def generate_json(
        self,
        prompt: str,
        image_bytes: Optional[bytes] = None,
        temperature: float = 0.1,
        timeout_s: float = 25.0,
    ) -> Any:
        system = (
            "You are a strict-JSON API. Respond with exactly one JSON object "
            "matching the schema implied by the user prompt. Do not include "
            "prose, markdown fences, or explanations."
        )
        text = await self._call(prompt, image_bytes, temperature, timeout_s, system=system)
        cleaned = self._strip_json_fences(text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.warning(f"[Claude] JSON parse failed; raw head={text[:200]!r}")
            raise LLMUnavailable(f"Claude returned non-JSON: {exc}") from exc

    async def generate_text(
        self,
        prompt: str,
        image_bytes: Optional[bytes] = None,
        temperature: float = 0.2,
        timeout_s: float = 30.0,
    ) -> str:
        return await self._call(prompt, image_bytes, temperature, timeout_s)


_singleton: Optional[ClaudeClient] = None


def get_llm() -> ClaudeClient:
    global _singleton
    if _singleton is None:
        _singleton = ClaudeClient()
    return _singleton


# Backwards-compat alias so existing imports continue to work even though the
# provider has flipped to Anthropic. Anything still importing ``get_gemini``
# transparently receives a Claude-backed client with the same interface.
def get_gemini() -> ClaudeClient:  # pragma: no cover — alias
    return get_llm()


GeminiClient = ClaudeClient  # type alias for old type hints
