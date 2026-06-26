"""Email verification — Gmail OAuth code-fetching for ATS submit-flow walls.

When a Greenhouse/Lever/Ashby form submits, the ATS often emails a 6–8 digit
verification code to the candidate's address and renders a code-input page.
This package fetches that code from the candidate's Gmail inbox via OAuth
(using `candidates.google_refresh_token`) and types it into the form.

Public API:
    fetch_verification_code(candidate_id, ...) -> Optional[str]
    detect_code_input(page, frame) -> Optional[CodeInputShape]
    fill_code(page, frame, shape, code) -> bool
"""
from .code_fetcher import (
    fetch_verification_code,
    detect_code_input,
    fill_code,
    CodeInputShape,
)

__all__ = [
    "fetch_verification_code",
    "detect_code_input",
    "fill_code",
    "CodeInputShape",
]
