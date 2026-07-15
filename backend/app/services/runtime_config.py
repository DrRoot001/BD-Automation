"""Runtime-editable settings, shared across every worker via Redis.

The admin panel writes overrides here; code reads them via :func:`get`. When no
override is set (or Redis is unreachable) we fall back to the ``.env`` value and
then a literal default — so behaviour is unchanged until the operator changes
something, and a Redis blip can never take the pipeline down.

Because all worker laptops share one Redis, a change made in the admin panel
propagates to every process within ``_CACHE_TTL`` seconds — no restart, no
redeploy. Only keys declared in :data:`SPEC` are editable (a whitelist; unknown
keys are rejected on write).

All functions are SYNC so they're callable from both async request handlers and
sync code paths (e.g. the LLM client's key resolution).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List

import redis  # sync client (the `redis` package ships both sync + redis.asyncio)

logger = logging.getLogger(__name__)

_HASH = "runtime_settings"
_CACHE_TTL = 5.0  # seconds — a change is visible on every worker within this window

# The editable whitelist. `cast` coerces the stored string; `env` is the .env
# fallback var; `default` is the last-resort literal. `type` drives the UI widget.
SPEC: Dict[str, Dict[str, Any]] = {
    "apply_score_threshold": {
        "type": "number",
        "label": "Apply score threshold",
        "help": "Jobs scoring at or above this (0–100) are tailored + auto-applied.",
        "min": 0, "max": 100,
        "cast": float, "env": "APPLY_SCORE_THRESHOLD", "default": 75,
    },
    "max_daily_applications_per_candidate": {
        "type": "number",
        "label": "Daily applications per candidate",
        "help": "Max auto-applications per candidate per day.",
        "min": 0,
        "cast": int, "env": "MAX_DAILY_APPLICATIONS_PER_CANDIDATE", "default": 50,
    },
    "llm_primary_provider": {
        "type": "select",
        "label": "Primary AI provider",
        "help": "Which LLM leads the apply/tailoring pipeline; the other is the fallback.",
        "options": ["openai", "gemini"],
        # Gemini-primary by default: it's the project's designated primary LLM
        # (see CLAUDE.md — "Gemini is the default for all AI tasks") and OpenAI's
        # 30k-TPM rate limit crawls the vision agent loop. Flip via the admin
        # panel (Redis override) when OpenAI credits are preferred.
        "cast": str, "env": None, "default": "gemini",
    },
}

_client: "redis.Redis | None" = None
_cache: Dict[str, str] = {}
_cache_at = 0.0


def _redis() -> "redis.Redis":
    global _client
    if _client is None:
        _client = redis.from_url(
            os.getenv("REDIS_URL", ""),
            decode_responses=True,
            socket_timeout=3,
            socket_connect_timeout=3,
        )
    return _client


def _raw() -> Dict[str, str]:
    """The override hash from Redis (cached ``_CACHE_TTL`` s); ``{}`` if Redis is down."""
    global _cache, _cache_at
    now = time.monotonic()
    if now - _cache_at < _CACHE_TTL and _cache_at > 0:
        return _cache
    try:
        _cache = _redis().hgetall(_HASH) or {}
    except Exception as exc:  # noqa: BLE001 - never let settings break a run
        logger.debug(f"[runtime_config] redis read failed, using .env defaults: {exc}")
        _cache = {}
    _cache_at = now
    return _cache


def _fallback(key: str) -> Any:
    spec = SPEC[key]
    env = spec.get("env")
    raw: Any = os.getenv(env) if env else None
    if raw in (None, ""):
        raw = spec["default"]
    try:
        return spec["cast"](raw)
    except Exception:  # noqa: BLE001
        return spec["default"]


def get(key: str) -> Any:
    """Effective value: the Redis override if set & valid, else .env, else default."""
    if key not in SPEC:
        raise KeyError(f"unknown runtime setting {key!r}")
    raw = _raw().get(key)
    if raw not in (None, ""):
        try:
            return SPEC[key]["cast"](raw)
        except Exception:  # noqa: BLE001 - a corrupt stored value falls back
            pass
    return _fallback(key)


def set_many(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + persist overrides. Rejects unknown keys / out-of-range / bad values.

    Returns the full :func:`describe` map so the caller can echo the new state.
    """
    clean: Dict[str, str] = {}
    for k, v in updates.items():
        if k not in SPEC:
            raise KeyError(f"unknown runtime setting {k!r}")
        spec = SPEC[k]
        try:
            val = spec["cast"](v)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{k}: invalid value {v!r}") from exc
        if spec["type"] == "select" and str(val) not in spec["options"]:
            raise ValueError(f"{k} must be one of {spec['options']}")
        if "min" in spec and val < spec["min"]:
            raise ValueError(f"{k} must be >= {spec['min']}")
        if "max" in spec and val > spec["max"]:
            raise ValueError(f"{k} must be <= {spec['max']}")
        clean[k] = str(val)
    if clean:
        _redis().hset(_HASH, mapping=clean)  # raises if Redis is truly down (caller 500s)
        global _cache_at
        _cache_at = 0.0  # invalidate local cache so the change is visible immediately
        logger.info(f"[runtime_config] updated {list(clean.keys())}")
    return describe()


def describe() -> Dict[str, Any]:
    """Each editable key's UI metadata + current effective value + source."""
    raw = _raw()
    out: Dict[str, Any] = {}
    for key, spec in SPEC.items():
        out[key] = {
            "value": get(key),
            "type": spec["type"],
            "label": spec["label"],
            "help": spec.get("help", ""),
            "source": "override" if raw.get(key) not in (None, "") else "default",
        }
        for opt in ("options", "min", "max"):
            if opt in spec:
                out[key][opt] = spec[opt]
    return out


def llm_key_priority() -> List[str]:
    """Env-var-name priority list derived from the ``llm_primary_provider`` knob.

    openai-primary → [OPENAI, GEMINI, …deeper fallbacks]; gemini-primary flips the
    first two. The deeper fallbacks (Anthropic/Groq/OpenRouter) are preserved for
    resilience. Consumed by the LLM client's key resolution.
    """
    deeper = ["ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_2", "GROQ_API_KEY", "OPENROUTER_API_KEY"]
    if get("llm_primary_provider") == "gemini":
        return ["GEMINI_API_KEY", "OPENAI_API_KEY", *deeper]
    return ["OPENAI_API_KEY", "GEMINI_API_KEY", *deeper]
