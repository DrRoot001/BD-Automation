"""Storage compatibility helpers for module2."""

from typing import Any


class JobStore:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    def save(self, job: Any) -> Any:
        return job


class EventPublisher:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    def publish(self, event: str, payload: Any | None = None) -> None:
        return None
