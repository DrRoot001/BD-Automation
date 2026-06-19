"""Simple Redis-like cache with optional redis-py fallback.

Used for Layer 1 dedup (URL hash lookup). If redis is unavailable, uses in-memory dict.
"""
from __future__ import annotations

import os
import time
from typing import Optional

try:
    import redis
    REDIS_AVAILABLE = True
except Exception:
    REDIS_AVAILABLE = False

class RedisCache:
    def __init__(self, url: Optional[str] = None):
        self._local = {}
        if REDIS_AVAILABLE and url:
            self.client = redis.from_url(url)
        else:
            self.client = None

    def get(self, key: str):
        if self.client:
            try:
                return self.client.get(key)
            except Exception as e:
                print(f"Redis get failed ({e}), falling back to local cache.")
                self.client = None
        return self._local.get(key)

    def set(self, key: str, value, ex: Optional[int] = None):
        if self.client:
            try:
                self.client.set(key, value, ex=ex)
                return
            except Exception as e:
                print(f"Redis set failed ({e}), falling back to local cache.")
                self.client = None
        
        self._local[key] = value
        if ex:
            # naive expiration
            self._local[f"{key}__exp"] = time.time() + ex

    def exists(self, key: str) -> bool:
        if self.client:
            try:
                return bool(self.client.exists(key))
            except Exception as e:
                print(f"Redis exists failed ({e}), falling back to local cache.")
                self.client = None

        exp = self._local.get(f"{key}__exp")
        if exp and time.time() > exp:
            self._local.pop(key, None)
            self._local.pop(f"{key}__exp", None)
            return False
        return key in self._local

