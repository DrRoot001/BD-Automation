"""Retry utilities for network calls."""
from __future__ import annotations

import time
import functools
from typing import Callable


def retry(times: int = 3, backoff: float = 0.5):
    def decorator(f: Callable):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return f(*args, **kwargs)
                except Exception:
                    attempt += 1
                    if attempt >= times:
                        raise
                    time.sleep(backoff * (2 ** (attempt - 1)))
        return wrapper
    return decorator
