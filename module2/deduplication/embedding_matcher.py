"""Embedding-based similarity matcher for deduplication."""
from __future__ import annotations

import math
from typing import List


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def is_similar(a: List[float], b: List[float], threshold: float = 0.92) -> bool:
    return cosine(a, b) >= threshold
