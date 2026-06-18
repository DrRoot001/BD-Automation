"""Proxy manager to rotate proxies (simple list-based)."""
from __future__ import annotations

from typing import List, Optional
import itertools


class ProxyManager:
    def __init__(self, proxies: Optional[List[str]] = None):
        self.proxies = proxies or []
        self._iter = itertools.cycle(self.proxies) if self.proxies else None

    def next(self) -> Optional[str]:
        if not self._iter:
            return None
        return next(self._iter)

    def add(self, proxy: str) -> None:
        self.proxies.append(proxy)
        self._iter = itertools.cycle(self.proxies)
