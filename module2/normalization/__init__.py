"""Submodule 2: Normalization Engine

Convert RawJobData (from adapters) into unified NormalizedJob schema.
"""

from .normalizer import Normalizer, get_normalizer, normalize, normalize_batch
from .schemas import NormalizedJob
from .helpers import (
    normalize_text,
    clean_title,
    classify_job_type,
    normalize_location,
    canonicalize_url,
    extract_required_years,
    tokenize,
    is_remote_job,
    is_usa_location,
)

__all__ = [
    "NormalizedJob",
    "Normalizer",
    "get_normalizer",
    "normalize",
    "normalize_batch",
    "normalize_text",
    "clean_title",
    "classify_job_type",
    "normalize_location",
    "canonicalize_url",
    "extract_required_years",
    "tokenize",
    "is_remote_job",
    "is_usa_location",
]
