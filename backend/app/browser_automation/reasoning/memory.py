"""Memory inputs fed to the reasoning engine.

Two kinds, both rendered into the perception prompt:

* :class:`BrowserMemory` — cross-run knowledge about the CURRENT host/portal
  (what flow worked before, which selectors clicked, whether it gated on login,
  captcha history). This is the shape the existing ``agent.portal_memory`` /
  ``agent.learned_fixes`` playbooks can be projected into — the engine only
  needs the rendered facts, not the storage layer.

* :class:`ConversationMemory` — the running record of THIS session: each turn's
  action, the reasoning behind it, and (once known) its outcome. It supplies the
  "previous action" + "previous reasoning" the model needs to answer
  "what changed?" and "did the previous action succeed?".

Neither carries logic — they are data + a compact ``render()`` for the prompt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class BrowserMemory:
    """What we already know about this host from prior runs."""
    host: str = ""
    known_flow: List[str] = field(default_factory=list)          # ordered action kinds that worked
    working_selectors: Dict[str, List[str]] = field(default_factory=dict)  # channel -> selectors
    success_signal: str = ""                                     # confirmation text/url that meant success
    last_outcome: Optional[str] = None                          # e.g. "SUBMITTED", "STUCK"
    captcha_history: List[str] = field(default_factory=list)     # captcha kinds seen here
    login_required: Optional[bool] = None
    notes: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any([
            self.known_flow, self.working_selectors, self.success_signal,
            self.last_outcome, self.captcha_history, self.notes,
            self.login_required is not None,
        ])

    def render(self) -> str:
        if self.is_empty:
            return "(no prior knowledge of this portal — first visit)"
        lines: List[str] = []
        if self.host:
            lines.append(f"host: {self.host}")
        if self.last_outcome:
            lines.append(f"last outcome here: {self.last_outcome}")
        if self.login_required is not None:
            lines.append(f"login required: {'yes' if self.login_required else 'no'}")
        if self.known_flow:
            lines.append("flow that worked before: " + " → ".join(self.known_flow[:24]))
        if self.working_selectors:
            for ch, sels in list(self.working_selectors.items())[:8]:
                if sels:
                    lines.append(f"selectors that worked for {ch}: " + ", ".join(sels[:5]))
        if self.captcha_history:
            lines.append("captcha seen here before: " + ", ".join(sorted(set(self.captcha_history))))
        if self.success_signal:
            lines.append(f"success looked like: {self.success_signal[:160]}")
        for n in self.notes[:6]:
            lines.append(f"note: {n}")
        return "\n".join(lines)


@dataclass
class ConversationTurn:
    step: int
    action_type: str
    action_summary: str = ""       # short human description of what we did
    reasoning_summary: str = ""    # the 'why' from that turn
    page_type: str = ""            # what the page looked like that turn
    outcome: str = ""              # filled in once the NEXT observation is known

    def render(self) -> str:
        parts = [f"#{self.step} {self.action_type}"]
        if self.action_summary:
            parts.append(self.action_summary)
        if self.page_type:
            parts.append(f"(page was {self.page_type})")
        line = " ".join(parts)
        if self.reasoning_summary:
            line += f"\n     reason: {self.reasoning_summary[:200]}"
        if self.outcome:
            line += f"\n     outcome: {self.outcome[:160]}"
        return line


@dataclass
class ConversationMemory:
    """The running history of the current session."""
    objective: str = ""
    turns: List[ConversationTurn] = field(default_factory=list)

    def add(self, turn: ConversationTurn) -> None:
        self.turns.append(turn)

    @property
    def last(self) -> Optional[ConversationTurn]:
        return self.turns[-1] if self.turns else None

    def set_last_outcome(self, outcome: str) -> None:
        """Attach the observed result to the most recent turn (called once the
        next observation reveals what the last action produced)."""
        if self.turns:
            self.turns[-1].outcome = outcome

    def render(self, last_n: int = 8) -> str:
        if not self.turns:
            return "(no actions taken yet — this is the first turn)"
        recent = self.turns[-last_n:]
        return "\n".join(t.render() for t in recent)
