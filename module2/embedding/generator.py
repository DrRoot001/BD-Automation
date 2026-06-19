"""Embedding generator with OpenAI fallback.

Provides `generate_embedding(texts)` which returns list of float vectors.
If OpenAI is not configured, returns deterministic pseudo-random vectors.
"""
from __future__ import annotations

import hashlib
import math
import os
from typing import List

try:
    import openai
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False

EMBEDDING_DIM = 1536


def _pseudo_embedding(text: str, dim: int = EMBEDDING_DIM) -> List[float]:
    """Deterministic pseudo-random vector from SHA256 hash of text."""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # Expand to dim floats by repeating hash bytes
    vec = []
    i = 0
    while len(vec) < dim:
        b = h[i % len(h)]
        vec.append((b / 255.0) * 2 - 1)  # map to [-1,1]
        i += 1
    # normalize
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def generate_embedding(texts: List[str]) -> List[List[float]]:
    """Generate embeddings for a list of texts.

    Tries OpenAI if environment variable `OPENAI_API_KEY` is set and package available.
    Otherwise uses deterministic pseudo embeddings.
    """
    if OPENAI_AVAILABLE and os.getenv("OPENAI_API_KEY"):
        # Use OpenAI embeddings API (text-embedding-3-small or similar)
        try:
            model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
            if hasattr(openai, "OpenAI"):
                client = openai.OpenAI()
                resp = client.embeddings.create(model=model, input=texts)
                return [r.embedding for r in resp.data]
            else:
                resp = openai.Embedding.create(model=model, input=texts)
                return [r["embedding"] for r in resp["data"]]
        except Exception:
            # Fallback to pseudo
            return [_pseudo_embedding(t) for t in texts]
    else:
        return [_pseudo_embedding(t) for t in texts]
