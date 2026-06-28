"""Normalization utilities for module2.

This package provides a lightweight compatibility layer so downstream modules,
including module3, can import a normalized job schema from module2 without
requiring the full backend-specific module2 implementation.
"""

from .schemas import NormalizedJob, normalize_job, normalize_batch

__all__ = ["NormalizedJob", "normalize_job", "normalize_batch"]
