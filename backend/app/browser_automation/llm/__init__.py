"""LLM provider — currently Anthropic Claude.

Public surface (used by forms.llm_filler, agent.page_agent, agent.failure_diagnoser):
    get_llm()        → ClaudeClient singleton
    LLMUnavailable   → raised on any non-recoverable LLM error

`get_gemini()` and `GeminiClient` are kept as transparent aliases so callers
that haven't been updated keep working.
"""
from .claude_client import (
    ClaudeClient,
    GeminiClient,
    LLMUnavailable,
    get_gemini,
    get_llm,
)
from . import telemetry

__all__ = [
    "ClaudeClient",
    "GeminiClient",
    "LLMUnavailable",
    "get_llm",
    "get_gemini",
    "telemetry",
]
