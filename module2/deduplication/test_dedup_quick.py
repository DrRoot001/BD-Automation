"""Quick tests for deduplication (URL, fuzzy, embedding)."""
from __future__ import annotations

import sys
from pathlib import Path
# add project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from module2.deduplication.deduplicate import Deduplicator
from module2.deduplication.fuzzy_matcher import title_company_score
from module2.deduplication.embedding_matcher import cosine
from module2.embedding.generator import generate_embedding


def test_url_cache():
    d = Deduplicator()
    url = "https://example.com/jobs/123?utm=1"
    assert not d.is_duplicate_by_url(url)
    d.mark_seen_url(url, "job1")
    assert d.is_duplicate_by_url(url)
    print("✓ URL cache works")


def test_fuzzy():
    s = title_company_score("Senior Python Engineer", "Acme", "Sr. Python Engineer", "Acme Inc")
    assert s > 0.7
    print("✓ Fuzzy matcher score", s)


def test_embedding_similarity():
    a = generate_embedding(["This is a test embedding"])[0]
    b = generate_embedding(["This is a test embedding"])[0]
    assert cosine(a, b) > 0.99
    print("✓ Embedding similarity high for identical texts")


def main():
    test_url_cache()
    test_fuzzy()
    test_embedding_similarity()
    print("\nAll dedup tests passed")

if __name__ == "__main__":
    main()
