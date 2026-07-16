"""Multi-turn conversation history for the AgentLoop.

Gives the LLM memory of what it has done across steps, eliminating the
"stateless re-reasoning" problem where the model retries failed selectors,
re-discovers already-filled fields, and can't learn from its own mistakes
within a single run.

Each turn records what action was taken, whether it succeeded, and what
changed on the page — formatted as a compact prompt block that fits into
the existing user-turn template without blowing up the token budget.

Usage::

    history = ConversationHistory(max_turns=12)
    history.record(TurnRecord(
        step=3, action_kind="fill_field", selector="#email",
        field_label="Email", value_summary="jane@example.com",
        result="ok", failure_reason=None,
        dom_changed=True, page_url_changed=False,
        timestamp=time.time(),
    ))
    prompt_block = history.format_for_prompt()

Design:
  * Bounded: keeps at most ``max_turns`` entries; oldest are evicted.
  * Compact: ``format_for_prompt`` produces ~2-4 lines per turn, so
    12 turns ≈ 30-50 lines — well within LLM context limits.
  * Security: ``value_summary`` is truncated and NEVER contains passwords
    (callers must sanitise before recording).
  * Immutable once recorded — turns are append-only.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional


@dataclass(frozen=True)
class TurnRecord:
    """One step in the agent's run."""

    step: int
    action_kind: str
    selector: Optional[str] = None
    field_label: Optional[str] = None
    # Truncated preview of the value (NEVER a password).
    value_summary: Optional[str] = None
    result: Literal["ok", "failed", "skipped"] = "ok"
    failure_reason: Optional[str] = None
    dom_changed: bool = False
    page_url_changed: bool = False
    timestamp: float = 0.0
    # Extra context the loop may attach (e.g. "captcha solved", "page scrolled")
    note: Optional[str] = None


class ConversationHistory:
    """Rolling window of the agent's recent actions + outcomes.

    The LLM prompt includes the formatted output of this object so it
    can reason about what has already been tried, what worked, and what
    failed — rather than re-discovering the form state from scratch on
    every turn.
    """

    def __init__(self, max_turns: int = 12):
        self._turns: List[TurnRecord] = []
        self._max = max(1, max_turns)
        # Track which selectors have failed across the whole run (not just
        # the rolling window) so the prompt can warn "never retry these".
        self._globally_failed_selectors: Dict[str, str] = {}  # selector → reason
        # Track which fields have been successfully filled so the LLM knows
        # "do NOT refill" even after the turn scrolls out of the window.
        self._filled_fields: Dict[str, str] = {}  # label → value_summary

    # ── Recording ─────────────────────────────────────────────────────────

    def record(self, turn: TurnRecord) -> None:
        """Append a turn, evicting the oldest if at capacity."""
        self._turns.append(turn)
        if len(self._turns) > self._max:
            self._turns = self._turns[-self._max :]

        # Track globally-failed selectors.
        if turn.result == "failed" and turn.selector:
            reason = turn.failure_reason or "element not found"
            self._globally_failed_selectors[turn.selector] = reason

        # Track successfully filled fields.
        if (
            turn.result == "ok"
            and turn.action_kind in ("fill_field", "upload_file")
            and turn.field_label
        ):
            self._filled_fields[turn.field_label] = (
                turn.value_summary or "(set)"
            )

    # ── Querying ──────────────────────────────────────────────────────────

    @property
    def turns(self) -> List[TurnRecord]:
        return list(self._turns)

    @property
    def filled_fields(self) -> Dict[str, str]:
        """All fields successfully filled during this run (label → value)."""
        return dict(self._filled_fields)

    @property
    def failed_selectors(self) -> Dict[str, str]:
        """Selectors that have failed at any point (selector → reason)."""
        return dict(self._globally_failed_selectors)

    def last_n(self, n: int = 5) -> List[TurnRecord]:
        return self._turns[-n:]

    def consecutive_failures(self) -> int:
        """Count of consecutive failed actions at the tail."""
        count = 0
        for t in reversed(self._turns):
            if t.result == "failed":
                count += 1
            else:
                break
        return count

    def consecutive_same_action(self) -> int:
        """Count of consecutive identical (kind+selector) actions at the tail."""
        if not self._turns:
            return 0
        last = self._turns[-1]
        count = 0
        for t in reversed(self._turns):
            if t.action_kind == last.action_kind and t.selector == last.selector:
                count += 1
            else:
                break
        return count

    def has_filled(self, field_label: str) -> bool:
        """True if this field was already successfully filled."""
        if not field_label:
            return False
        fl = field_label.strip().lower()
        return any(k.strip().lower() == fl for k in self._filled_fields)

    def selector_has_failed(self, selector: str) -> bool:
        """True if this selector has failed at any point in the run."""
        return selector in self._globally_failed_selectors

    # ── Prompt formatting ─────────────────────────────────────────────────

    def format_for_prompt(self, max_lines: int = 60) -> str:
        """Render the conversation history as a compact block for the LLM.

        Returns an empty string if no turns have been recorded yet.
        """
        if not self._turns:
            return ""

        lines: List[str] = []

        # Section 1: Globally dead selectors (across entire run, not just window).
        dead = self._globally_failed_selectors
        if dead:
            lines.append("⛔ DEAD SELECTORS (tried and FAILED — do NOT retry):")
            for sel, reason in list(dead.items())[:8]:
                lines.append(f"   {sel} — {reason}")
            lines.append("")

        # Section 2: Recent action history (the rolling window).
        lines.append("CONVERSATION HISTORY (most recent actions):")
        for t in self._turns:
            icon = "✅" if t.result == "ok" else "❌" if t.result == "failed" else "⏭"
            parts = [f"  Step {t.step}: {icon} {t.action_kind}"]

            if t.field_label:
                parts.append(f'"{t.field_label}"')
            elif t.selector:
                parts.append(f"on {t.selector}")

            if t.value_summary and t.action_kind in (
                "fill_field",
                "upload_file",
                "click",
                "click_apply",
            ):
                # Truncate for prompt space; never show full long values.
                val = t.value_summary[:50]
                if len(t.value_summary) > 50:
                    val += "…"
                parts.append(f"= {val!r}")

            if t.result == "failed" and t.failure_reason:
                parts.append(f"REASON: {t.failure_reason[:80]}")

            if t.dom_changed:
                parts.append("[page changed]")
            if t.page_url_changed:
                parts.append("[URL changed]")
            if t.note:
                parts.append(f"({t.note[:40]})")

            line = " ".join(parts)
            lines.append(line)

            if len(lines) >= max_lines:
                lines.append("  ... (earlier turns omitted)")
                break

        return "\n".join(lines)

    def format_filled_summary(self) -> str:
        """Compact list of all fields successfully filled (for FILLED block)."""
        if not self._filled_fields:
            return "(none yet)"
        items = []
        for label, val in self._filled_fields.items():
            v = val[:30] + "…" if len(val) > 30 else val
            items.append(f"  ✓ {label} = {v}")
        return "\n".join(items)

    # ── Helpers ───────────────────────────────────────────────────────────

    def clear(self) -> None:
        """Reset all state. Used when the loop restarts on a new form."""
        self._turns.clear()
        self._globally_failed_selectors.clear()
        self._filled_fields.clear()

    def __len__(self) -> int:
        return len(self._turns)

    def __repr__(self) -> str:
        return (
            f"ConversationHistory(turns={len(self._turns)}, "
            f"filled={len(self._filled_fields)}, "
            f"dead_selectors={len(self._globally_failed_selectors)})"
        )
