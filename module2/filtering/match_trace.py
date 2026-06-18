"""Capture match scoring breakdown for transparency."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class MatchTrace:
    score: float = 0.0
    components: Dict[str, float] = field(default_factory=dict)

    def add(self, name: str, weight: float, value: float) -> None:
        self.components[name] = value * weight
        self.score += value * weight

    def to_dict(self):
        return {"score": self.score, "components": self.components}
