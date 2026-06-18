"""Simple embedding cache using shelve for persistence.
"""
from __future__ import annotations

import shelve
import hashlib
from typing import Optional, List
from pathlib import Path

CACHE_PATH = Path(__file__).parent / "embeddings.db"


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_embedding(text: str) -> Optional[List[float]]:
    with shelve.open(str(CACHE_PATH)) as db:
        return db.get(_key(text))


def set_embedding(text: str, vector: List[float]) -> None:
    with shelve.open(str(CACHE_PATH)) as db:
        db[_key(text)] = vector


def bulk_get(texts: List[str]) -> dict:
    out = {}
    with shelve.open(str(CACHE_PATH)) as db:
        for t in texts:
            v = db.get(_key(t))
            if v is not None:
                out[t] = v
    return out
