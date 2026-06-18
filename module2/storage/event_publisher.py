"""Simple event publisher that logs events and supports subscribers (in-memory).
"""
from __future__ import annotations

from typing import Callable, Dict, List, Any


class EventPublisher:
    def __init__(self):
        self._subs: Dict[str, List[Callable[[Any], None]]] = {}

    def subscribe(self, event_name: str, handler: Callable[[Any], None]) -> None:
        self._subs.setdefault(event_name, []).append(handler)

    def publish(self, event_name: str, payload: Any) -> None:
        handlers = self._subs.get(event_name, [])
        for h in handlers:
            try:
                h(payload)
            except Exception:
                # swallow to avoid breaking pipeline
                pass

    def subscribers(self, event_name: str) -> int:
        return len(self._subs.get(event_name, []))
