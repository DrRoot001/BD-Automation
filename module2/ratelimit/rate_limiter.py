"""Simple token-bucket rate limiter per-key."""
from __future__ import annotations

import time
from typing import Dict


class TokenBucket:
    def __init__(self, capacity: int, refill_rate_per_sec: float):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate_per_sec
        self.last = time.time()

    def consume(self, tokens: int = 1) -> bool:
        now = time.time()
        delta = now - self.last
        self.tokens = min(self.capacity, self.tokens + delta * self.refill_rate)
        self.last = now
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False


class RateLimiter:
    def __init__(self):
        self.buckets: Dict[str, TokenBucket] = {}

    def ensure(self, key: str, capacity: int, refill_rate_per_sec: float):
        if key not in self.buckets:
            self.buckets[key] = TokenBucket(capacity, refill_rate_per_sec)

    def allow(self, key: str, tokens: int = 1) -> bool:
        bucket = self.buckets.get(key)
        if not bucket:
            return True
        return bucket.consume(tokens)
