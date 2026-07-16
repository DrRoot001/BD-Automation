"""Per-session token telemetry for the AI-driven form-filling pipeline.

The goal is concrete, reportable numbers — "this run cost X input + Y output
tokens across N calls" — so we can measure the impact of prompt-shrink and
context-baking optimizations. The counter is process-global; reset it at the
start of each run via ``reset_session()``.

Token counts come from the provider's response when available (Anthropic
returns ``usage.input_tokens`` / ``usage.output_tokens``; OpenRouter mirrors
that field; Gemini returns ``usageMetadata.promptTokenCount`` /
``candidatesTokenCount``). When the provider doesn't report usage we fall
back to a coarse char-length estimate (≈4 chars/token).
"""
from __future__ import annotations

import contextvars
import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CallRecord:
    label: str           # e.g. "agent_loop.step", "llm_filler.batch"
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    has_image: bool
    cost_usd: float = 0.0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class SessionTotals:
    calls: List[CallRecord] = field(default_factory=list)

    def add(self, rec: CallRecord) -> None:
        self.calls.append(rec)

    @property
    def total_input(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def total_output(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    @property
    def total(self) -> int:
        return self.total_input + self.total_output

    @property
    def total_cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    @property
    def by_label(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for c in self.calls:
            out[c.label] = out.get(c.label, 0) + c.total
        return out

    def summary(self) -> str:
        n = len(self.calls)
        by_lbl = ", ".join(f"{k}={v}" for k, v in sorted(self.by_label.items()))
        return (
            f"calls={n} input={self.total_input} output={self.total_output} "
            f"total={self.total} cost=${self.total_cost_usd:.4f} "
            f"| by_label: {by_lbl or '(none)'}"
        )


# Per-run state is held in ContextVars, NOT module globals. The browser worker
# runs --pool=threads --concurrency=2, so two applications share this process:
# with a global, one apply's reset_session() would wipe the other's counters and
# its set_label() would mislabel the other's calls. Each Celery task thread gets
# a fresh context, and the value propagates into the asyncio tasks it spawns.
# Mirrors the isolation in llm/budget.py — see that module's "Isolation" note.
_lock = threading.Lock()
_session_var: contextvars.ContextVar[Optional[SessionTotals]] = contextvars.ContextVar(
    "llm_telemetry_session", default=None
)
_label_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "llm_telemetry_label", default="uncategorized"
)


def _session_for_thread() -> SessionTotals:
    s = _session_var.get()
    if s is None:
        s = SessionTotals()
        _session_var.set(s)
    return s


def reset_session() -> None:
    """Start a fresh token ledger for THIS run/thread only."""
    _session_var.set(SessionTotals())
    _label_var.set("uncategorized")


def set_label(label: str) -> None:
    """Tag all subsequent LLM calls on this run with this label until changed."""
    _label_var.set(label or "uncategorized")


def record(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    has_image: bool = False,
    label: Optional[str] = None,
) -> None:
    # Every provider path funnels through here, which makes this the one place
    # that has to know about money. Billing the per-application ledger from
    # this single choke point keeps the five _call_* methods budget-unaware.
    from . import budget as _budget

    usd = _budget.charge(provider, model, int(input_tokens or 0), int(output_tokens or 0))
    rec = CallRecord(
        label=label or _label_var.get(),
        provider=provider,
        model=model,
        input_tokens=int(input_tokens or 0),
        output_tokens=int(output_tokens or 0),
        has_image=has_image,
        cost_usd=usd,
    )
    with _lock:
        _session_for_thread().add(rec)
    logger.info(
        f"[Tokens] {rec.label} {rec.provider}/{rec.model} "
        f"in={rec.input_tokens} out={rec.output_tokens} img={rec.has_image} "
        f"cost=${usd:.5f} left=${_budget.remaining_usd():.4f}"
    )


def estimate_from_text(s: str) -> int:
    """Fallback estimate when provider doesn't return usage."""
    if not s:
        return 0
    # Anthropic / Gemini average ≈3.5–4 chars/token for English. 4 is conservative.
    return max(1, len(s) // 4)


def get_session() -> SessionTotals:
    with _lock:
        return _session_for_thread()


def log_summary(prefix: str = "[Tokens]") -> None:
    s = get_session()
    logger.info(f"{prefix} SESSION SUMMARY — {s.summary()}")
