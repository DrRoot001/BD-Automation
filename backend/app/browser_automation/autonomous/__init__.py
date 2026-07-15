"""Perception-driven autonomous agent for Module 4.

Wires the perception layer (:mod:`..perception`) and the reasoning engine
(:mod:`..reasoning`) into a strict observe → reason → act → observe loop that
replaces assumption-based scripts. The Generic adapter is built on this; other
adapters can adopt it the same way.

Public surface:
    AutonomousAgent      — the observe/reason/act driver
    AgentRunResult       — status + observed conditions + turn history
    AgentStatus          — terminal outcomes (SUBMITTED, OTP_REQUIRED, …)
    DetectedConditions   — what was observed (evidence-backed flags)
    ActionExecutor       — executes one reasoned action via Playwright
    detect_conditions    — deterministic observation over a BrowserState
"""
from .action_executor import ActionExecutor
from .agent import AutonomousAgent
from .conditions import detect_conditions, merge_reasoning_signals
from .models import ActionResult, AgentRunResult, AgentStatus, DetectedConditions
from .recovery import RecoveryController

__all__ = [
    "AutonomousAgent",
    "AgentRunResult",
    "AgentStatus",
    "ActionResult",
    "DetectedConditions",
    "ActionExecutor",
    "RecoveryController",
    "detect_conditions",
    "merge_reasoning_signals",
]
