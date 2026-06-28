"""Filtering helpers for module2 compatibility."""

from dataclasses import dataclass
from typing import Any


@dataclass
class FilterResult:
    passed: bool
    reason: str | None = None


class FilteringEngine:
    def apply_filters(self, job: Any, filters: Any | None = None) -> FilterResult:
        return FilterResult(passed=True)


def score_job(job: Any, filters: Any | None = None) -> float:
    return 1.0
