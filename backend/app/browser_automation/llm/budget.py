"""Per-application LLM cost budget with tiered-model rationing.

The operator's requirement: **one browser-automation application must cost at
most $0.10 of LLM spend**. The "deep" (Pro-model) reasoning tier is permitted
as an *exception* for genuinely hard decisions — but it must be paid for out of
the same $0.10, never on top of it.

This module is the meter and the governor:

* **Meter** — every provider path in ``claude_client`` already funnels its token
  usage through ``telemetry.record()``. That is the single choke point, so this
  module hooks there (see ``telemetry.record``) and converts tokens → dollars
  using a price table. No provider call site needs to know about budgets.
* **Governor** — ``allow_deep()`` decides whether a Pro-tier call is affordable
  *right now*, and ``exhausted()`` tells the loop to stop spending. The loop
  degrades (deep → fast → stop) instead of overrunning.

Isolation
---------
The browser worker runs ``--pool=threads --concurrency=2``, so **two
applications run concurrently inside one process**. A module-global counter
would bill one application's tokens to the other. The ledger is therefore held
in a :class:`contextvars.ContextVar`: each Celery task thread starts with a
fresh empty context, and the value propagates into any ``asyncio`` tasks that
thread spawns. Thread-safe and async-safe with no locking on the hot path.

Pricing
-------
Prices are **$ per 1M tokens** and are ``LLM_PRICE_TABLE``-overridable (JSON).

    LLM_PRICE_TABLE='{"gemini/my-model": {"input": 0.1, "output": 0.4}}'

.. warning::
   The built-in defaults are a *starting point*, not gospel — provider pricing
   changes and is not verifiable from inside this process. Confirm the rates
   for the models you actually run against the provider's pricing page and pin
   them via ``LLM_PRICE_TABLE``. An unknown model is deliberately billed at the
   most expensive known rate (fail-safe: we would rather downgrade the tier
   early than silently blow the cap).

Usage::

    from ..llm import budget

    budget.start_run(application_id="abc-123")      # at the top of a run
    ...
    if budget.allow_deep(estimated_usd=0.02):       # gate a Pro-tier call
        tier = "deep"
    ...
    budget.log_summary()                            # at the end of a run
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

#: Hard ceiling on LLM spend for ONE application. The operator's target.
DEFAULT_BUDGET_USD = float(os.getenv("LLM_BUDGET_USD_PER_APPLICATION", "0.10"))

#: Fraction of the budget held back so the run can always afford enough cheap
#: (fast-tier) turns to finish filling and submit. Deep-tier calls may only be
#: paid for out of the *unreserved* remainder — that is what makes Pro an
#: exception rather than the default.
DEEP_RESERVE_FRACTION = float(os.getenv("LLM_BUDGET_DEEP_RESERVE_FRACTION", "0.40"))

#: When True, exceeding the cap only warns (useful for measuring a new portal
#: before committing to a number). When False (default) the governor degrades.
BUDGET_SOFT_MODE = os.getenv("LLM_BUDGET_SOFT_MODE", "false").lower() == "true"

#: $ per 1M tokens, keyed "<provider>/<model>". VERIFY against provider pricing
#: and override via LLM_PRICE_TABLE — see the module warning above.
_DEFAULT_PRICES: Dict[str, Dict[str, float]] = {
    # Gemini — flash-class (the production default, cheap tier)
    "gemini/gemini-3.5-flash": {"input": 0.30, "output": 2.50},
    "gemini/gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini/gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini/gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    # Gemini — pro-class (the expensive "deep" tier)
    "gemini/gemini-3-pro-preview": {"input": 2.00, "output": 12.00},
    "gemini/gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "gemini/gemini-1.5-pro": {"input": 1.25, "output": 5.00},
    # Anthropic (fallback provider)
    "anthropic/claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "anthropic/claude-sonnet-5": {"input": 3.00, "output": 15.00},
    "anthropic/claude-opus-4-8": {"input": 5.00, "output": 25.00},
    # Groq / OpenAI (opportunistic fallbacks)
    "groq/llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
    "openai/gpt-4o-mini": {"input": 0.15, "output": 0.60},
}

#: Billed for a model with no price entry. Deliberately pessimistic — an
#: unpriced model must never look free, or the cap silently fails open.
_UNKNOWN_PRICE = {"input": 5.00, "output": 25.00}


def _load_price_table() -> Dict[str, Dict[str, float]]:
    table = dict(_DEFAULT_PRICES)
    raw = os.getenv("LLM_PRICE_TABLE")
    if raw:
        try:
            override = json.loads(raw)
            for k, v in (override or {}).items():
                if isinstance(v, dict) and "input" in v and "output" in v:
                    table[str(k)] = {
                        "input": float(v["input"]),
                        "output": float(v["output"]),
                    }
                else:
                    logger.warning(f"[Budget] ignoring malformed LLM_PRICE_TABLE entry: {k}")
        except Exception as exc:
            logger.warning(f"[Budget] LLM_PRICE_TABLE is not valid JSON, using defaults: {exc}")
    return table


_PRICES = _load_price_table()


def price_for(provider: str, model: str) -> Tuple[float, float, bool]:
    """Return ``(input_per_1m, output_per_1m, is_known)`` for a provider/model.

    Falls back to a prefix match (so ``gemini-2.5-pro-preview-06`` still prices
    as ``gemini-2.5-pro``) before giving up and billing ``_UNKNOWN_PRICE``.
    """
    key = f"{provider}/{model}"
    hit = _PRICES.get(key)
    if hit:
        return hit["input"], hit["output"], True

    # Longest-prefix match handles dated/preview model suffixes.
    best: Optional[str] = None
    for known in _PRICES:
        kp, _, kmodel = known.partition("/")
        if kp != provider or not kmodel:
            continue
        if model.startswith(kmodel) and (best is None or len(kmodel) > len(best.partition("/")[2])):
            best = known
    if best:
        p = _PRICES[best]
        return p["input"], p["output"], True

    return _UNKNOWN_PRICE["input"], _UNKNOWN_PRICE["output"], False


def cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    """Dollar cost of one call. Never raises."""
    try:
        in_rate, out_rate, _ = price_for(provider or "", model or "")
        return (
            (max(0, int(input_tokens)) / 1_000_000.0) * in_rate
            + (max(0, int(output_tokens)) / 1_000_000.0) * out_rate
        )
    except Exception:
        return 0.0


# ── The ledger ───────────────────────────────────────────────────────────────


@dataclass
class RunBudget:
    """Dollar ledger for ONE application run."""

    limit_usd: float = DEFAULT_BUDGET_USD
    application_id: Optional[str] = None
    spent_usd: float = 0.0
    calls: int = 0
    deep_calls: int = 0
    #: Spend attributed per model, for the end-of-run summary.
    by_model: Dict[str, float] = field(default_factory=dict)
    #: True once a call was priced with the unknown-model fallback — the
    #: summary flags it so the operator knows the number is an upper bound.
    saw_unknown_model: bool = False
    _warned: bool = False

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    @property
    def spendable_on_deep_usd(self) -> float:
        """Budget available for Pro-tier calls, after the fast-tier reserve."""
        reserve = self.limit_usd * DEEP_RESERVE_FRACTION
        return max(0.0, self.remaining_usd - reserve)

    def charge(self, provider: str, model: str, usd: float) -> None:
        self.spent_usd += max(0.0, usd)
        self.calls += 1
        key = f"{provider}/{model}"
        self.by_model[key] = self.by_model.get(key, 0.0) + max(0.0, usd)

    def summary(self) -> str:
        pct = (self.spent_usd / self.limit_usd * 100.0) if self.limit_usd > 0 else 0.0
        models = ", ".join(f"{k}=${v:.4f}" for k, v in sorted(self.by_model.items()))
        flag = " [UPPER BOUND — unpriced model seen]" if self.saw_unknown_model else ""
        return (
            f"spent=${self.spent_usd:.4f} / ${self.limit_usd:.2f} ({pct:.0f}%) "
            f"calls={self.calls} deep={self.deep_calls}{flag} | {models or '(none)'}"
        )


#: Per-thread / per-task ledger. See "Isolation" in the module docstring.
_current: contextvars.ContextVar[Optional[RunBudget]] = contextvars.ContextVar(
    "llm_run_budget", default=None
)


def start_run(
    application_id: Optional[str] = None,
    limit_usd: Optional[float] = None,
) -> RunBudget:
    """Begin a fresh ledger for this application. Call once per run."""
    rb = RunBudget(
        limit_usd=float(limit_usd if limit_usd is not None else DEFAULT_BUDGET_USD),
        application_id=application_id,
    )
    _current.set(rb)
    logger.info(
        f"[Budget] run started app={application_id or '?'} cap=${rb.limit_usd:.2f} "
        f"(deep reserve={DEEP_RESERVE_FRACTION:.0%}, soft={BUDGET_SOFT_MODE})"
    )
    return rb


def end_run() -> Optional[RunBudget]:
    """Clear the ledger and return it (for logging/persistence)."""
    rb = _current.get()
    _current.set(None)
    return rb


def get_run() -> Optional[RunBudget]:
    return _current.get()


def charge(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    """Bill a completed call to the active ledger. Returns the cost in USD.

    A no-op returning the computed cost when no run is active (e.g. LLM calls
    made outside an application run, or in tests). Never raises — billing must
    never be the thing that fails an application.
    """
    try:
        _, _, known = price_for(provider or "", model or "")
        usd = cost_usd(provider, model, input_tokens, output_tokens)
        rb = _current.get()
        if rb is None:
            return usd
        if not known:
            rb.saw_unknown_model = True
            logger.warning(
                f"[Budget] no price for {provider}/{model} — billing at fallback "
                f"${_UNKNOWN_PRICE['input']}/{_UNKNOWN_PRICE['output']} per 1M. "
                f"Pin it via LLM_PRICE_TABLE."
            )
        rb.charge(provider, model, usd)
        if rb.spent_usd >= rb.limit_usd and not rb._warned:
            rb._warned = True
            logger.warning(
                f"[Budget] CAP REACHED for app={rb.application_id or '?'}: {rb.summary()}"
                + (" (soft mode — not enforcing)" if BUDGET_SOFT_MODE else "")
            )
        return usd
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"[Budget] charge failed (non-fatal): {exc}")
        return 0.0


# ── The governor ─────────────────────────────────────────────────────────────


def allow_deep(estimated_usd: float = 0.02) -> bool:
    """May we spend on a Pro-tier ("deep") call right now?

    Deep is an *exception*, so it is only allowed when its estimated cost fits
    in the budget that remains **after** holding back the fast-tier reserve
    needed to finish the run. With no active ledger, allow (tests / non-run
    callers keep their existing behaviour).
    """
    rb = _current.get()
    if rb is None:
        return True
    if BUDGET_SOFT_MODE:
        return True
    return rb.spendable_on_deep_usd >= max(0.0, estimated_usd)


def note_deep_call() -> None:
    """Record that a deep-tier call was actually issued (for the summary)."""
    rb = _current.get()
    if rb is not None:
        rb.deep_calls += 1


def exhausted() -> bool:
    """True when the run has spent its entire cap and must stop calling the LLM."""
    rb = _current.get()
    if rb is None or BUDGET_SOFT_MODE:
        return False
    return rb.spent_usd >= rb.limit_usd


def remaining_usd() -> float:
    rb = _current.get()
    return rb.remaining_usd if rb is not None else float("inf")


def log_summary(prefix: str = "[Budget]") -> None:
    rb = _current.get()
    if rb is None:
        logger.info(f"{prefix} no active run ledger")
        return
    logger.info(f"{prefix} RUN SUMMARY — {rb.summary()}")
