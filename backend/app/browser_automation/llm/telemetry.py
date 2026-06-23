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
            f"total={self.total} | by_label: {by_lbl or '(none)'}"
        )


_lock = threading.Lock()
_session = SessionTotals()
_current_label = "uncategorized"


def reset_session() -> None:
    global _session, _current_label
    with _lock:
        _session = SessionTotals()
        _current_label = "uncategorized"


def set_label(label: str) -> None:
    """Tag all subsequent LLM calls with this label until changed."""
    global _current_label
    with _lock:
        _current_label = label or "uncategorized"


def record(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    has_image: bool = False,
    label: Optional[str] = None,
) -> None:
    rec = CallRecord(
        label=label or _current_label,
        provider=provider,
        model=model,
        input_tokens=int(input_tokens or 0),
        output_tokens=int(output_tokens or 0),
        has_image=has_image,
    )
    with _lock:
        _session.add(rec)
    logger.info(
        f"[Tokens] {rec.label} {rec.provider}/{rec.model} "
        f"in={rec.input_tokens} out={rec.output_tokens} img={rec.has_image}"
    )


def estimate_from_text(s: str) -> int:
    """Fallback estimate when provider doesn't return usage."""
    if not s:
        return 0
    # Anthropic / Gemini average ≈3.5–4 chars/token for English. 4 is conservative.
    return max(1, len(s) // 4)


def get_session() -> SessionTotals:
    with _lock:
        return _session


def log_summary(prefix: str = "[Tokens]") -> None:
    s = get_session()
    logger.info(f"{prefix} SESSION SUMMARY — {s.summary()}")
