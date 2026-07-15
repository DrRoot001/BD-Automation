"""Evidence-grounded reasoning / decision layer for Module 4.

Consumes a :class:`~..perception.BrowserState` and produces a structured,
evidence-justified :class:`ReasoningOutput` (what page / what changed / did the
last action succeed / error? / OTP? / email verification? / captcha? / next
action / why). Replaces assumption-based rules with observed-evidence reasoning.

Public surface:
    DecisionEngine          — build prompt → LLM → parse → ground → record
    ReasoningOutput         — the structured reasoning schema (typed)
    BrowserMemory           — cross-run per-host knowledge (prompt input)
    ConversationMemory      — this session's action/reasoning history (prompt input)
    SYSTEM_PROMPT           — the perception prompt (system)
    build_user_turn         — per-turn user message builder
    render_browser_state    — BrowserState → evidence text
"""
from .engine import DecisionEngine
from .memory import BrowserMemory, ConversationMemory, ConversationTurn
from .models import (
    ActionType,
    NextAction,
    Observation,
    PageType,
    PreviousActionAssessment,
    ReasoningOutput,
    SignalClaim,
    Signals,
)
from .prompt import SYSTEM_PROMPT, build_user_turn, render_browser_state

__all__ = [
    # engine
    "DecisionEngine",
    # schema
    "ReasoningOutput",
    "Observation",
    "PreviousActionAssessment",
    "Signals",
    "SignalClaim",
    "NextAction",
    "ActionType",
    "PageType",
    # memory
    "BrowserMemory",
    "ConversationMemory",
    "ConversationTurn",
    # prompt
    "SYSTEM_PROMPT",
    "build_user_turn",
    "render_browser_state",
]
