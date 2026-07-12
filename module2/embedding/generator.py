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
    from google import genai
    from google.genai import types as genai_types
    GEMINI_AVAILABLE = True
except Exception:
    GEMINI_AVAILABLE = False

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
    Tries Gemini if GEMINI_API_KEY is set.
    Otherwise uses deterministic pseudo embeddings.
    """
    if OPENAI_AVAILABLE and os.getenv("OPENAI_API_KEY"):
        # Use OpenAI embeddings API (text-embedding-3-small or similar)
        try:
            model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
            if hasattr(openai, "OpenAI"):
                # Bound the call: the OpenAI SDK defaults to a 10-minute timeout
                # with retries, which (when invoked from a request handler) can
                # stall the pipeline. Fail fast to the pseudo-embedding fallback.
                client = openai.OpenAI(timeout=20.0, max_retries=1)
                resp = client.embeddings.create(model=model, input=texts)
                return [r.embedding for r in resp.data]
            else:
                resp = openai.Embedding.create(model=model, input=texts)
                return [r["embedding"] for r in resp["data"]]
        except Exception:
            # Fallback to next methods
            pass

    if GEMINI_AVAILABLE and os.getenv("GEMINI_API_KEY"):
        try:
            client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
            response = client.models.embed_content(
                model="gemini-embedding-001",
                contents=texts,
                config=genai_types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT")
            )
            embeddings = [emb.values for emb in response.embeddings]
            
            # Handle case where only a single embedding is returned (not wrapped in a list)
            if embeddings and not isinstance(embeddings[0], list):
                embeddings = [embeddings]
                
            padded_embeddings = []
            for emb in embeddings:
                # Handle dimension scaling (truncating or padding to 1536)
                if len(emb) > EMBEDDING_DIM:
                    emb = emb[:EMBEDDING_DIM]
                elif len(emb) < EMBEDDING_DIM:
                    emb = emb + [0.0] * (EMBEDDING_DIM - len(emb))
                
                # Re-normalize unit vector to ensure cosine distances are correct
                norm = math.sqrt(sum(x * x for x in emb)) or 1.0
                emb = [x / norm for x in emb]
                padded_embeddings.append(emb)
            return padded_embeddings
        except Exception:
            # Fallback to pseudo
            return [_pseudo_embedding(t) for t in texts]
    else:
        return [_pseudo_embedding(t) for t in texts]
