"""Business rules and scoring configuration for matching."""
from __future__ import annotations

from typing import Dict

# Default weights (can be adjusted by config or user profile)
DEFAULT_WEIGHTS = {
    "stacks": 0.45,
    "experience": 0.25,
    "type_location": 0.18,
    "salary": 0.12,
}


def get_weights() -> Dict[str, float]:
    return DEFAULT_WEIGHTS.copy()
