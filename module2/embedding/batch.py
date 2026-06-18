"""Batch embedding helpers.
"""
from __future__ import annotations

from typing import List
from .generator import generate_embedding
from .cache import bulk_get, set_embedding


def batch_generate(texts: List[str], batch_size: int = 16) -> List[List[float]]:
    """Generate embeddings with simple caching.

    Returns list of vectors aligned with `texts`.
    """
    result = [None] * len(texts)
    cached = bulk_get(texts)
    to_request = []
    idxs = []
    for i, t in enumerate(texts):
        if t in cached:
            result[i] = cached[t]
        else:
            to_request.append(t)
            idxs.append(i)
    if to_request:
        # Chunk requests
        for i in range(0, len(to_request), batch_size):
            chunk = to_request[i : i + batch_size]
            vectors = generate_embedding(chunk)
            for j, v in enumerate(vectors):
                set_embedding(chunk[j], v)
                result[idxs[i + j]] = v
    return result
