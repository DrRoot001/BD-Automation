"""M3 must not destroy the candidate's real location when tailoring a resume.

Live chain this pins (2026-07-17, Mark Anderson → Softthink/CareerPlug):
`candidates.location` was just "US" → `resume_tailor` used the DB value with no
fallback → the TAILORED PDF header read "... | US | ..." → M4 downloads that
tailored PDF and runs `resume_enricher` on it → no city/state found → the
identity card said `Location: US` → the live form's location question was
answered "not provided".

Self-fulfilling: the coarse value overwrites the real one, then the enricher
cannot recover what the pipeline itself erased.

Two defects, both fixed here:
  1. `basics.location` took the DB value ONLY (email/phone both fall back to the
     parsed resume; location did not), and "US" is non-blank so a plain
     `_first_non_blank` fallback would never fire anyway.
  2. The tailoring PROMPT ordered the LLM to fabricate: "You MUST ensure the
     location reflects a USA residence. If it is outside the USA, change it to a
     suitable US tech hub." That misrepresents a real candidate to an employer.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from module3.tailoring.resume_tailor import (  # noqa: E402
    _best_location,
    _is_generic_location,
)

_TAILOR_SRC = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../../..", "module3", "tailoring", "resume_tailor.py")
)
_PARSER_SRC = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../../..", "module3", "parser", "resume_parser.py")
)


def _src(p: str) -> str:
    with open(p, encoding="utf-8") as fh:
        return fh.read()


# ── the generic-location predicate ───────────────────────────────────────────


@pytest.mark.parametrize("v", ["US", "USA", "usa", "united states", "u.s.", "", None,
                               "America", "remote", "N/A", "-", "CA"])
def test_country_only_values_are_generic(v):
    assert _is_generic_location(v) is True


@pytest.mark.parametrize("v", ["Austin, TX, USA", "Austin, Texas 78701",
                               "Austin, TX 78701, USA", "London, UK",
                               "San Francisco, California"])
def test_real_addresses_are_not_generic(v):
    assert _is_generic_location(v) is False


# ── picking the richer location ──────────────────────────────────────────────


def test_thin_db_value_loses_to_the_resume_header():
    """THE bug: DB 'US' must not overwrite the resume's real city."""
    assert _best_location("US", "Austin, Texas 78701") == "Austin, Texas 78701"


def test_real_db_value_wins_over_the_resume():
    """A curated DB address stays the source of truth."""
    assert _best_location("Austin, TX, USA", "Somewhere Else, NY") == "Austin, TX, USA"


def test_zip_and_country_are_preserved_verbatim():
    """No normalising — the ZIP must survive to reach the live form."""
    got = _best_location("US", "Austin, Texas 78701, USA")
    assert got == "Austin, Texas 78701, USA"
    assert "78701" in got


def test_blank_db_falls_back_to_the_resume():
    assert _best_location("", "Austin, TX") == "Austin, TX"
    assert _best_location(None, "Austin, TX") == "Austin, TX"


def test_both_generic_degrades_gracefully_without_inventing():
    """Never fabricate — keep what little we have."""
    assert _best_location("US", "USA") == "US"
    assert _best_location("", "") == ""


def test_non_us_location_is_kept_not_relocated():
    """A UK candidate must stay in the UK."""
    assert _best_location("", "London, EC1A 1BB, UK") == "London, EC1A 1BB, UK"


# ── the wiring + the prompt ──────────────────────────────────────────────────


def test_tailor_uses_best_location_not_first_non_blank():
    src = _src(_TAILOR_SRC)
    assert '"location": _best_location(' in src, (
        "basics.location must go through _best_location — _first_non_blank "
        "accepts the non-blank 'US' and erases the real address"
    )
    assert '"location": _first_non_blank(candidate_profile.get("location")),' not in src


def test_parser_extracts_location_from_the_resume_header():
    """Without this there is no fallback source at all."""
    src = _src(_PARSER_SRC)
    assert "location: Optional[str]" in src, (
        "ResumeSection must extract location, or the resume header (often the "
        "ONLY place the real city/state/ZIP exists) is never captured"
    )
    assert "VERBATIM" in src or "verbatim" in src


def test_prompt_no_longer_tells_the_llm_to_fabricate_a_us_city():
    """Fabricating a residence misrepresents a real person to an employer."""
    src = _src(_TAILOR_SRC)
    assert "change it to a suitable US tech hub" not in src, (
        "the tailoring prompt must NOT instruct the model to relocate the "
        "candidate to an invented city"
    )
    assert "reflects a USA residence" not in src


def test_prompt_demands_verbatim_location_passthrough():
    src = _src(_TAILOR_SRC)
    block = src[src.index("- LOCATION:") : src.index("- LINKS:")]
    assert "EXACTLY" in block
    assert "ZIP" in block
    assert "NEVER invent" in block or "never invent" in block.lower()


# ── the cached-parse escape hatch ────────────────────────────────────────────


def test_stale_parsed_json_triggers_a_reparse_for_location():
    """Every parsed_json stored before the parser learned `location` has
    email+phone but no location. If the re-parse guard only checks email/phone,
    it never fires for those rows, sections.location stays None, and the tailored
    resume silently falls back to the country-only DB value — reintroducing the
    'US' bug the parser change was meant to fix.
    """
    src = _src(os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../../..", "module3", "orchestrator.py")
    ))
    block = src[src.index("def _resume_contact_is_incomplete"):]
    block = block[: block.index("async def")]
    assert '"location"' in block, (
        "_resume_contact_is_incomplete must treat a missing location as "
        "incomplete, or stale parsed_json is never re-scanned"
    )
