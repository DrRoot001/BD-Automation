"""A résumé must never land in a cover-letter slot.

Live bug (2026-07-17, Mark Anderson → Softthink/CareerPlug — the run that DID
submit, confirmed by a thank-you email): the operator saw the résumé uploaded
into the cover-letter section as well as the résumé section.

Cause: ``_execute_action``'s upload routing keyed ONLY on the model's own label::

    file_key = (action.value or "").lower()
    path = resume_path if "cover" not in file_key else cover_letter_path

so ``{"selector": "#cover_letter", "value": "resume"}`` routed to ``resume_path``
and uploaded the résumé into the cover-letter field. The system prompt already
said "you have no cover letter, skip it" — but a prompt is a request, not a
guarantee, and the model ignored it. The target slot is a fact about the page;
the label is the model's opinion. The page wins.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

_LOOP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "agent", "loop.py"))


def _upload_block() -> str:
    with open(_LOOP, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index('if kind == "upload_file":')
    return src[start : start + 2000]


def test_routing_considers_the_target_not_only_the_model_label():
    block = _upload_block()
    assert "target_hint" in block and "action.selector" in block, (
        "upload routing must inspect the TARGET selector/label, not just "
        "action.value — otherwise {'selector':'#cover_letter','value':'resume'} "
        "uploads the résumé into the cover-letter slot"
    )
    assert "target_is_cover" in block


def test_resume_is_refused_for_a_cover_slot_when_no_cover_letter_exists():
    block = _upload_block()
    seg = block[block.index("if target_is_cover:") :]
    assert "if not cover_letter_path:" in seg
    assert "return False" in seg, (
        "with no cover letter, an upload aimed at a cover-letter slot must be "
        "REFUSED — never silently substituted with the résumé"
    )


def test_cover_slot_uses_the_cover_letter_when_one_exists():
    block = _upload_block()
    seg = block[block.index("if target_is_cover:") :]
    assert "path = cover_letter_path" in seg


def test_the_old_label_only_routing_is_gone():
    """The exact line that caused the bug must not be the first decision."""
    block = _upload_block()
    first = block[: block.index("target_hint")]
    assert 'path = resume_path if "cover" not in file_key else cover_letter_path' not in first, (
        "label-only routing must not run before the target check"
    )


def test_cover_detection_matches_real_slot_names():
    """The heuristic must catch the field names ATSes actually use."""
    block = _upload_block()
    m = re.search(r'target_is_cover = \((.*?)\)\n', block, re.S)
    assert m, "could not locate target_is_cover"
    expr = m.group(1)
    assert '"cover"' in expr and '"letter"' in expr, (
        "must match both 'cover' (cover_letter, coverLetter) and 'letter' "
        "(letter_of_interest)"
    )


def test_resume_slot_still_gets_the_resume():
    """The guard must not break the normal path."""
    block = _upload_block()
    seg = block[block.index("else:") :]
    assert "resume_path" in seg
