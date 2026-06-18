"""Deduplication orchestration combining URL, fuzzy, and embedding checks."""
from __future__ import annotations

from typing import Optional
from ..extraction.url_canonicalizer import url_hash, are_urls_equivalent
from .redis_cache import RedisCache
from .fuzzy_matcher import title_company_score
from .embedding_matcher import is_similar


class Deduplicator:
    def __init__(self, redis_url: Optional[str] = None):
        self.cache = RedisCache(redis_url)

    def is_duplicate_by_url(self, url: str) -> bool:
        key = f"job_hash:{url_hash(url)}"
        return self.cache.exists(key)

    def mark_seen_url(self, url: str, job_id: str) -> None:
        key = f"job_hash:{url_hash(url)}"
        self.cache.set(key, job_id, ex=60 * 60 * 24 * 7)  # 7 days

    def fuzzy_duplicate(self, job_a, job_b, threshold: float = 0.88) -> bool:
        score = title_company_score(job_a.title, job_a.company, job_b.title, job_b.company)
        return score >= threshold

    def embedding_duplicate(self, emb_a, emb_b, threshold: float = 0.92) -> bool:
        return is_similar(emb_a, emb_b, threshold=threshold)
