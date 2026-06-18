"""Submodule 7: Deduplication Engine

3-layer deduplication:
- Layer 1: URL canonicalization + Redis cache (Submodule 5)
- Layer 2: Fuzzy title+company match
- Layer 3: Embedding cosine similarity (pgvector)
"""
